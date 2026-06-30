"""
SupabaseClient — Fire-and-forget persistence layer for relay v2.

Saves messages and memory entries to Supabase via REST API.
All writes run in a background thread so the relay is never blocked.

Memory tags parsed from Claude responses:
  [REMEMBER: fact]
  [GOAL: goal text | DEADLINE: optional date]
  [DONE: search text for completed goal]
  [INSIGHT: content | PROJECT: name | TYPE: failure_mode | CONFIDENCE: 3]

These tags are stripped from the response text before it reaches Telegram.
"""

import json
import logging
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

import config

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Tag patterns
# ------------------------------------------------------------------

_REMEMBER_RE = re.compile(r'\[REMEMBER:\s*(.+?)\]', re.DOTALL)
_GOAL_RE = re.compile(r'\[GOAL:\s*(.+?)(?:\s*\|\s*DEADLINE:\s*(.+?))?\]', re.DOTALL)
_DONE_RE = re.compile(r'\[DONE:\s*(.+?)\]', re.DOTALL)
_INSIGHT_RE = re.compile(
    r'\[INSIGHT:\s*(.+?)(?:\s*\|\s*PROJECT:\s*([^\|\]]+?))?(?:\s*\|\s*TYPE:\s*([^\|\]]+?))?(?:\s*\|\s*CONFIDENCE:\s*(\d))?\]',
    re.DOTALL,
)
_ALL_TAGS_RE = re.compile(r'\[(REMEMBER|GOAL|DONE|INSIGHT):[^\]]+\]', re.DOTALL)


# ------------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------------

def _rest_insert(table: str, payload: dict) -> bool:
    """Insert one row into a Supabase table via REST API. Returns True on success."""
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return False
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.warning(f"Supabase insert {table} failed {e.code}: {body[:200]}")
        return False
    except Exception as e:
        log.warning(f"Supabase insert {table} error: {e}")
        return False


def _rest_patch(table: str, filters: str, payload: dict) -> bool:
    """PATCH (update) rows matching filters. Never deletes anything."""
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return False
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{filters}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.warning(f"Supabase patch {table} failed {e.code}: {body[:200]}")
        return False
    except Exception as e:
        log.warning(f"Supabase patch {table} error: {e}")
        return False


def _fire(fn, *args):
    """Run fn(*args) in a daemon thread — non-blocking."""
    threading.Thread(target=fn, args=args, daemon=True).start()


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def _mark_done(search_text: str):
    """
    Mark a memory row as completed by setting completed_at.
    Searches for rows whose content contains search_text (case-insensitive).
    Never deletes — only updates. If no match, inserts a completed record.
    """
    import urllib.parse, datetime
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return

    now = datetime.datetime.utcnow().isoformat() + "Z"

    # Try to find and update existing row
    search_encoded = urllib.parse.quote(search_text[:60])
    find_url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/memory"
        f"?content=ilike.*{search_encoded}*&completed_at=is.null&limit=1&select=id"
    )
    req = urllib.request.Request(
        find_url,
        headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"Mark done lookup failed: {e}")
        rows = []

    if rows:
        row_id = rows[0]["id"]
        _rest_patch("memory", f"id=eq.{row_id}", {"completed_at": now})
        log.info(f"Marked done (id={row_id}): {search_text[:60]}")
    else:
        # No matching open task — insert a completed record so it's still tracked
        _rest_insert("memory", {
            "type": "completed_goal",
            "content": f"Completed: {search_text.strip()}",
            "completed_at": now,
            "priority": 0,
            "metadata": {},
        })
        log.info(f"No open task found for '{search_text[:40]}', inserted completed record")


def save_message(role: str, content: str, channel: str = "telegram", metadata: Optional[dict] = None):
    """Save a message to the messages table (non-blocking)."""
    payload = {
        "role": role,
        "content": content,
        "channel": channel,
        "metadata": metadata or {},
    }
    _fire(_rest_insert, "messages", payload)


def _get_or_create_project_id(project_name: str) -> Optional[str]:
    """Look up project by name, create if not exists. Returns UUID string."""
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return None
    # Try to fetch existing
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/projects?name=eq.{urllib.parse.quote(project_name)}&select=id&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
            if rows:
                return rows[0]["id"]
    except Exception as e:
        log.warning(f"Project lookup failed: {e}")
        return None
    # Create new project
    ok = _rest_insert("projects", {"name": project_name, "status": "active"})
    if not ok:
        return None
    # Fetch the new ID
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
            return rows[0]["id"] if rows else None
    except Exception:
        return None


