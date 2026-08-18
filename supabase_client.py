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
from datetime import datetime
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
_DREAM_RE = re.compile(r'\[DREAM:\s*(.+?)\]', re.DOTALL)
_SKILL_RE = re.compile(
    # name= must look like a real kebab-case identifier, not placeholder text
    # like "<kebab-case-name>" or "..." -- this is what stops the regex from
    # matching when someone quotes the tag syntax as a literal example
    # (e.g. in backticks) instead of actually emitting a real tag.
    r'\[SKILL:\s*name=([a-z0-9][a-z0-9-]*)\s*\|\s*keywords=([^\|]+)\s*\|\s*desc=([^\|]+)\s*\|\s*(.+?)\]',
    re.DOTALL,
)
_ALL_TAGS_RE = re.compile(
    # AROUSAL/CURIOSITY/TENSION added 2026-08-08 — they were emitted per the
    # system-prompt instructions but never stripped, leaking raw tags to Telegram.
    r'\[(REMEMBER|GOAL|DONE|INSIGHT|DREAM|SKILL|VALENCE|MOOD|AROUSAL|CURIOSITY|TENSION|ANTON_STATE):[^\]]+\]',
    re.DOTALL,
)


def strip_response_tags(text: str) -> str:
    """Remove protocol tags without executing any persistence side effects."""
    return _ALL_TAGS_RE.sub("", text).strip()


# ------------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------------

def _rest_insert(table: str, payload: dict) -> bool:
    """Insert one row into a Supabase table via REST API. Returns True on success."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return False
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return False
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{filters}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PATCH", headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal"})
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


def _rest_get(path: str, timeout: int = 10):
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return []
    req = urllib.request.Request(f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Supabase GET {path} failed: {e}")
        return []


def fetch_actor_states() -> list[dict]:
    """Fetch the shared/global actor directory."""
    return _rest_get("actor_state?order=actor_id.asc")


def fetch_prompt_actor_states() -> list[dict] | None:
    """Strict global actor fetch: distinguish a real empty table from failure."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        log.error("Prompt actor fetch unavailable: Supabase is not configured")
        return None
    path = "actor_state?order=actor_id.asc"
    req = urllib.request.Request(f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}", headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:
        log.error("Prompt actor fetch failed: %s", exc, exc_info=True)
        return None
    if not isinstance(rows, list):
        log.error("Prompt actor fetch returned non-list payload: %r", rows)
        return None
    return rows


