#!/usr/bin/env python3
"""
RalphNode — Autonomous execution loop for Axon. Phase 1.

Passive by default. Activated explicitly via:
  python ralph_node.py trigger --task-id <uuid>
  python ralph_node.py trigger --task "description"

Or by a future /ralph command from telegram_node.

Architecture:
  - Daemon process listens on ralph.sock for trigger messages
  - On trigger: loads task from Supabase personal_tasks
  - Sends structured prompts to session_manager via user_input.sock
  - Reads responses from claude_response.sock (filtered by source="ralph")
  - Claude must end each response with: RALPH:DONE / RALPH:CONTINUE / RALPH:STUCK:<reason>
  - Loops up to MAX_ITERATIONS, then stops with a stuck report
  - Sends status notifications directly via Telegram Bot API
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ralph] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ralph")

RALPH_SOCK = f"{config.SOCKET_DIR}/ralph.sock"
MAX_ITERATIONS = 8
ITERATION_TIMEOUT = 600  # seconds per iteration
CHECKPOINT_TIMEOUT = 3600  # 1h for human to reply at a checkpoint

# ── Verdict dataclass ─────────────────────────────────────────────────────────

@dataclass
class Verdict:
    kind: str        # DONE | CONTINUE | STUCK | CHECKPOINT
    reason: str = ""
    summary: str = ""  # for CHECKPOINT: what was done this step
    plan: str = ""     # for CHECKPOINT: what next step will be

# ── Prompt templates ──────────────────────────────────────────────────────────

_PROMPT_AUTONOMOUS = """\
[RALPH LOOP — ITERATION {n}/{max}]
Task: {title}{notes_block}{prev_block}

Execute the next concrete step toward completing this task. Use your tools.
Think, act, verify.

End your response with exactly one verdict on its own line:
  RALPH:DONE        — task is fully complete, nothing left to do
  RALPH:CONTINUE    — making progress, next iteration needed
  RALPH:STUCK:<why> — blocked, cannot proceed without human input
"""

_PROMPT_SUPERVISED = """\
[RALPH LOOP — ITERATION {n}/{max} — SUPERVISED]
Task: {title}{notes_block}{prev_block}

Execute ONE concrete step toward completing this task. Use your tools.
Think, act, verify. Do not proceed to the next step — stop after one step.

After completing the step, end your response with EXACTLY these two lines \
(fill in the blanks):
  RALPH:CHECKPOINT: <one sentence — what was accomplished this iteration>
  RALPH:PLAN: <one sentence — what the next iteration will do>

Or if the task is complete:
  RALPH:DONE

Or if blocked:
  RALPH:STUCK:<reason>
"""


def _build_prompt(title: str, notes: str, n: int, prev: str, supervised: bool = False) -> str:
    notes_block = f"\nNotes:\n{notes.strip()}" if notes and notes.strip() else ""
    prev_block = f"\nPrevious result (summary):\n{prev.strip()}" if prev and prev.strip() else ""
    template = _PROMPT_SUPERVISED if supervised else _PROMPT_AUTONOMOUS
    return template.format(
        n=n,
        max=MAX_ITERATIONS,
        title=title,
        notes_block=notes_block,
        prev_block=prev_block,
    )


# ── Telegram notification (direct Bot API, no session_manager) ────────────────

def _tg_notify(text: str):
    token = config.get("TELEGRAM_BOT_TOKEN")
    user_id = config.get("TELEGRAM_USER_ID")
    if not token or not user_id:
        log.info(f"[NOTIFY] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": user_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_get(table: str, filters: str) -> list[dict]:
    """Fetch rows from Supabase REST API. Returns list of dicts."""
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return []
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{filters}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Supabase GET {table} error: {e}")
        return []


def _sb_patch(table: str, filters: str, payload: dict) -> bool:
    """PATCH rows in Supabase."""
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return False
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{filters}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
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
    except Exception as e:
        log.warning(f"Supabase PATCH {table} error: {e}")
        return False


def _load_task(task_id: Optional[str] = None) -> Optional[dict]:
    if not task_id:
        return None
    rows = _sb_get("personal_tasks", f"id=eq.{urllib.parse.quote(task_id)}&limit=1")
    return rows[0] if rows else None


def _mark_task(task_id: Optional[str], status: str):
    if task_id:
        _sb_patch("personal_tasks", f"id=eq.{urllib.parse.quote(task_id)}", {"status": status})


# ── Session manager I/O ───────────────────────────────────────────────────────

def _send_to_session(text: str) -> bool:
    """Write a message to user_input.sock (source=ralph)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(config.USER_INPUT_SOCK)
        msg = json.dumps({"text": text, "source": "ralph", "user_id": "ralph"})
        sock.sendall((msg + "\n").encode())
        sock.close()
        return True
    except Exception as e:
        log.error(f"send_to_session failed: {e}")
        return False