def save_insight(
    content: str,
    project_name: Optional[str] = None,
    type_: str = "pattern",
    confidence: int = 3,
    context: Optional[str] = None,
    source: str = "auto",
):
    """Save a professional insight to the insights table (non-blocking)."""
    def _write():
        project_id = None
        if project_name:
            project_id = _get_or_create_project_id(project_name.strip())
        payload: dict = {
            "type": type_.strip() if type_ else "pattern",
            "content": content.strip(),
            "confidence": max(1, min(5, int(confidence))),
            "source": source,
            "metadata": {},
        }
        if project_id:
            payload["project_id"] = project_id
        if context:
            payload["context"] = context.strip()
        _rest_insert("insights", payload)
    threading.Thread(target=_write, daemon=True).start()


def save_memory(type_: str, content: str, deadline: Optional[str] = None, priority: int = 0):
    """Save a memory entry (fact, goal, preference, completed_goal) — non-blocking."""
    payload: dict = {
        "type": type_,
        "content": content.strip(),
        "priority": priority,
        "metadata": {},
    }
    if deadline:
        payload["deadline"] = deadline.strip()
    _fire(_rest_insert, "memory", payload)


def fetch_memory_context(limit: int = 50) -> str:
    """
    Fetch facts, preferences, and active goals from Supabase.
    Returns a formatted string ready to inject into the system prompt.
    Returns empty string if Supabase is not configured or unreachable.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return ""

    results = []
    for type_filter, label in [("fact", "Facts"), ("preference", "Preferences"), ("goal", "Goals")]:
        url = (
            f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/memory"
            f"?type=eq.{type_filter}&order=created_at.desc&limit={limit}"
            f"&select=content,deadline"
        )
        req = urllib.request.Request(
            url,
            headers={
                "apikey": config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows = json.loads(resp.read().decode())
                if rows:
                    items = []
                    for r in rows:
                        entry = r["content"]
                        if r.get("deadline"):
                            entry += f" (deadline: {r['deadline']})"
                        items.append(f"- {entry}")
                    results.append(f"{label}:\n" + "\n".join(items))
        except Exception as e:
            log.warning(f"Failed to fetch {type_filter} from Supabase: {e}")

    if not results:
        return ""

    return "Long-term memory from past sessions:\n" + "\n\n".join(results)


def fetch_recent_messages(n: int = 20, channel: Optional[str] = None) -> str:
    """
    Fetch the last N messages from the messages table for session resume context.
    Returns a formatted transcript string, oldest first.
    Returns empty string if unavailable.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return ""

    filter_part = f"&channel=eq.{channel}" if channel else ""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/messages"
        f"?select=role,content,created_at{filter_part}"
        f"&order=created_at.desc&limit={n}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch recent messages: {e}")
        return ""

    if not rows:
        return ""

    rows.reverse()  # oldest first
    lines = []
    for r in rows:
        role = r.get("role", "?")
        speaker = "User" if role == "user" else "Assistant"
        content = r.get("content", "").strip()
        if content:
            lines.append(f"{speaker}: {content[:300]}")

    if not lines:
        return ""

    return "Recent conversation (last session, for context):\n" + "\n".join(lines)


def process_response(text: str, channel: str = "telegram") -> str:
    """
    Parse memory tags from Claude's response text, save them to Supabase,
    and return the cleaned text (tags stripped) for delivery to the user.
    """
    facts = _REMEMBER_RE.findall(text)
    goals = _GOAL_RE.findall(text)
    dones = _DONE_RE.findall(text)

    for fact in facts:
        fact = fact.strip()
        if fact:
            log.info(f"Memory: saving fact: {fact[:60]}")
            save_memory("fact", fact)

    for goal_text, deadline in goals:
        goal_text = goal_text.strip()
        deadline = deadline.strip() if deadline else None
        if goal_text:
            log.info(f"Memory: saving goal: {goal_text[:60]}")
            save_memory("goal", goal_text, deadline=deadline)

    for done_text in dones:
        done_text = done_text.strip()
        if done_text:
            log.info(f"Memory: marking done: {done_text[:60]}")
            _fire(_mark_done, done_text)

    insights = _INSIGHT_RE.findall(text)
    for content, project, type_, confidence in insights:
        content = content.strip()
        if content:
            conf = int(confidence) if confidence else 3
            log.info(f"Insight ({type_ or 'pattern'}, project={project or 'cross'}, conf={conf}): {content[:60]}")
            save_insight(
                content=content,
                project_name=project or None,
                type_=type_ or "pattern",
                confidence=conf,
                source="auto",
            )

    # Strip all tags from delivered text
    cleaned = _ALL_TAGS_RE.sub("", text).strip()
    return cleaned