def _prompt_relevance_exists(path: str) -> bool | None:
    """Strict, cheap existence probe: None means the gate must fail open."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    req = urllib.request.Request(
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}",
        headers={"apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                 "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("Prompt actor relevance probe failed for %s: %s", path, exc)
        return None
    return bool(rows) if isinstance(rows, list) else None


def prompt_actor_relevance_changed(actor_row: dict) -> bool | None:
    """Actor-type-aware dirty probe; add new actor probes to this dispatcher."""
    actor_id = str(actor_row.get("actor_id", ""))
    actor_type = str(actor_row.get("actor_type", ""))
    kind = "fitness-food-coach" if (
        actor_id == "fitness-food-coach" or actor_id.endswith(":fitness-food-coach")
        or actor_type in {"fitness", "fitness-food-coach"}
    ) else actor_type
    if kind not in {"fitness-food-coach", "anton-state-tracker"}:
        return None
    since = actor_row.get("last_advanced_at")
    if not since:
        return True
    encoded_since = urllib.parse.quote(str(since), safe="-:TZ.")
    if kind == "anton-state-tracker":
        probes = (
            f"compulsive_behavior_tracking?select=id&created_at=gt.{encoded_since}&limit=1",
            f"food_entries?select=id&created_at=gt.{encoded_since}&limit=1",
            f"fitness_log?select=date&updated_at=gt.{encoded_since}&limit=1",
        )
    else:
        probes = (
            f"food_entries?select=id&created_at=gt.{encoded_since}&limit=1",
            f"fitness_log?select=date&updated_at=gt.{encoded_since}&limit=1",
        )
    for path in probes:
        changed = _prompt_relevance_exists(path)
        if changed is not False:
            return changed
    return False


def save_prompt_actor_update(actor_row: dict, update) -> bool:
    """Synchronously CAS-save one validated prompt-embedded actor update.

    A false return is always logged by this function and must be treated as a
    visible persistence failure by the caller. Actor history is capped at 50
    lean transition records (status/summary/error only); the current living
    state is stored once at the top level. Only the newest eight history
    entries are injected into prompts.
    """
    actor_id = actor_row.get("actor_id")
    revision = int(actor_row.get("revision", 0))
    old_state = dict(actor_row.get("state") or {})
    history = list(old_state.get("history") or [])[-49:]
    now = datetime.utcnow().isoformat() + "Z"
    history.append({
        "at": now, "revision": revision + 1, "status": update.status,
        "summary": update.summary,
        **({"error_reason": update.error_reason} if update.error_reason else {}),
    })
    # Code is immutable prompt context for this transition. Preserve it from
    # the row and persist only the model's dynamic data update beside it.
    from actor_model.prompt_blocks import CODE_STATE_KEYS
    state = {key: old_state[key] for key in CODE_STATE_KEYS if key in old_state}
    state.update({key: value for key, value in update.state.items()
                  if key not in CODE_STATE_KEYS and key != "history"})
    state["history"] = history
    disposition = {"running": "ready_again", "finished": "completed", "error": "blocked"}[update.status]
    payload = {
        "state": state,
        "directory_projection": {"summary": update.summary, "status": update.status},
        "disposition": disposition,
        "blocked_reason": update.error_reason if update.status == "error" else None,
        "revision": revision + 1,
        "dirty": False,
        "last_advanced_at": now,
        "updated_at": now,
    }
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        log.error("Actor update persistence unavailable for %s: Supabase is not configured", actor_id)
        return False
    filters = (f"actor_id=eq.{urllib.parse.quote(str(actor_id))}"
               f"&revision=eq.{revision}&select=actor_id,revision")
    req = urllib.request.Request(
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/actor_state?{filters}",
        data=json.dumps(payload).encode(), method="PATCH", headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode() or "[]")
    except Exception as exc:
        log.error("Actor update persistence failed for %s at revision %s: %s",
                  actor_id, revision, exc, exc_info=True)
        return False
    if len(rows) != 1 or rows[0].get("revision") != revision + 1:
        log.error("Actor update CAS failed for %s: expected revision %s, response=%r",
                  actor_id, revision, rows)
        return False
    log.info("Prompt actor saved: actor_id=%s revision=%s status=%s",
             actor_id, revision + 1, update.status)
    return True


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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
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
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    # Try to fetch existing
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/projects?name=eq.{urllib.parse.quote(project_name)}&select=id&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
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
                "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
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
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
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


def fetch_recent_summaries(n: int = 5, channel: str = "telegram") -> list:
    """
    Fetch the last N conversation summaries for the given channel, oldest first.
    Returns list of summary strings.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return []
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/summaries"
        f"?select=content,created_at&channel=eq.{channel}"
        f"&order=created_at.desc&limit={n}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch summaries: {e}")
        return []
    rows.reverse()  # oldest first
    return [r["content"] for r in rows if r.get("content")]


def save_summary(channel: str, content: str, message_count: int) -> None:
    """Insert a new summary into the summaries table."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/summaries"
    payload = json.dumps({"channel": channel, "content": content, "message_count": message_count}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log.warning(f"Failed to save summary: {e}")


def get_last_summary_time(channel: str = "telegram") -> Optional[str]:
    """Return the created_at timestamp of the most recent summary, or None."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/summaries"
        f"?select=created_at&channel=eq.{channel}&order=created_at.desc&limit=1"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
            return rows[0]["created_at"] if rows else None
    except Exception as e:
        log.warning(f"Failed to get last summary time: {e}")
        return None


def fetch_messages_since(since_ts: Optional[str], channel: str = "telegram", limit: int = 20) -> list:
    """
    Fetch up to `limit` messages after `since_ts` (ISO timestamp), oldest first.
    If since_ts is None, fetches the oldest `limit` messages in channel.
    Returns list of {"role": str, "content": str} dicts.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return []
    filter_ts = f"&created_at=gt.{since_ts}" if since_ts else ""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/messages"
        f"?select=role,content&channel=eq.{channel}{filter_ts}"
        f"&order=created_at.asc&limit={limit}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch messages since {since_ts}: {e}")
        return []
    return [
        {"role": r["role"], "content": r["content"]}
        for r in rows
        if r.get("role") in ("user", "assistant") and r.get("content", "").strip()
    ]


def fetch_recent_messages_as_turns(n: int = 30, channel: Optional[str] = None) -> list:
    """
    Fetch the last N messages as a list of {"role": str, "content": str} dicts.
    Used to seed DeepSeekBrain.history on startup so conversation continuity
    survives process restarts. No content truncation.
    Returns [] if unavailable.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return []

    filter_part = f"&channel=eq.{channel}" if channel else ""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/messages"
        f"?select=role,content{filter_part}"
        f"&order=created_at.desc&limit={n}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch recent messages as turns: {e}")
        return []

    if not rows:
        return []

    rows.reverse()  # oldest first
    turns = []
    for r in rows:
        role = r.get("role", "").strip()
        content = r.get("content", "").strip()
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns


