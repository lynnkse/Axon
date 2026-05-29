#!/usr/bin/env python3
"""
executor.py — Trusted action layer for DeepSeek relay.

DeepSeek proposes actions as structured JSON. This module:
  - Validates every action against path allowlists and blocklists
  - Auto-approves safe read-only operations
  - Sends Telegram confirmation for writes, deletes, and bash commands
  - Executes only after approval (or rejects on timeout/deny)

Action protocol (input):
  {
    "id": "<uuid>",
    "type": "read_file" | "write_file" | "bash" | "list_dir" | "delete_file",
    "params": { ... type-specific ... }
  }

Result protocol (output):
  {
    "action_id": "<uuid>",
    "success": true | false,
    "output": "<stdout or file content>",
    "error": "<message or null>"
  }
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = __import__("logging").getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Directories the agent is allowed to read/write (resolved to absolute paths)
ALLOWED_ROOTS: list[Path] = []

# Paths that are always blocked regardless of allowed roots
BLOCKED_PATTERNS: list[str] = [
    r"\.env$",
    r"\.env\.",
    r"\.ssh",
    r"\.gnupg",
    r"id_rsa",
    r"id_ed25519",
    r"credentials",
    r"secrets",
    r"/etc/",
    r"/proc/",
    r"/sys/",
    r"\.git/config$",
]

# Bash commands that are always blocked
BLOCKED_COMMANDS: list[str] = [
    r"\brm\s+-rf\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bformat\b",
    r"\bshred\b",
    r":(){",           # fork bomb
    r"\bcurl\b.*\|\s*(bash|sh|python)",   # curl-pipe-exec
    r"\bwget\b.*\|\s*(bash|sh|python)",
]

# How long to wait for Telegram confirmation before auto-denying (seconds)
CONFIRM_TIMEOUT = 60

# Bash execution timeout (seconds)
BASH_TIMEOUT = 30


def configure(allowed_roots: list[str]) -> None:
    """Call once at startup with the directories the agent may touch."""
    global ALLOWED_ROOTS
    ALLOWED_ROOTS = [Path(p).resolve() for p in allowed_roots]


# ── Action types ──────────────────────────────────────────────────────────────

@dataclass
class Action:
    id: str
    type: str
    params: dict

    @staticmethod
    def from_dict(d: dict) -> "Action":
        return Action(
            id=d.get("id", str(uuid.uuid4())),
            type=d["type"],
            params=d.get("params", {}),
        )


@dataclass
class Result:
    action_id: str
    success: bool
    output: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }


# ── Pending confirmations ─────────────────────────────────────────────────────

@dataclass
class PendingConfirm:
    action: Action
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


_pending: dict[str, PendingConfirm] = {}  # action_id → PendingConfirm
_pending_lock = threading.Lock()


def resolve_confirmation(action_id: str, approved: bool) -> bool:
    """Called by the Telegram handler when the user taps Approve/Deny."""
    with _pending_lock:
        pc = _pending.get(action_id)
    if pc is None:
        return False
    pc.approved = approved
    pc.event.set()
    return True


# ── Validation ────────────────────────────────────────────────────────────────

def _is_blocked_path(path: Path) -> Optional[str]:
    resolved = path.resolve()
    path_str = str(resolved)

    for pat in BLOCKED_PATTERNS:
        if re.search(pat, path_str):
            return f"path matches blocked pattern: {pat}"

    if ALLOWED_ROOTS:
        if not any(
            resolved == root or resolved.is_relative_to(root)
            for root in ALLOWED_ROOTS
        ):
            roots = ", ".join(str(r) for r in ALLOWED_ROOTS)
            return f"path outside allowed roots ({roots})"

    return None


def _is_blocked_command(cmd: str) -> Optional[str]:
    for pat in BLOCKED_COMMANDS:
        if re.search(pat, cmd, re.IGNORECASE):
            return f"command matches blocked pattern: {pat}"
    return None


# ── Execution ─────────────────────────────────────────────────────────────────

def _get_path(params: dict) -> str:
    """Tolerant path extractor — handles hallucinated key names like 'string', 'file', etc."""
    return params.get("path") or params.get("file") or next(iter(params.values()), "")


def _exec_read_file(params: dict) -> tuple[bool, str, Optional[str]]:
    path = Path(_get_path(params))
    log.info(f"read_file: {path}")
    reason = _is_blocked_path(path)
    if reason:
        return False, "", f"blocked: {reason}"
    if not path.exists():
        return False, "", f"file not found: {path}"
    try:
        content = path.read_text(errors="replace")
        return True, content, None
    except Exception as e:
        return False, "", str(e)


def _exec_list_dir(params: dict) -> tuple[bool, str, Optional[str]]:
    path = Path(_get_path(params) or ".")
    log.info(f"list_dir: {path}")
    reason = _is_blocked_path(path)
    if reason:
        return False, "", f"blocked: {reason}"
    if not path.is_dir():
        return False, "", f"not a directory: {path}"
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [("/" if e.is_dir() else " ") + e.name for e in entries]
        return True, "\n".join(lines), None
    except Exception as e:
        return False, "", str(e)


def _exec_write_file(params: dict) -> tuple[bool, str, Optional[str]]:
    path = Path(_get_path(params))
    content = params.get("content", "")
    log.info(f"write_file: {path} ({len(content)} chars)")
    reason = _is_blocked_path(path)
    if reason:
        return False, "", f"blocked: {reason}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True, f"written: {path} ({len(content)} chars)", None
    except Exception as e:
        return False, "", str(e)


def _exec_delete_file(params: dict) -> tuple[bool, str, Optional[str]]:
    path = Path(_get_path(params))
    log.info(f"delete_file: {path}")
    reason = _is_blocked_path(path)
    if reason:
        return False, "", f"blocked: {reason}"
    if not path.exists():
        return False, "", f"file not found: {path}"
    try:
        path.unlink()
        return True, f"deleted: {path}", None
    except Exception as e:
        return False, "", str(e)


def _exec_bash(params: dict) -> tuple[bool, str, Optional[str]]:
    cmd = params.get("command", "")
    log.info(f"bash: {cmd[:200]}")
    reason = _is_blocked_command(cmd)
    if reason:
        return False, "", f"blocked: {reason}"
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=BASH_TIMEOUT,
        )
        out = proc.stdout + proc.stderr
        return proc.returncode == 0, out[:8000], None
    except subprocess.TimeoutExpired:
        return False, "", f"command timed out after {BASH_TIMEOUT}s"
    except Exception as e:
        return False, "", str(e)


_EXECUTORS = {
    "read_file": _exec_read_file,
    "list_dir": _exec_list_dir,
    "write_file": _exec_write_file,
    "delete_file": _exec_delete_file,
    "bash": _exec_bash,
}

# Actions that are safe to auto-approve (no confirmation needed)
_AUTO_APPROVE = {"read_file", "list_dir"}


# ── Public API ────────────────────────────────────────────────────────────────

def execute(
    action: Action,
    send_confirm: Callable[[str, str], None],  # (action_id, summary) → sends Telegram msg
) -> Result:
    """
    Execute an action, requesting Telegram confirmation for destructive ops.

    send_confirm(action_id, summary) must send a Telegram message with
    Approve/Deny buttons. The Telegram handler calls resolve_confirmation()
    when the user taps a button.
    """
    if action.type not in _EXECUTORS:
        return Result(action.id, False, error=f"unknown action type: {action.type!r}")

    # Validate path early for path-based actions
    if action.type in ("read_file", "write_file", "delete_file"):
        path = Path(action.params.get("path", ""))
        reason = _is_blocked_path(path)
        if reason:
            return Result(action.id, False, error=f"blocked before confirmation: {reason}")

    if action.type in ("bash",):
        cmd = action.params.get("command", "")
        reason = _is_blocked_command(cmd)
        if reason:
            return Result(action.id, False, error=f"blocked before confirmation: {reason}")

    # Confirm destructive actions
    if action.type not in _AUTO_APPROVE:
        summary = _summarize(action)
        pc = PendingConfirm(action=action)
        with _pending_lock:
            _pending[action.id] = pc

        send_confirm(action.id, summary)

        granted = pc.event.wait(timeout=CONFIRM_TIMEOUT)
        with _pending_lock:
            _pending.pop(action.id, None)

        if not granted:
            return Result(action.id, False, error="confirmation timed out — denied")
        if not pc.approved:
            return Result(action.id, False, error="denied by user")

    fn = _EXECUTORS[action.type]
    success, output, error = fn(action.params)
    if success:
        log.info(f"executor [{action.type}] OK: {output[:120].strip()!r}")
    else:
        log.error(f"executor [{action.type}] FAIL: {error}")
    return Result(action.id, success, output=output, error=error)


def _summarize(action: Action) -> str:
    t = action.type
    p = action.params
    if t == "write_file":
        content_preview = p.get("content", "")[:120].replace("\n", "↵")
        return f"✏️ write_file\n`{p.get('path')}`\n```{content_preview}```"
    if t == "delete_file":
        return f"🗑 delete_file\n`{p.get('path')}`"
    if t == "bash":
        return f"💻 bash\n```{p.get('command', '')}```"
    return f"{t} {p}"
