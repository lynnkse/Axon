#!/usr/bin/env python3
"""
Study Book Ingestion Script

Reads a PDF, extracts text by page, chunks it, embeds via Supabase embed function,
stores in study_book_chunks. Auto-populates study_topics from detected chapters.

Usage:
  set -a; source /path/to/.env; set +a
  python3 study_ingest.py <path/to/book.pdf> --area "Linear Algebra" [--title "..."] [--author "..."]
"""

import os
import sys
import json
import time
import re
import argparse
import urllib.request
import urllib.error
from pathlib import Path

import pdfplumber

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_USER_ID", "")
CHUNK_CHARS   = 1800
CHUNK_OVERLAP = 300
# ─────────────────────────────────────────────────────────────────────────────


def _request(method: str, url: str, data: dict = None, headers: dict = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _supabase_get(table: str, params: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _supabase_insert(table: str, row: dict) -> dict:
    data = json.dumps([row]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}",
        data=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())[0]


def trigger_embed(row_id: str, content: str):
    data = json.dumps({"record": {"id": row_id, "content": content}, "table": "study_book_chunks"}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/embed",
        data=data,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


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


def chunk_text(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return chunks


CHAPTER_RE = re.compile(
    r'^(chapter\s+\d+|section\s+[\d.]+|\d+\.\s+[A-Z])',
    re.IGNORECASE | re.MULTILINE
)

def detect_chapter(text: str) -> str | None:
    m = CHAPTER_RE.search(text[:300])
    return m.group(0).strip() if m else None


def get_area_id(area_name: str) -> str | None:
    rows = _supabase_get("study_areas", f"name=eq.{urllib.parse.quote(area_name)}&select=id")
    return rows[0]["id"] if rows else None


def create_book(title: str, author: str, area_id: str, file_name: str, total_pages: int) -> str:
    row = _supabase_insert("study_books", {
        "title": title,
        "author": author,
        "area_id": area_id,
        "file_name": file_name,
        "total_pages": total_pages,
    })
    return row["id"]


def upsert_topic(book_id: str, area_id: str, name: str):
    rows = _supabase_get("study_topics", f"name=eq.{urllib.parse.quote(name)}&book_id=eq.{book_id}&select=id")
    if not rows:
        _supabase_insert("study_topics", {
            "name": name,
            "area_id": area_id,
            "book_id": book_id,
            "status": "not_started",
            "progress": 0,
        })


def main():
    import urllib.parse

    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--area", required=True, help="Study area name (e.g. 'Linear Algebra')")
    parser.add_argument("--title", help="Book title (defaults to filename)")
    parser.add_argument("--author", default="", help="Book author")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Missing SUPABASE_URL or SUPABASE_ANON_KEY")
        sys.exit(1)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    title = args.title or pdf_path.stem
    print(f"Ingesting: {title} ({args.area})")

    area_id = get_area_id(args.area)
    if not area_id:
        print(f"Area '{args.area}' not found in study_areas. Create it first.")
        sys.exit(1)

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"Total pages: {total_pages}")

        book_id = create_book(title, args.author, area_id, pdf_path.name, total_pages)
        print(f"Book ID: {book_id}")

        chunks_total = errors = 0
        chapters_seen = set()
        current_chapter = None
        buffer = ""
        buffer_page = 1

        def flush_buffer(chapter, page):
            nonlocal chunks_total, errors
            if not buffer.strip():
                return
            for chunk in chunk_text(buffer):
                if len(chunk.strip()) < 100:
                    continue
                try:
                    row = _supabase_insert("study_book_chunks", {
                        "book_id": book_id,
                        "chapter": chapter,
                        "content": chunk,
                        "page_num": page,
                    })
                    trigger_embed(row["id"], chunk)
                    chunks_total += 1
                    time.sleep(0.1)
                except Exception as e:
                    print(f"  ERROR chunk: {e}")
                    errors += 1

        for i, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
                ch = detect_chapter(text)
                if ch and ch != current_chapter:
                    flush_buffer(current_chapter, buffer_page)
                    buffer = ""
                    buffer_page = i + 1
                    current_chapter = ch
                    if ch not in chapters_seen:
                        chapters_seen.add(ch)
                        upsert_topic(book_id, area_id, ch)
                        print(f"  Chapter: {ch}")
                buffer += " " + text

                if (i + 1) % 20 == 0:
                    print(f"  Page {i+1}/{total_pages}, chunks so far: {chunks_total}")

            except Exception as e:
                print(f"  ERROR page {i+1}: {e}")
                errors += 1

        flush_buffer(current_chapter, buffer_page)

    summary = (
        f"Study book ingestion complete!\n"
        f"Book: {title}\n"
        f"Area: {args.area}\n"
        f"Pages: {total_pages}\n"
        f"Chunks: {chunks_total}\n"
        f"Chapters detected: {len(chapters_seen)}\n"
        f"Errors: {errors}"
    )
    print(f"\n{summary}")
    send_telegram(summary)


if __name__ == "__main__":
    main()