def _search_edge(query: str, table: str, match_count: int = 5, match_threshold: float = 0.65) -> Optional[list]:
    """
    Call the Supabase search edge function.
    Embeds the query server-side (OpenAI key lives in Supabase) and returns matches.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = f"{config.SUPABASE_URL.rstrip('/')}/functions/v1/search"
    data = json.dumps({
        "query": query[:500],
        "table": table,
        "match_count": match_count,
        "match_threshold": match_threshold,
    }).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Search edge function failed ({table}): {e}")
        return None


def fetch_permanent_rules() -> str:
    """Fetch all active rules marked permanent=true. Always loaded into system prompt."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return ""
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules?active=eq.true&permanent=eq.true&name=is.null&select=content"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read().decode())
            if not rows:
                return ""
            return "[Permanent protocols — always follow these]\n" + "\n\n".join(r["content"] for r in rows)
    except Exception as e:
        log.warning(f"Failed to fetch permanent rules: {e}")
        return ""


def fetch_skills_index() -> str:
    """
    Fetch name + short_description for all active permanent skills.
    Returns a compact index for the system prompt — full content is loaded on demand via fetch_skill_by_name().
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return ""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules"
        f"?active=eq.true&permanent=eq.true&name=not.is.null&select=name,short_description"
    )
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read().decode())
            if not rows:
                return ""
            lines = [f"- {r['name']}: {r.get('short_description', '(no description)')}" for r in rows]
            return (
                "[Available skills — when triggered, query the Supabase rules table WHERE name = '<skill_name>' to load full protocol before acting]\n"
                + "\n".join(lines)
            )
    except Exception as e:
        log.warning(f"Failed to fetch skills index: {e}")
        return ""


def fetch_skill_by_name(name: str) -> str:
    """Fetch full content of a skill by name. Returns the full protocol/algorithm text."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return f"[skill '{name}' unavailable — database not configured]"
    import urllib.parse as _up
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules"
        f"?active=eq.true&name=eq.{_up.quote(name)}&select=name,content&limit=1"
    )
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read().decode())
            if not rows:
                return f"[skill '{name}' not found]"
            return f"[Skill loaded: {rows[0]['name']}]\n\n{rows[0]['content']}"
    except Exception as e:
        log.warning(f"Failed to fetch skill '{name}': {e}")
        return f"[skill '{name}' fetch error: {e}]"


def _fetch_all_rules() -> list:
    """Fetch all active rules (content + keywords) from Supabase."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return []
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules?active=eq.true&select=content,keywords"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch rules: {e}")
        return []


def fetch_relevant_rules(message_text: str) -> str:
    """
    Keyword-match active rules against the incoming message.
    Returns a formatted prefix string for rules that match, or empty string.
    """
    rules = _fetch_all_rules()
    if not rules:
        return ""
    msg_lower = message_text.lower()
    matched = []
    for rule in rules:
        keywords = [kw.strip() for kw in (rule.get("keywords") or "").split(",") if kw.strip()]
        if any(kw in msg_lower for kw in keywords):
            matched.append(rule["content"])
    if not matched:
        return ""
    return "[Rules to follow for this message]\n" + "\n".join(f"- {r}" for r in matched) + "\n\n"


def fetch_relevant_rule_names(message_text: str) -> str:
    """
    Keyword-match active rules against the incoming message.
    Returns only rule names + short descriptions (anchors), not full content.
    Claude fetches full protocol via the rules table when it needs to act.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return ""
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules?active=eq.true&select=name,short_description,keywords,content"
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            rules = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Failed to fetch rules for anchor matching: {e}")
        return ""

    if not rules:
        return ""

    msg_lower = message_text.lower()
    matched = []
    for rule in rules:
        keywords = [kw.strip() for kw in (rule.get("keywords") or "").split(",") if kw.strip()]
        if not any(kw in msg_lower for kw in keywords):
            continue
        name = rule.get("name")
        desc = rule.get("short_description", "")
        if name:
            # Named rule: emit anchor only
            entry = f"- {name}" + (f": {desc}" if desc else "")
        else:
            # Unnamed rule: emit full content (these are typically short hard constraints)
            entry = f"- {rule['content']}"
        matched.append(entry)

    if not matched:
        return ""
    return (
        "[Rules to follow for this message — query the Supabase rules table WHERE name = '<rule_name>' to load full protocol before acting]\n"
        + "\n".join(matched)
        + "\n\n"
    )


