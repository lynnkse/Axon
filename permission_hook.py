#!/usr/bin/env python3
"""
PermissionRequest hook for Relay v2.

Claude Code calls this script when a tool needs permission.
We forward the request to SessionManagerNode via permission.sock,
wait for the user's decision (from Telegram or CLI), and return it.

Claude Code protocol:
  stdin  — JSON with tool_name, tool_input, session_id, etc.
  stdout — JSON with hookSpecificOutput.decision.behavior = "allow"|"deny"
  exit 0 always (non-zero = error, Claude Code falls back to TUI)

Timeout: retries up to MAX_RETRIES times (ATTEMPT_TIMEOUT seconds each),
re-broadcasting the inline keyboard on every retry. After all attempts
exhausted, denies cleanly (exit 0 with deny) rather than falling to TUI.
If SessionManagerNode is not reachable at all, falls through to TUI (exit 1).
"""

import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

# Must match config.py
PERMISSION_SOCK = "/tmp/cognitive-hq/permission.sock"
ATTEMPT_TIMEOUT = 120  # seconds to wait per attempt
MAX_RETRIES = 3       # deny after this many unanswered attempts
LOG_FILE = "/tmp/permission_hook.log"

# Paths that are never auto-allowed for writes, even within home dir
_SENSITIVE_PATHS = (
    "/.ssh/", "/.aws/", "/.gnupg/", "/.config/", "/etc/",
    "/.netrc", "/.npmrc", "/.pypirc",
)

# Bash patterns that are never auto-allowed regardless of other rules
_DANGEROUS_BASH = re.compile(
    r"\bsudo\b"
    r"|rm\s+-[a-z]*r[a-z]*f\b"          # rm -rf (any path)
    r"|rm\s+--force"
    r"|git\s+reset\s+--hard"
    r"|git\s+push\s+(--force|-f)"
    r"|\bdd\b.+of=/"                    # dd writing to disk
    r"|\bmkfs\b"
    r"|\bfdisk\b|\bparted\b"
    r"|>\s*/dev/sd"                      # writing to block device
    r"|:\(\)\{.*\}.*:"                   # fork bomb
    r"|chmod\s+[0-7]*7[0-7]*\s+/"       # chmod 777 /...
    r"|chown\s+.+\s+/"
    r"|mv\s+.+\s+/dev/null",
    re.IGNORECASE,
)


def _auto_decision(tool_name: str, tool_input: dict) -> str | None:
    """
    Fast pre-check before asking the user.

    Returns "allow" (safe to proceed automatically),
            "deny"  (clearly unsafe, block immediately), or
            None    (ambiguous — ask via Telegram as usual).

    Controlled by CLAUDE_AUTO_ALLOW env var:
      unset / "1" / "true"  → auto-allow logic active (default)
      "0" / "false"         → always ask user (disables auto-allow)
    """
    auto = os.environ.get("CLAUDE_AUTO_ALLOW", "1").lower()
    if auto in ("0", "false", "no", "off"):
        return None

    # ── MCP tools — always allow ──────────────────────────────────────────
    if tool_name.startswith("mcp__"):
        return "allow"

    # ── Always-safe read-only tools ───────────────────────────────────────
    if tool_name in ("Read", "Glob", "Grep", "LS", "NotebookRead"):
        return "allow"

    # ── Web browsing ──────────────────────────────────────────────────────
    if tool_name in ("WebFetch", "WebSearch"):
        return "allow"

    # ── File writes: allow within home dir, block sensitive paths ─────────
    if tool_name in ("Edit", "Write", "NotebookEdit", "Create"):
        file_path = tool_input.get("file_path", "")
        home = str(Path.home())
        if not file_path.startswith(home):
            return None  # outside home — ask
        if any(s in file_path for s in _SENSITIVE_PATHS):
            return None  # sensitive path — ask
        return "allow"

    # ── Bash: block known-dangerous patterns, allow everything else ────────
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # Only scan the first line — heredoc bodies can contain dangerous-
        # looking strings that are just content, not commands being run.
        first_line = command.split("\n")[0]
        if _DANGEROUS_BASH.search(first_line):
            return None  # ask user — don't auto-deny, let them decide
        return "allow"

    # Unknown tool — ask
    return None


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"{ts} {msg}\n"
    sys.stderr.write(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def _allow() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def _deny(message: str = "Denied via Telegram relay.") -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": message},
        }
    }


