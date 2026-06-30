#!/usr/bin/env python3
"""
Study Book Ingestion Script

Reads a PDF, extracts text by page, chunks it, embeds via Supabase embed function,
stores in study_book_chunks. Auto-populates study_topics from detected chapters.

For presentation/slide PDFs with sparse text, automatically falls back to vision OCR
using the Claude CLI to extract content from rendered page images.

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
import subprocess
import tempfile
import shutil
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import pdfplumber

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_USER_ID", "")
CHUNK_CHARS   = 1800
CHUNK_OVERLAP = 300
VISION_THRESHOLD_CHARS_PER_PAGE = 100

CLAUDE_BIN = (
    "/home/anton/.npm-global/lib/node_modules/@anthropic-ai/claude-code"
    "/node_modules/@anthropic-ai/claude-code-linux-x64/claude"
)


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
    # Exact case-insensitive match first
    rows = _supabase_get("study_areas", f"name=ilike.{urllib.parse.quote(area_name)}&select=id,name")
    if rows:
        return rows[0]["id"]
    # Partial match: area_name is a substring of the stored name (e.g. "research paper" → "Research Papers")
    rows = _supabase_get("study_areas", f"name=ilike.{urllib.parse.quote('%' + area_name + '%')}&select=id,name")
    if rows:
        print(f"  Fuzzy matched area '{area_name}' → '{rows[0]['name']}'")
        return rows[0]["id"]
    return None


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


def extract_pages_pdfplumber(pdf_path: Path) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def ocr_page_vision(image_path: str) -> str:
    prompt = (
        "Read the image at " + image_path + " and extract all visible content: "
        "text, equations, labels, and figure descriptions. "
        "Return only the extracted content as plain text, no commentary."
    )
    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions", "--allowedTools", "Read"],
        capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()


def extract_pages_vision(pdf_path: Path) -> list[str]:
    try:
        import fitz
    except ImportError:
        print("PyMuPDF not installed — cannot use vision fallback")
        return []

    tmp_dir = tempfile.mkdtemp(prefix="study_ocr_")
    try:
        doc = fitz.open(str(pdf_path))
        page_texts = []
        for i, page in enumerate(doc):
            img_path = os.path.join(tmp_dir, f"page_{i+1:03d}.png")
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            pix.save(img_path)
            print(f"  OCR page {i+1}/{len(doc)}...", flush=True)
            text = ocr_page_vision(img_path)
            page_texts.append(text)
            time.sleep(0.5)
        doc.close()
        return page_texts
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--area", required=True, help="Study area name (e.g. 'Linear Algebra')")
    parser.add_argument("--title", help="Book title (defaults to filename)")
    parser.add_argument("--author", default="", help="Book author")
    parser.add_argument("--force-vision", action="store_true", help="Skip pdfplumber, always use vision OCR")
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

    # Extract text — try pdfplumber first, fall back to vision if sparse
    page_texts = extract_pages_pdfplumber(pdf_path)
    total_pages = len(page_texts)
    total_chars = sum(len(t) for t in page_texts)
    avg_chars = total_chars / total_pages if total_pages else 0

    use_vision = args.force_vision or (avg_chars < VISION_THRESHOLD_CHARS_PER_PAGE)
    if use_vision and not args.force_vision:
        print(f"Sparse text detected ({avg_chars:.0f} chars/page avg) — switching to vision OCR")

    if use_vision:
        page_texts = extract_pages_vision(pdf_path)
        if not page_texts:
            print("Vision OCR failed, aborting.")
            sys.exit(1)
        total_pages = len(page_texts)

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
            if len(chunk.strip()) < 30:
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

    for i, text in enumerate(page_texts):
        try:
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
        f"Mode: {'vision OCR' if use_vision else 'text'}\n"
        f"Pages: {total_pages}\n"
        f"Chunks: {chunks_total}\n"
        f"Chapters detected: {len(chapters_seen)}\n"
        f"Errors: {errors}"
    )
    print(f"\n{summary}")
    send_telegram(summary)


if __name__ == "__main__":
    main()