def _read_ralph_response(timeout: float = ITERATION_TIMEOUT) -> Optional[str]:
    """
    Subscribe to claude_response.sock and return the first response
    where source == "ralph". Blocks up to `timeout` seconds.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(config.CLAUDE_RESPONSE_SOCK)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(remaining, 5.0))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("source") == "ralph":
                        sock.close()
                        return msg.get("text", "")
                except json.JSONDecodeError:
                    continue
        sock.close()
    except Exception as e:
        log.error(f"read_ralph_response error: {e}")
    return None


# ── Verdict parsing ───────────────────────────────────────────────────────────

def _parse_verdict(response: str) -> Verdict:
    """Scan the response from bottom up for a RALPH verdict line."""
    lines = response.splitlines()
    for i, line in enumerate(reversed(lines)):
        stripped = line.strip()
        if stripped == "RALPH:DONE":
            return Verdict(kind="DONE")
        if stripped == "RALPH:CONTINUE":
            return Verdict(kind="CONTINUE")
        if stripped.startswith("RALPH:STUCK:"):
            return Verdict(kind="STUCK", reason=stripped[len("RALPH:STUCK:"):].strip())
        if stripped == "RALPH:STUCK":
            return Verdict(kind="STUCK", reason="(no reason given)")
        if stripped.startswith("RALPH:CHECKPOINT:"):
            summary = stripped[len("RALPH:CHECKPOINT:"):].strip()
            # look for RALPH:PLAN: on the immediately following scanned line
            # (which is the line before this one in the original doc)
            plan = ""
            rev_idx = i  # how many lines back from end we are
            fwd_idx = len(lines) - 1 - rev_idx  # index in original lines[]
            if fwd_idx + 1 < len(lines):
                next_line = lines[fwd_idx + 1].strip()
                if next_line.startswith("RALPH:PLAN:"):
                    plan = next_line[len("RALPH:PLAN:"):].strip()
            return Verdict(kind="CHECKPOINT", summary=summary, plan=plan)
    return Verdict(kind="CONTINUE")  # no verdict found — keep looping


# ── RALPH Loop executor ───────────────────────────────────────────────────────

def run_loop(task: dict, supervised: bool = False,
             checkpoint_event: Optional[threading.Event] = None):
    """
    Execute the RALPH loop for a task.
    Runs in its own thread. Sends Telegram notifications on key events.

    supervised=True: pause after each step and wait for human "ok" before continuing.
    checkpoint_event: threading.Event set externally when user approves continuation.
    """
    task_id = task.get("id")
    title = task.get("title", "(no title)")
    notes = task.get("notes") or ""
    prev_summary = ""
    mode_tag = "SUPERVISED" if supervised else "AUTO"

    print(f"\n{'═'*60}")
    print(f"  RALPH LOOP START  [{mode_tag}]")
    print(f"  Task: {title}")
    print(f"{'═'*60}")
    log.info(f"RALPH loop start [{mode_tag}]: {title!r} (id={task_id})")
    _tg_notify(
        f"🔁 *RALPH starting* `[{mode_tag}]`\n"
        f"Task: {title}"
        + ("\n_Reply `ok` after each step to continue._" if supervised else "")
    )

    if task_id:
        _mark_task(task_id, "in_progress")

    for n in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'─'*60}")
        print(f"  RALPH  iteration {n}/{MAX_ITERATIONS}")
        print(f"{'─'*60}")
        log.info(f"Iteration {n}/{MAX_ITERATIONS}")

        prompt = _build_prompt(title, notes, n, prev_summary, supervised=supervised)

        # Subscribe BEFORE sending to avoid missing the response
        response_q: queue.Queue[Optional[str]] = queue.Queue()

        def _listen(q=response_q):
            q.put(_read_ralph_response(timeout=ITERATION_TIMEOUT))

        listener = threading.Thread(target=_listen, daemon=True)
        listener.start()

        time.sleep(0.2)

        print(f"  → sending task to Claude (session_manager)...")
        if not _send_to_session(prompt):
            print(f"  ✗ could not reach session_manager")
            _tg_notify(f"❌ RALPH: failed to reach session manager on iteration {n}")
            return

        print(f"  ↻ waiting for Claude's response...")
        try:
            response = response_q.get(timeout=ITERATION_TIMEOUT + 30)
        except queue.Empty:
            response = None

        if not response:
            print(f"  ✗ no response received (timeout)")
            _tg_notify(f"⏱ RALPH: no response in iteration {n} — stopping")
            log.warning("No response received")
            return

        preview = response[:300].replace('\n', ' ')
        print(f"  ← Claude responded: {preview}{'...' if len(response) > 300 else ''}")

        prev_summary = response[-600:] if len(response) > 600 else response

        v = _parse_verdict(response)
        print(f"\n  evaluating verdict...")
        log.info(f"Verdict: {v.kind!r} reason={v.reason!r}")

        if v.kind == "DONE":
            print(f"  ✓ verdict: DONE — task complete after {n} iteration(s)")
            _tg_notify(f"✅ *RALPH: DONE* after {n} iteration(s)\nTask: {title}")
            if task_id:
                _mark_task(task_id, "done")
            return

        if v.kind == "STUCK":
            print(f"  ✗ verdict: STUCK — {v.reason}")
            _tg_notify(
                f"🚧 *RALPH: STUCK* after {n} iteration(s)\n"
                f"Task: {title}\n"
                f"Reason: {v.reason}\n"
                f"Waiting for human input."
            )
            return

        if v.kind == "CHECKPOINT" or (supervised and v.kind == "CONTINUE"):
            # Pause and ask human to approve next step
            summary_text = v.summary or "(step complete)"
            plan_text = v.plan or "(continue working on task)"
            _tg_notify(
                f"🔵 *RALPH checkpoint* {n}/{MAX_ITERATIONS}\n"
                f"*Done:* {summary_text}\n"
                f"*Next:* {plan_text}\n\n"
                f"Reply `ok` to continue, or `ralph stop` to abort."
            )
            log.info(f"Checkpoint {n}: waiting for human approval...")
            if checkpoint_event is None:
                log.warning("No checkpoint_event — aborting (supervised mode needs RalphNode daemon)")
                return
            checkpoint_event.clear()
            approved = checkpoint_event.wait(timeout=CHECKPOINT_TIMEOUT)
            if not approved:
                _tg_notify(
                    f"⏱ *RALPH: checkpoint timed out* after {CHECKPOINT_TIMEOUT//3600}h\n"
                    f"Task: {title}\nRe-trigger when ready."
                )
                return
            log.info("Checkpoint approved — continuing")
            time.sleep(0.5)
            continue

        # CONTINUE (autonomous mode) — next iteration
        print(f"  → verdict: CONTINUE — moving to iteration {n+1}")
        time.sleep(1)

    _tg_notify(
        f"⚠️ *RALPH: max iterations reached* ({MAX_ITERATIONS})\n"
        f"Task: {title}\n"
        f"Review progress and re-trigger if needed."
    )
    log.warning(f"Max iterations ({MAX_ITERATIONS}) reached without DONE")


# ── Daemon node ───────────────────────────────────────────────────────────────

class RalphNode:
    """Listens on ralph.sock for trigger commands. Passive until triggered."""

    def __init__(self):
        self._running = True
        self._active: Optional[threading.Thread] = None
        self._checkpoint_event = threading.Event()
        self._supervised = False

    def _handle(self, msg: dict):
        action = msg.get("action")

        if action == "start":
            if self._active and self._active.is_alive():
                _tg_notify("⚠️ RALPH loop already running — wait for it to finish.")
                return

            task_id = msg.get("task_id")
            task_desc = msg.get("task")
            supervised = bool(msg.get("supervised", False))
            self._supervised = supervised

            if task_id:
                task = _load_task(task_id)
                if not task:
                    _tg_notify(f"❌ RALPH: task {task_id!r} not found in personal_tasks")
                    return
            elif task_desc:
                task = {"id": None, "title": task_desc, "notes": "", "status": "pending"}
            else:
                _tg_notify("❌ RALPH: provide task_id or task description")
                return

            self._checkpoint_event.clear()
            self._active = threading.Thread(
                target=run_loop,
                kwargs={"task": task, "supervised": supervised,
                        "checkpoint_event": self._checkpoint_event},
                daemon=True, name="ralph-loop",
            )
            self._active.start()

        elif action == "continue":
            if self._active and self._active.is_alive():
                log.info("Checkpoint approved via 'continue' command")
                self._checkpoint_event.set()
            else:
                _tg_notify("⚠️ RALPH: no active loop to continue.")

        elif action == "status":
            if self._active and self._active.is_alive():
                mode = "SUPERVISED" if self._supervised else "AUTO"
                _tg_notify(f"🔄 RALPH loop is running `[{mode}]`.")
            else:
                _tg_notify("💤 RALPH is idle.")

        elif action == "stop":
            if self._active and self._active.is_alive():
                # Unblock any waiting checkpoint so the loop can exit cleanly
                self._checkpoint_event.set()
            log.info("Stop command received — shutting down")
            self._running = False

    def _handle_conn(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except Exception:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._handle(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning(f"Bad message: {e}")

    def run(self):
        os.makedirs(config.SOCKET_DIR, exist_ok=True)
        if os.path.exists(RALPH_SOCK):
            os.unlink(RALPH_SOCK)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(RALPH_SOCK)
        server.listen(5)
        log.info(f"RalphNode listening on {RALPH_SOCK}")

        while self._running:
            try:
                server.settimeout(1.0)
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._handle_conn, args=(conn,), daemon=True
                ).start()
            except Exception as e:
                if self._running:
                    log.error(f"Accept error: {e}")
                break

        try:
            os.unlink(RALPH_SOCK)
        except Exception:
            pass
        log.info("RalphNode stopped")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _send_to_ralph(msg: dict):
    """Send a command to a running RalphNode via ralph.sock."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(RALPH_SOCK)
        sock.sendall((json.dumps(msg) + "\n").encode())
        sock.close()
    except FileNotFoundError:
        print("ralph.sock not found — is RalphNode running? Start with: python ralph_node.py serve")
        sys.exit(1)
    except Exception as e:
        print(f"Failed to connect to ralph.sock: {e}")
        sys.exit(1)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="RalphNode — autonomous execution loop")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="Run as daemon (default if no command given)")

    t = sub.add_parser("trigger", help="Trigger a RALPH loop")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--task-id", metavar="UUID", help="personal_tasks row ID")
    g.add_argument("--task", metavar="TEXT", help="Ad-hoc task description")
    t.add_argument("--supervised", action="store_true",
                   help="Pause after each step and wait for human 'ok'")

    sub.add_parser("continue", help="Approve the current checkpoint (supervised mode)")
    sub.add_parser("status", help="Check if a loop is running")
    sub.add_parser("stop", help="Shut down the RalphNode daemon")

    args = parser.parse_args()
    cmd = args.cmd or "serve"

    if cmd == "serve":
        RalphNode().run()
    elif cmd == "trigger":
        msg: dict = {"action": "start"}
        if getattr(args, "task_id", None):
            msg["task_id"] = args.task_id
        else:
            msg["task"] = args.task
        if getattr(args, "supervised", False):
            msg["supervised"] = True
        _send_to_ralph(msg)
        print(f"RALPH triggered: {msg}")
    elif cmd == "continue":
        _send_to_ralph({"action": "continue"})
        print("Continue sent — RALPH will proceed to the next step.")
    elif cmd == "status":
        _send_to_ralph({"action": "status"})
        print("Status request sent — check Telegram.")
    elif cmd == "stop":
        _send_to_ralph({"action": "stop"})
        print("Stop command sent.")


if __name__ == "__main__":
    main()