_RELAY_UNREACHABLE = "unreachable"  # sentinel: relay not running, fall through to TUI


def _try_once(request: dict) -> str | None:
    """
    Open a fresh connection to permission.sock, send the request, and wait
    up to ATTEMPT_TIMEOUT seconds for a decision.

    Returns "allow" or "deny" on success, None on timeout, _RELAY_UNREACHABLE
    if the socket doesn't exist (relay not running — caller should exit 1).
    Each call causes SessionManagerNode to re-broadcast the inline keyboard.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(PERMISSION_SOCK)
        _log("connected to permission.sock")

        payload = (json.dumps(request) + "\n").encode()
        sock.sendall(payload)
        _log("request sent, waiting for decision...")

        sock.settimeout(ATTEMPT_TIMEOUT)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(256)
            if not chunk:
                _log("socket closed by server before decision")
                return None
            buf += chunk

        line = buf.split(b"\n")[0].strip()
        _log(f"received: {line.decode()}")
        response = json.loads(line)
        return response.get("decision", "deny")

    except socket.timeout:
        _log("attempt timed out")
        return None
    except (FileNotFoundError, ConnectionRefusedError) as e:
        _log(f"relay not reachable: {e}")
        return _RELAY_UNREACHABLE
    except Exception as e:
        _log(f"ERROR: {e}")
        return _RELAY_UNREACHABLE
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def main():
    # Only intercept permissions for the relay's own Claude session.
    # Other Claude Code sessions on this machine exit immediately so their
    # normal TUI dialog is shown instead.
    if not os.environ.get("CLAUDE_RELAY_SESSION"):
        sys.exit(1)

    _log("=== permission_hook started ===")

    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
        _log(f"stdin: tool={request.get('tool_name')} input={str(request.get('tool_input',''))[:80]}")
    except Exception as e:
        _log(f"ERROR reading stdin: {e}")
        sys.exit(1)  # fall through to TUI

    tool_name  = request.get("tool_name", "")
    tool_input = request.get("tool_input", {})

    auto = _auto_decision(tool_name, tool_input)
    if auto == "allow":
        _log(f"AUTO-ALLOW: {tool_name}")
        print(json.dumps(_allow()))
        sys.stdout.flush()
        sys.exit(0)
    if auto == "deny":
        _log(f"AUTO-DENY: {tool_name}")
        print(json.dumps(_deny("Auto-denied: unsafe operation.")))
        sys.stdout.flush()
        sys.exit(0)

    _log(f"asking user: {tool_name}")
    for attempt in range(1, MAX_RETRIES + 1):
        _log(f"attempt {attempt}/{MAX_RETRIES}")
        decision = _try_once(request)

        if decision == _RELAY_UNREACHABLE:
            _log("relay not running — falling through to Claude Code TUI")
            sys.exit(1)

        if decision is not None:
            if decision == "allow":
                output = json.dumps(_allow())
            else:
                output = json.dumps(_deny("Denied via Telegram relay."))
            _log(f"stdout: {output}")
            print(output)
            sys.stdout.flush()
            _log("=== permission_hook done (exit 0) ===")
            sys.exit(0)

        _log(f"attempt {attempt} timed out — {'retrying' if attempt < MAX_RETRIES else 'giving up'}")

    # All attempts exhausted: deny cleanly (no TUI fallback)
    _log("All attempts exhausted — defaulting to deny")
    output = json.dumps(_deny("No response after 3 attempts — denied automatically."))
    print(output)
    sys.stdout.flush()
    _log("=== permission_hook done (exit 0 / auto-deny) ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
