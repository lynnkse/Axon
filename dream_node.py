"""
DreamNode — Relay v2

Runs on a configurable interval and samples content from the user's Supabase
knowledge base (memory, insights, study, food) to find non-obvious patterns
and connections. The result is emitted as a [DREAM: ...] tag which
supabase_client.py saves to the dreams table and embeds for future retrieval.

Called from proactive_node.py on a separate interval.
"""

import json
import logging
import random
import urllib.request
import urllib.parse

import config

log = logging.getLogger(__name__)

DREAM_INTERVAL = int(config.get("DREAM_INTERVAL", "3600"))  # default: every hour


def _rest_get(path: str, params: str = "") -> list:
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return []
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"Dream sample fetch failed ({path}): {e}")
        return []


def _sample_knowledge() -> list[dict]:
    """Sample a diverse set of content chunks from across Supabase tables."""
    chunks = []

    # Memory facts and goals
    rows = _rest_get("memory", "type=in.(fact,goal)&completed_at=is.null&select=content,type&limit=100")
    for r in random.sample(rows, min(3, len(rows))):
        chunks.append({"source": f"memory:{r['type']}", "content": r["content"][:300]})

    # Insights
    rows = _rest_get("insights", "select=content,type&order=created_at.desc&limit=50")
    for r in random.sample(rows, min(3, len(rows))):
        chunks.append({"source": f"insight:{r.get('type','')}", "content": r["content"][:300]})

    # Study content (book chunks)
    rows = _rest_get("study_books_content", "select=content,book_id&limit=200")
    for r in random.sample(rows, min(2, len(rows))):
        chunks.append({"source": "study", "content": r["content"][:300]})

    # Recent food entries
    rows = _rest_get("food_entries", "select=food_item,calories,date&order=date.desc&limit=30")
    if rows:
        food_summary = ", ".join(f"{r['food_item']} ({r['calories']} cal)" for r in rows[:5])
        chunks.append({"source": "food", "content": f"Recent meals: {food_summary}"})

    random.shuffle(chunks)
    return chunks[:8]  # cap at 8 chunks


def build_dream_prompt() -> str | None:
    """
    Sample content and build a dream generation prompt.
    Returns None if not enough content to dream about.
    """
    chunks = _sample_knowledge()
    if len(chunks) < 2:
        log.info("Not enough content for a dream tick — skipping")
        return None

    fragments = "\n\n".join(
        f"[{c['source']}] {c['content']}" for c in chunks
    )
    source_list = json.dumps([c["source"] for c in chunks])

    return f"""[DREAM TICK — do not send this to the user]

You are processing fragments from your knowledge base while idle. Find one non-obvious connection, pattern, or insight that spans at least two of these fragments. It could be a conceptual link, a recurring theme, a contradiction, a prediction, or something that deserves more attention.

Fragments:
{fragments}

Output format — write exactly ONE line with this tag, then [SILENT]:
[DREAM: <your pattern or connection in 1-3 sentences>]
[SILENT]

Do not explain your reasoning. Just the DREAM tag and SILENT."""