def save_rule(content: str):
    """Insert a new rule and trigger embedding via the embed edge function. Non-blocking."""
    def _write():
        ok = _rest_insert("rules", {"content": content.strip()})
        if not ok:
            return
        # Fetch the new row id so we can trigger embedding
        url = (
            f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules"
            f"?content=eq.{urllib.parse.quote(content.strip())}&order=created_at.desc&limit=1&select=id"
        )
        req = urllib.request.Request(url, headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows = json.loads(resp.read().decode())
                if not rows:
                    return
                row_id = rows[0]["id"]
        except Exception as e:
            log.warning(f"Rule fetch after insert failed: {e}")
            return
        # Trigger embed edge function (same format as DB webhook)
        embed_url = f"{config.SUPABASE_URL.rstrip('/')}/functions/v1/embed"
        embed_req = urllib.request.Request(
            embed_url,
            data=json.dumps({"record": {"id": row_id, "content": content.strip()}, "table": "rules"}).encode(),
            method="POST",
            headers={
                "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(embed_req, timeout=15) as r:
                log.info(f"Rule saved and embedded: {content[:60]}")
        except Exception as e:
            log.warning(f"Rule embed failed: {e}")
    threading.Thread(target=_write, daemon=True).start()


def save_skill(name: str, keywords: str, short_description: str, content: str):
    """Insert a new agent-created skill into the rules table. Non-blocking."""
    def _write():
        payload = {
            "name": name.strip(),
            "keywords": keywords.strip(),
            "short_description": short_description.strip()[:120],
            "content": content.strip(),
            "active": True,
            "created_by": "agent",
        }
        ok = _rest_insert("rules", payload)
        if ok:
            log.info(f"Skill saved: {name}")
    threading.Thread(target=_write, daemon=True).start()


def bump_rule_usage(names: list[str]):
    """Increment use_count and set last_used=now for named rules via RPC. Non-blocking."""
    if not names:
        return
    def _write():
        rpc_url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rpc/increment_rule_usage"
        for name in names:
            data = json.dumps({"rule_name": name}).encode()
            req = urllib.request.Request(
                rpc_url, data=data, method="POST",
                headers={
                    "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5):
                    log.debug(f"Bumped rule usage: {name}")
            except Exception as e:
                log.debug(f"bump_rule_usage failed for {name!r}: {e}")
    threading.Thread(target=_write, daemon=True).start()


def fetch_alive_state() -> dict | None:
    """Fetch the single alive_state row. Returns dict or None if unavailable."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = (f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/alive_state"
           f"?instance=eq.{config.INSTANCE}&limit=1")
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read().decode())
            return rows[0] if rows else None
    except Exception as e:
        log.warning(f"fetch_alive_state failed: {e}")
        return None


def save_alive_state(
    tick: int,
    valence: float,
    mood_label: str,
    personality_note: str | None = None,
    arousal: float = 0.0,
    valence_sigma: float = 0.2,
    arousal_sigma: float = 0.2,
    curiosity_focus: str | None = None,
    background_affect: float = 0.0,
    tension: float = 0.0,
) -> None:
    """Upsert the alive_state row. Non-blocking."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return
    payload: dict = {
        "instance": config.INSTANCE,
        "tick": tick,
        "valence": round(valence, 4),
        "mood_label": mood_label,
        "arousal": round(arousal, 4),
        "valence_sigma": round(valence_sigma, 4),
        "arousal_sigma": round(arousal_sigma, 4),
        "background_affect": round(background_affect, 4),
        "tension": round(tension, 4),
        "last_updated": datetime.utcnow().isoformat() + "Z",
    }
    if personality_note is not None:
        payload["personality_note"] = personality_note
    if curiosity_focus is not None:
        payload["curiosity_focus"] = curiosity_focus

    def _write():
        url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/alive_state"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8):
                pass
        except Exception as e:
            log.warning(f"save_alive_state failed: {e}")

    threading.Thread(target=_write, daemon=True).start()


def fetch_anton_model() -> dict | None:
    """Fetch the single anton_model row (persistent filtered estimate of Anton's
    state — affective loop v3, 2026-08-08). Returns dict or None."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = (f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/anton_model"
           f"?instance=eq.{config.INSTANCE}&limit=1")
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read().decode())
            return rows[0] if rows else None
    except Exception as e:
        log.warning(f"fetch_anton_model failed: {e}")
        return None


def save_anton_model(fields: dict) -> None:
    """Upsert this instance's anton_model row. Non-blocking."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return
    payload = {"instance": config.INSTANCE,
               "updated_at": datetime.utcnow().isoformat() + "Z", **fields}

    def _write():
        url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/anton_model"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8):
                pass
        except Exception as e:
            log.warning(f"save_anton_model failed: {e}")

    threading.Thread(target=_write, daemon=True).start()


def save_anton_state(
    tick: int,
    valence: float | None,
    energy: float | None,
    mode: str | None,
    explicit: bool,
    evidence: str | None,
    channel: str | None,
    axon_valence: float,
    axon_arousal: float,
    axon_tension: float,
) -> None:
    """Insert one inferred-Anton-state observation (affective loop, 2026-08-08).

    Axon's own state is snapshotted into the same row so the valence-correlation
    report needs no joins. Non-blocking."""
    payload: dict = {
        "tick": tick,
        "explicit": explicit,
        "axon_valence": round(axon_valence, 4),
        "axon_arousal": round(axon_arousal, 4),
        "axon_tension": round(axon_tension, 4),
    }
    if valence is not None:
        payload["valence"] = round(max(-1.0, min(1.0, valence)), 4)
    if energy is not None:
        payload["energy"] = round(max(-1.0, min(1.0, energy)), 4)
    if mode:
        payload["mode"] = mode.strip()[:80]
    if evidence:
        payload["evidence"] = evidence.strip()[:300]
    if channel:
        payload["channel"] = channel

    def _write():
        if not _rest_insert("anton_state_log", payload):
            log.warning("save_anton_state insert failed")

    threading.Thread(target=_write, daemon=True).start()


def save_dream(content: str, sources: list):
    """Insert a dream into the dreams table and trigger embedding. Non-blocking."""
    def _write():
        ok = _rest_insert("dreams", {"content": content.strip(), "sources": sources})
        if not ok:
            return
        url = (
            f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/dreams"
            f"?order=created_at.desc&limit=1&select=id"
        )
        req = urllib.request.Request(url, headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows = json.loads(resp.read().decode())
                if not rows:
                    return
                row_id = rows[0]["id"]
        except Exception as e:
            log.warning(f"Dream fetch after insert failed: {e}")
            return
        embed_url = f"{config.SUPABASE_URL.rstrip('/')}/functions/v1/embed"
        embed_req = urllib.request.Request(
            embed_url,
            data=json.dumps({"record": {"id": row_id, "content": content.strip()}, "table": "dreams"}).encode(),
            method="POST",
            headers={
                "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(embed_req, timeout=15):
                log.info(f"Dream saved and embedded: {content[:60]}")
        except Exception as e:
            log.warning(f"Dream embed failed: {e}")
    threading.Thread(target=_write, daemon=True).start()


def fetch_relevant_dreams(query: str, match_count: int = 3, match_threshold: float = 0.60) -> str:
    """
    Find dreams semantically similar to the query.
    Returns a formatted context string ready to prepend to a message, or empty string.
    """
    results = _search_edge(query, "dreams", match_count=match_count, match_threshold=match_threshold)
    if not results:
        return ""
    lines = []
    for r in results:
        content = r.get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    if not lines:
        return ""
    return "[Dreams — patterns noticed during idle reflection]\n" + "\n".join(lines)


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

    dreams = _DREAM_RE.findall(text)
    for dream_content in dreams:
        dream_content = dream_content.strip()
        if dream_content:
            log.info(f"Dream captured: {dream_content[:60]}")
            save_dream(dream_content, [])

    skills = _SKILL_RE.findall(text)
    for name, keywords, desc, content in skills:
        name = name.strip()
        if name:
            log.info(f"Skill captured: {name}")
            save_skill(name=name, keywords=keywords, short_description=desc, content=content)

    # Strip all tags from delivered text
    cleaned = _ALL_TAGS_RE.sub("", text).strip()
    return cleaned
