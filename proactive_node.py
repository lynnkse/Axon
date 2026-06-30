#!/usr/bin/env python3
"""
ProactiveNode — Relay v2

Runs on a configurable interval and sends context-aware check-in prompts
to SessionManagerNode. Claude decides whether to reach out or stay silent.

- If Claude responds with [SILENT], the message is discarded.
- Otherwise, TelegramNode forwards it to the user automatically.

Configurable via .env:
  PROACTIVE_INTERVAL=600    # seconds between check-ins (default: 10 min)
  PROACTIVE_ENABLED=1       # set to 0 to disable without stopping the process
"""

import asyncio
import datetime
import json
import logging
import os
import socket as socket_module
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proactive] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

INTERVAL      = int(config.get("PROACTIVE_INTERVAL", "600"))
ENABLED       = config.get("PROACTIVE_ENABLED", "1").lower() not in ("0", "false", "no")
SILENT_TOKEN  = "[SILENT]"
REMINDERS_FILE = Path(config.RELAY_DIR) / "reminders.json"


# ── Supabase task fetch ───────────────────────────────────────────────────────

def _fetch_task_context() -> str:
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return ""

    headers = {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
    }
    parts = []

    today = datetime.date.today().isoformat()
    week  = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()

    # Tasks due within 7 days
    url = (
        f"{config.SUPABASE_URL}/rest/v1/personal_tasks"
        f"?status=in.(pending,in_progress)&due_date=gte.{today}&due_date=lte.{week}"
        f"&order=due_date.asc&limit=10&select=title,due_date,priority,parent_id"
    )
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = [x for x in json.load(r) if not x.get("parent_id")]
        if rows:
            lines = [f"  - {r['title']} (due {r['due_date']}, {r['priority']})" for r in rows]
            parts.append("Upcoming deadlines (7 days):\n" + "\n".join(lines))
    except Exception as e:
        log.warning(f"Task fetch failed: {e}")

    # High-priority tasks with no due date
    url = (
        f"{config.SUPABASE_URL}/rest/v1/personal_tasks"
        f"?status=eq.pending&priority=eq.high&due_date=is.null&parent_id=is.null"
        f"&order=created_at.desc&limit=6&select=title,category"
    )
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.load(r)
        if rows:
            lines = [f"  - {r['title']} ({r['category']})" for r in rows]
            parts.append("High-priority open tasks:\n" + "\n".join(lines))
    except Exception as e:
        log.warning(f"Task fetch failed: {e}")

    return "\n\n".join(parts)


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt() -> str:
    tz  = ZoneInfo(config.USER_TIMEZONE or "UTC")
    now = datetime.datetime.now(tz)
    time_str = now.strftime("%A, %d %B %Y, %H:%M")
    hour = now.hour

    if   6  <= hour < 10: period, hint = "morning",  "Give a brief morning overview if there's something time-sensitive today. Otherwise stay silent."
    elif 10 <= hour < 17: period, hint = "daytime",  "Only speak up if something is genuinely urgent or time-sensitive right now."
    elif 17 <= hour < 21: period, hint = "evening",  "Give a brief evening wrap-up: what was done, what's still open — if useful. Otherwise stay silent."
    else:                  period, hint = "night",    "It's late. Only reach out if something is truly urgent."

    task_ctx = _fetch_task_context()
    name = config.USER_NAME or "the user"

    return f"""[PROACTIVE CHECK-IN — {time_str} — {period}]

{hint}

{task_ctx if task_ctx else "No tasks with upcoming deadlines found."}

Instructions:
- If there is something genuinely worth telling {name} right now, write a short natural message. Be brief and direct.
- If nothing needs attention, respond with exactly: {SILENT_TOKEN}
- Do NOT explain your reasoning. Either message or silence."""


# ── Scheduled one-shot reminders ─────────────────────────────────────────────
#
# reminders.json format: list of {"id": str, "fire_at": "ISO8601", "text": str}
# Claude writes entries via schedule_reminder(); proactive_node fires and removes them.

def _load_reminders() -> list[dict]:
    try:
        if REMINDERS_FILE.exists():
            return json.loads(REMINDERS_FILE.read_text())
    except Exception:
        pass
    return []


def _save_reminders(reminders: list[dict]):
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMINDERS_FILE.write_text(json.dumps(reminders, indent=2))


def schedule_reminder(fire_at: datetime.datetime, text: str, reminder_id: str = "") -> str:
    """Add a one-shot reminder. Returns the reminder ID."""
    import uuid
    rid = reminder_id or str(uuid.uuid4())[:8]
    reminders = _load_reminders()
    reminders.append({
        "id": rid,
        "fire_at": fire_at.isoformat(),
        "text": text,
    })
    _save_reminders(reminders)
    log.info(f"Reminder scheduled: [{rid}] at {fire_at.isoformat()} — {text[:60]}")
    return rid


def _check_and_fire_reminders() -> list[str]:
    """Fire any reminders whose time has passed. Returns list of texts to send."""
    tz = ZoneInfo(config.USER_TIMEZONE or "UTC")
    now = datetime.datetime.now(tz)
    reminders = _load_reminders()
    to_fire = []
    remaining = []
    for r in reminders:
        fire_at = datetime.datetime.fromisoformat(r["fire_at"])
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=tz)
        if now >= fire_at:
            to_fire.append(r["text"])
            log.info(f"Firing reminder [{r['id']}]: {r['text'][:60]}")
        else:
            remaining.append(r)
    if len(to_fire) > 0:
        _save_reminders(remaining)
    return to_fire


# ── Session socket sender ─────────────────────────────────────────────────────

def _send_to_session(prompt: str):
    msg = {"text": prompt, "source": "proactive", "user_id": "proactive"}
    payload = (json.dumps(msg) + "\n").encode()
    try:
        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        sock.connect(config.USER_INPUT_SOCK)
        sock.sendall(payload)
        sock.close()
        log.info("Check-in prompt sent to session_manager")
    except Exception as e:
        log.error(f"Could not reach session_manager: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run():
    if not ENABLED:
        log.info("ProactiveNode disabled (PROACTIVE_ENABLED=0) — exiting")
        return

    log.info(f"ProactiveNode started — interval {INTERVAL}s, tz={config.USER_TIMEZONE}")

    # Stagger first check-in so session_manager has time to fully start
    await asyncio.sleep(30)

    while True:
        try:
            # Fire scheduled reminders directly — no Claude judgment, exact time
            for reminder_text in _check_and_fire_reminders():
                _send_to_session(reminder_text)
                await asyncio.sleep(2)  # brief gap between multiple reminders

            # Regular context-aware check-in
            prompt = _build_prompt()
            _send_to_session(prompt)
        except Exception as e:
            log.error(f"Check-in error: {e}")

        await asyncio.sleep(INTERVAL)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
