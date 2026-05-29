#!/usr/bin/env python3
"""
Study Book Search MCP Tool

Provides semantic search over ingested study books so Claude can find
relevant content when testing the user or answering study questions.
"""

import os
import sys
import json
import asyncio
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def _load_env() -> dict:
    env_path = Path(__file__).parent.parent / ".env"
    result = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return result

_env = _load_env()

def _get(key: str) -> str:
    return os.environ.get(key) or _env.get(key, "")

SUPABASE_URL = _get("SUPABASE_URL")
SUPABASE_KEY = _get("SUPABASE_ANON_KEY")


def search_study_content(query: str, area: str = "", limit: int = 6) -> list[dict]:
    data = json.dumps({"query": query, "match_count": limit, "table": "study_book_chunks"}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/search",
        data=data,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            results = json.loads(r.read().decode())
        return results if isinstance(results, list) else [results]
    except Exception as e:
        # Fallback: full-text search via REST
        try:
            encoded = urllib.parse.quote(query[:100])
            url = f"{SUPABASE_URL}/rest/v1/study_book_chunks?content=ilike.*{encoded}*&limit={limit}&select=content,chapter,page_num"
            req2 = urllib.request.Request(url, headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            })
            with urllib.request.urlopen(req2, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e2:
            return [{"error": str(e2)}]


def get_study_progress() -> dict:
    url = f"{SUPABASE_URL}/rest/v1/study_topics?select=name,status,progress,last_tested_at&order=progress.asc"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"topics": json.loads(r.read().decode())}
    except Exception as e:
        return {"error": str(e)}


def update_topic_progress(topic_name: str, progress: int, status: str = "") -> dict:
    import urllib.request as ur
    patch_data: dict = {"progress": progress}
    if status:
        patch_data["status"] = status
    if progress == 100:
        patch_data["status"] = "done"
    elif progress > 0:
        patch_data["status"] = "in_progress"

    from datetime import datetime, timezone
    patch_data["last_tested_at"] = datetime.now(timezone.utc).isoformat()

    encoded_name = urllib.parse.quote(topic_name)
    data = json.dumps(patch_data).encode()
    req = ur.Request(
        f"{SUPABASE_URL}/rest/v1/study_topics?name=eq.{encoded_name}",
        data=data,
        method="PATCH",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    try:
        with ur.urlopen(req, timeout=15) as r:
            return {"updated": json.loads(r.read().decode())}
    except Exception as e:
        return {"error": str(e)}


def list_books() -> dict:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/study_books?select=id,title,author,total_pages,created_at",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"books": json.loads(r.read().decode())}
    except Exception as e:
        return {"error": str(e)}


# ── MCP stdio protocol ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_study_content",
        "description": (
            "Search the user's study books by semantic similarity. "
            "Use this to find relevant theory, definitions, or examples when testing the user "
            "or answering study questions. Returns matching text chunks with chapter and page info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for (concept, theorem, topic)"},
                "area": {"type": "string", "description": "Optional: filter by study area name (e.g. 'Linear Algebra')"},
                "limit": {"type": "integer", "description": "Number of results (default 6)", "default": 6},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_study_progress",
        "description": "Get the user's current progress across all study topics — what's done, in progress, not started.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_topic_progress",
        "description": "Update progress percentage (0–100) for a study topic after testing the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic_name": {"type": "string", "description": "Exact topic name from study_topics"},
                "progress": {"type": "integer", "description": "Progress 0–100"},
                "status": {"type": "string", "description": "Optional override: not_started|in_progress|done|needs_review"},
            },
            "required": ["topic_name", "progress"],
        },
    },
    {
        "name": "list_study_books",
        "description": "List all ingested study books with their areas and page counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def handle_message(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "study-search", "version": "1.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        try:
            if name == "search_study_content":
                result = search_study_content(args["query"], args.get("area", ""), args.get("limit", 6))
            elif name == "get_study_progress":
                result = get_study_progress()
            elif name == "update_topic_progress":
                result = update_topic_progress(args["topic_name"], args["progress"], args.get("status", ""))
            elif name == "list_study_books":
                result = list_books()
            else:
                result = {"error": f"Unknown tool: {name}"}
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]},
            }

    if method == "notifications/initialized":
        return None

    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


async def main():
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line.decode())
            response = await handle_message(msg)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except Exception:
            break


if __name__ == "__main__":
    asyncio.run(main())
