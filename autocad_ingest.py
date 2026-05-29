#!/usr/bin/env python3
"""
AutoCAD 2025 Help Ingestion Script

Reads all HTML files from the offline help, extracts text,
chunks into ~400 token pieces, embeds via Supabase embed function,
stores in autocad_docs table. Sends Telegram notification when done.

Usage:
  set -a; source /path/to/.env; set +a
  nohup python3 autocad_ingest.py > /tmp/autocad_ingest.log 2>&1 &
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.error
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
HELP_DIR      = Path("/home/lynnkse/autocad_help/Help/wrapped-filesACD")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_USER_ID", "")
CHUNK_CHARS   = 1600
CHUNK_OVERLAP = 320
MIN_LENGTH    = 200   # skip files shorter than this
PROGRESS_FILE = Path("/tmp/autocad_ingest_progress.json")
LOG_EVERY     = 100   # print progress every N files
# ─────────────────────────────────────────────────────────────────────────────


def extract_text(path: Path) -> tuple[str, str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        # Files are JS-wrapped: var topic = "...html..."; // SIG block
        end = raw.find('"\n// SIG')
        if end == -1:
            end = raw.rfind('";\n')
        html = raw[12:end].replace('\\"', '"').replace("\\'", "'").replace('\\r\\n', '\r\n').replace('\\n', '\n')
        soup = BeautifulSoup(html, "lxml")
        title = soup.find("title")
        title = title.get_text(strip=True) if title else path.stem
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()
        return title, text
    except Exception:
        return path.stem, ""


def chunk_text(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return chunks


def insert_row(row: dict) -> str:
    data = json.dumps([row]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/autocad_docs",
        data=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)[0]["id"]


def trigger_embed(row_id: str, content: str) -> str:
    data = json.dumps({"record": {"id": row_id, "content": content}, "table": "autocad_docs"}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/embed",
        data=data,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def send_telegram(message: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    data = json.dumps({"chat_id": CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def load_progress() -> set:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()


def save_progress(done: set):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done), f)


def main():
    missing = [v for v in ["SUPABASE_URL", "SUPABASE_ANON_KEY"]
               if not os.environ.get(v)]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    files = sorted(HELP_DIR.glob("*.htm.js"))
    total = len(files)
    print(f"Total files: {total}")

    done = load_progress()
    print(f"Already done: {len(done)}")

    pending = [f for f in files if f.name not in done]
    print(f"Pending: {len(pending)}\n")

    processed = skipped = errors = chunks_total = 0
    start_time = time.time()

    for i, path in enumerate(pending):
        try:
            title, text = extract_text(path)

            if len(text) < MIN_LENGTH:
                skipped += 1
                done.add(path.name)
                if i % LOG_EVERY == 0:
                    save_progress(done)
                continue

            chunks = chunk_text(text)
            for j, chunk in enumerate(chunks):
                row_id = insert_row({
                    "filename": path.name,
                    "title": title,
                    "chunk_index": j,
                    "content": chunk,
                })
                trigger_embed(row_id, chunk)
                chunks_total += 1
                time.sleep(0.15)

            done.add(path.name)
            processed += 1

            if (i + 1) % LOG_EVERY == 0:
                save_progress(done)
                elapsed = time.time() - start_time
                rate = len(done) / elapsed * 60
                remaining = (len(pending) - i - 1) / (rate / 60) / 60 if rate > 0 else 0
                print(f"[{len(done)}/{total}] processed={processed} skipped={skipped} "
                      f"chunks={chunks_total} errors={errors} "
                      f"rate={rate:.0f}/min eta={remaining:.0f}min")

        except Exception as e:
            print(f"ERROR {path.name}: {e}")
            errors += 1
            time.sleep(2)

    save_progress(done)

    elapsed = (time.time() - start_time) / 60
    summary = (
        f"AutoCAD docs ingestion complete!\n"
        f"Files processed: {processed}\n"
        f"Chunks in DB: {chunks_total}\n"
        f"Skipped (too short): {skipped}\n"
        f"Errors: {errors}\n"
        f"Time: {elapsed:.0f} min"
    )
    print(f"\n{summary}")
    send_telegram(summary)


if __name__ == "__main__":
    main()
