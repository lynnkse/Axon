#!/usr/bin/env python3
from __future__ import annotations
"""
SessionManagerNode — Relay v2

Owns the persistent Claude CLI process. Receives messages from all frontends
via sockets, serializes them into Claude's stdin, and publishes responses.

Sockets:
  user_input.sock   — NDJSON in:  {text, source, user_id, media_path?}
  cli_input.sock    — raw bytes in: keyboard input from CLINode
  display.sock      — raw bytes out: PTY output to CLINode
  claude_response.sock — NDJSON out: {text, source, user_id}

State machine (queue processor):
  IDLE:       keyboard bytes flow freely to Claude's PTY.
              Dequeues next item when available → GENERATING.
  GENERATING: keyboard bytes buffered (not forwarded).
              Polls session JSONL for new assistant entry → publishes → IDLE.

Response detection strategy:
  Claude's interactive TUI does NOT echo all response text to the PTY (it
  suppresses control tokens like sentinels). Instead, we watch the session
  JSONL file which always contains the complete, clean response text once
  a turn is finished. No sentinel needed.
"""

import os
import sys
import pty
import fcntl
import termios
import struct
import socket
import threading
import queue
import re
import signal as signal_module
import json
import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
import supabase_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [session_manager] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# How long to wait for Claude's response before giving up (seconds)
# Needs to be long enough for multi-tool runs with several permission prompts
_RESPONSE_TIMEOUT = 600
# How often to poll the session JSONL (seconds)
_POLL_INTERVAL = 0.5
# If file has stopped growing for this long with no "text" entry, return
# whatever text we have (catches cases where Claude ends on a tool_use)
_STALL_FALLBACK = 30.0


@dataclass
class QueueItem:
    text: str
    source: str       # "telegram" | "proactive"
    user_id: str
    media_path: Optional[str] = None


class SessionManagerNode:

    def __init__(self):
        self.input_queue: queue.Queue[Optional[QueueItem]] = queue.Queue()
        self.state = "IDLE"           # IDLE | GENERATING
        self.current_item: Optional[QueueItem] = None

        # Keyboard bytes buffered while GENERATING
        self.keyboard_buffer: list[bytes] = []
        self.state_lock = threading.Lock()

        # Ring buffer of recent PTY output for TUI prompt detection (~4KB)
        self._pty_output_buf: deque = deque(maxlen=4096)
        self._last_tui_prompt_hash: Optional[int] = None

        self.master_fd: Optional[int] = None
        self.claude_proc: Optional[subprocess.Popen] = None
        self.pty_lock = threading.Lock()

        # CLINode display connection (one at a time)
        self.display_client: Optional[socket.socket] = None
        self.display_lock = threading.Lock()

        # Subscribers to claude_response.sock (RouterNode, etc.)
        self.response_subscribers: list[socket.socket] = []
        self.response_subs_lock = threading.Lock()

        # Tracked after Claude spawns so JSONL watcher knows where to look
        self.current_session_id: Optional[str] = None
        # Epoch time of most recent Claude spawn — used to filter session files
        self._spawn_time: float = 0.0

        # Permission request state
        # When a PermissionRequest hook connects, we hold its connection here
        # until a decision arrives (from Telegram or CLI).
        self._permission_conn: Optional[socket.socket] = None
        self._permission_lock = threading.Lock()

        self._running = True
        self._reader_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Config / system prompt
    # ------------------------------------------------------------------

    def _load_profile(self) -> str:
        try:
            return config.PROFILE_PATH.read_text()
        except Exception:
            return ""

    def _build_system_prompt(self) -> str:
        profile = self._load_profile()
        parts = []
        if config.USER_NAME:
            parts.append(f"You are speaking with {config.USER_NAME}.")
        if config.USER_TIMEZONE:
            parts.append(f"User timezone: {config.USER_TIMEZONE}")
        if profile:
            parts.append(f"\nProfile:\n{profile}")
        if not config.SKIP_MEMORY_FETCH:
            memory_context = supabase_client.fetch_memory_context()
            if memory_context:
                parts.append(f"\n{memory_context}")
            recent_msgs = supabase_client.fetch_recent_messages(n=20)
            if recent_msgs:
                parts.append(f"\n{recent_msgs}")
            parts.append(
                "\nMEMORY MANAGEMENT: When the user shares something worth remembering, "
                "include these tags in your response (processed automatically, hidden from user):\n"
                "[REMEMBER: fact to store]\n"
                "[GOAL: goal text | DEADLINE: optional date]\n"
                "[DONE: search text for completed goal]\n"
                "[INSIGHT: content | PROJECT: project_name | TYPE: architecture|failure_mode|performance|stability|design|procedure|warning|pattern | CONFIDENCE: 1-5]\n"
                "Use INSIGHT for professional/technical observations: system architecture patterns, failure modes, "
                "performance characteristics, mathematical stability edge cases, design tradeoffs. "
                "PROJECT is optional (omit for cross-project insights). CONFIDENCE: 1=hypothesis, 3=observed, 5=battle-tested."
            )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Session ID tracking
    # ------------------------------------------------------------------

    def _get_saved_session_id(self) -> Optional[str]:
        try:
            return Path(config.SESSION_ID_FILE).read_text().strip() or None
        except Exception:
            return None

    def _find_newest_session(self, not_before: float) -> Optional[str]:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        sessions_dir = Path.home() / ".claude" / "projects" / project_name
        files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            if f.stat().st_mtime > not_before:
                return f.stem
        return None

    def _save_session_id(self, session_id: str):
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.SESSION_ID_FILE).write_text(session_id)
        log.info(f"Session ID saved: {session_id[:8]}...")

    def _capture_new_session_id(self, spawn_time: float):
        deadline = spawn_time + 30
        while time.time() < deadline:
            time.sleep(2)
            session_id = self._find_newest_session(not_before=spawn_time)
            if session_id:
                self._save_session_id(session_id)
                self.current_session_id = session_id
                log.info(f"New session captured: {session_id[:8]}...")
                return
        log.warning("Could not capture new session ID within 30s")

    def _get_session_file_path(self, session_id: str) -> Path:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        sessions_dir = Path.home() / ".claude" / "projects" / project_name
        return sessions_dir / f"{session_id}.jsonl"

    # ------------------------------------------------------------------
    # Claude process lifecycle
    # ------------------------------------------------------------------

    def _spawn_claude(self):
        cmd = [config.CLAUDE_PATH]

        existing_session = self._get_saved_session_id()
        self._spawn_time = time.time()
        if existing_session:
            cmd += ["--resume", existing_session]
            self.current_session_id = existing_session
            log.info(f"Resuming session: {existing_session[:8]}...")
        else:
            log.info("Starting new session (ID captured on first response)")

        system_prompt = self._build_system_prompt()
        if system_prompt.strip():
            cmd += ["--append-system-prompt", system_prompt]

        rows, cols = 24, 80
        master_fd, slave_fd = pty.openpty()
        self._set_pty_size(master_fd, rows, cols)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_RELAY_SESSION"] = "1"

        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=config.PROJECT_DIR,
            env=env,
        )
        os.close(slave_fd)

        with self.pty_lock:
            self.master_fd = master_fd
            self.claude_proc = proc

        log.info(f"Claude spawned (PID: {proc.pid})")

    def _set_pty_size(self, fd: int, rows: int, cols: int):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass
        if self.claude_proc and self.claude_proc.poll() is None:
            try:
                os.kill(self.claude_proc.pid, signal_module.SIGWINCH)
            except Exception:
                pass

    def _handle_claude_exit(self):
        log.warning("Claude process exited — restarting in 2s...")
        time.sleep(2)
        if self._running:
            self.current_session_id = None  # will be re-captured or resumed
            self._spawn_claude()
            self._reader_thread = threading.Thread(
                target=self._pty_reader_thread, daemon=True
            )
            self._reader_thread.start()

    # ------------------------------------------------------------------
    # PTY reader thread — display only, no response detection
    # ------------------------------------------------------------------

    def _pty_reader_thread(self):
        """
        Reads Claude's PTY output and forwards raw bytes to CLINode display.
        Response detection is done via JSONL polling, not PTY scanning.
        """
        while self._running:
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._forward_display(chunk)

        log.warning("PTY reader exiting")
        self._handle_claude_exit()

    # ANSI escape code pattern for stripping
    _ANSI_RE = re.compile(rb'\x1b(?:\[[0-9;]*[a-zA-Z]|\][^\x07]*\x07|[^[a-zA-Z])')

    def _forward_display(self, data: bytes):
        with self.display_lock:
            if self.display_client:
                try:
                    self.display_client.sendall(data)
                except Exception:
                    self.display_client = None

        # Feed into PTY output buffer for TUI prompt detection
        for b in data:
            self._pty_output_buf.append(b)
        self._detect_tui_prompt()

    def _detect_tui_prompt(self):
        """
        Scan recent PTY output for Claude TUI choice prompts like:
          1. Yes
          2. Yes, allow...
          3. No
        When detected, publish a tui_prompt event to response subscribers
        so TelegramNode can show inline buttons.
        """
        raw = bytes(self._pty_output_buf)
        clean = self._ANSI_RE.sub(b'', raw)
        text = clean.decode('utf-8', errors='replace')

        # Look for 2+ consecutive numbered choices
        lines = text.splitlines()
        choices = []
        for line in lines[-20:]:  # only scan last 20 lines
            stripped = line.strip()
            m = re.match(r'^(\d+)\.\s+(.+)', stripped)
            if m:
                choices.append((int(m.group(1)), m.group(2).strip()))
            elif choices:
                # Non-choice line after choices started — stop collecting
                break

        if len(choices) < 2:
            return

        # Find the question (last non-choice, non-empty line before choices)
        choice_nums = {c[0] for c in choices}
        question = ""
        for line in reversed(lines[-30:]):
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r'^\d+\.', stripped):
                continue
            if "Esc to cancel" in stripped or "Tab to amend" in stripped:
                continue
            question = stripped
            break

        prompt_hash = hash(tuple(choices))
        if prompt_hash == self._last_tui_prompt_hash:
            return
        self._last_tui_prompt_hash = prompt_hash

        log.info(f"TUI prompt detected: {question!r} choices={choices}")
        self._publish_tui_prompt(question, choices)

    def _publish_tui_prompt(self, question: str, choices: list):
        payload = json.dumps({
            "type": "tui_prompt",
            "question": question,
            "choices": [{"num": n, "text": t} for n, t in choices],
        }) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    def _publish_activity(self, growing: bool):
        payload = json.dumps({"type": "activity", "growing": growing}) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    # ------------------------------------------------------------------
    # JSONL response detection
    # ------------------------------------------------------------------

    def _get_jsonl_state(self, session_file: Path, offset: int) -> tuple[Optional[str], Optional[str]]:
        """
        Scan assistant entries from `offset`.
        Returns (combined_text, last_assistant_type) where:
          combined_text        — all assistant text blocks joined with double newline
          last_assistant_type  — content type of the very last assistant entry
                                 ("text", "tool_use", "thinking", …)

        Accumulates ALL text blocks across entries so multi-step responses
        (text → tool_use → text) are not truncated to just the final block.
        """
        text_blocks: list[str] = []
        last_assistant_type: Optional[str] = None
        try:
            if not session_file.exists():
                return None, None
            with open(session_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        msg = obj.get("message", {})
                        if msg.get("role") != "assistant":
                            continue
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                ctype = c.get("type", "")
                                if ctype:
                                    last_assistant_type = ctype
                                if ctype == "text":
                                    text = c.get("text", "").strip()
                                    if text:
                                        text_blocks.append(text)
                        else:
                            text = str(content).strip()
                            if text:
                                text_blocks.append(text)
                                last_assistant_type = "text"
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except Exception:
            pass
        combined = "\n\n".join(text_blocks) if text_blocks else None
        return combined, last_assistant_type

    def _sessions_dir(self) -> Path:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        return Path.home() / ".claude" / "projects" / project_name

    def _wait_for_jsonl_response(
        self,
        session_file: Optional[Path],
        initial_size: int,
    ) -> str:
        """
        Poll for a complete assistant text entry using debounce.

        Claude writes content items as separate JSONL lines (text, tool_use,
        thinking each get their own entry).  We want the LAST text entry once
        the response is fully done.  Strategy:
          - Track file size; on each growth record time + fetch last text entry.
          - Return when file hasn't grown for DEBOUNCE seconds (response done).

        If session_file is None (new session), scan all files newer than
        _spawn_time — Claude creates the JSONL only on the first exchange.
        """
        # Silence after the LAST assistant entry signals response complete —
        # but ONLY when that last entry is "text", not "tool_use".  During tool
        # execution the file stops growing while Claude Code runs the tool;
        # we must not mistake that gap for the end of the response.
        _DEBOUNCE = 1.5  # seconds of silence after last "text" entry → done

        deadline = time.time() + _RESPONSE_TIMEOUT
        _partial_text: Optional[str] = None  # best text seen so far, for timeout fallback

        if session_file is not None:
            # Known session — poll the specific file.
            last_text: Optional[str] = None
            last_assistant_type: Optional[str] = None
            last_file_size = initial_size
            last_activity_time: float = 0.0
            activity_seen = False

            while time.time() < deadline and self._running:
                time.sleep(_POLL_INTERVAL)
                try:
                    current_size = session_file.stat().st_size if session_file.exists() else initial_size
                except Exception:
                    current_size = initial_size

                if current_size > last_file_size:
                    activity_seen = True
                    last_file_size = current_size
                    last_activity_time = time.time()
                    self._publish_activity(growing=True)
                    text, atype = self._get_jsonl_state(session_file, initial_size)
                    if text:
                        last_text = text
                        _partial_text = text
                    if atype:
                        last_assistant_type = atype

                elapsed = time.time() - last_activity_time
                # Primary: last entry is "text" and file has been quiet for DEBOUNCE.
                if (
                    activity_seen
                    and last_text
                    and last_assistant_type == "text"
                    and elapsed >= _DEBOUNCE
                ):
                    log.info(f"Response complete ({len(last_text)} chars)")
                    return last_text
                # Fallback: file stalled for a long time without a final text entry.
                # Return whatever text we have so Telegram isn't left silent.
                if (
                    activity_seen
                    and last_text
                    and elapsed >= _STALL_FALLBACK
                ):
                    log.warning(
                        f"Response stalled ({last_assistant_type} was last type) — "
                        f"returning best text after {elapsed:.0f}s"
                    )
                    return last_text
        else:
            # Unknown session — scan all files newer than spawn time.
            sessions_dir = self._sessions_dir()
            try:
                baseline: dict[Path, int] = {
                    f: f.stat().st_size
                    for f in sessions_dir.glob("*.jsonl")
                }
            except Exception:
                baseline = {}

            # Per-file debounce state.
            file_last_text: dict[Path, Optional[str]] = {}
            file_last_atype: dict[Path, Optional[str]] = {}
            file_last_size: dict[Path, int] = {}
            file_last_activity: dict[Path, float] = {}
            file_activity_seen: dict[Path, bool] = {}

            while time.time() < deadline and self._running:
                time.sleep(_POLL_INTERVAL)
                try:
                    for f in sessions_dir.glob("*.jsonl"):
                        if f.stat().st_mtime <= self._spawn_time:
                            continue
                        offset = baseline.get(f, 0)
                        try:
                            current_size = f.stat().st_size
                        except Exception:
                            continue

                        prev_size = file_last_size.get(f, offset)
                        if current_size > prev_size:
                            file_activity_seen[f] = True
                            file_last_size[f] = current_size
                            file_last_activity[f] = time.time()
                            self._publish_activity(growing=True)
                            text, atype = self._get_jsonl_state(f, offset)
                            if text:
                                file_last_text[f] = text
                                _partial_text = text
                            if atype:
                                file_last_atype[f] = atype

                        if (
                            file_activity_seen.get(f)
                            and file_last_text.get(f)
                            and file_last_atype.get(f) == "text"
                            and (time.time() - file_last_activity.get(f, 0)) >= _DEBOUNCE
                        ):
                            sid = f.stem
                            self._save_session_id(sid)
                            self.current_session_id = sid
                            response = file_last_text[f]
                            log.info(f"Session ID captured on first response: {sid[:8]}...")
                            log.info(f"Response complete ({len(response)} chars)")
                            return response
                except Exception as e:
                    log.warning(f"Session scan error: {e}")

        log.error("Timeout waiting for JSONL response")
        if _partial_text:
            log.warning("Returning partial text after timeout")
            return _partial_text + "\n\n_(Response may be incomplete — timed out after 10 min.)_"
        return "⌛ No response in 10 minutes. Claude may not have received the message. Send /restart to reset the session."

    # ------------------------------------------------------------------
    # Queue processor thread (state machine)
    # ------------------------------------------------------------------

    def _queue_processor_thread(self):
        while self._running:
            item = self.input_queue.get()
            if item is None:
                break

            with self.state_lock:
                self.state = "GENERATING"
                self.current_item = item

            log.info(f"Processing message from {item.source}: {item.text[:50]!r}")

            # Persist user message to Supabase
            supabase_client.save_message(
                role="user",
                content=item.text,
                channel=config.SESSION_CHANNEL,
            )

            # If we know the session file, record its current size so we only
            # read entries written AFTER this message. If session_id is unknown
            # (first exchange on a new session), pass None — _wait_for_jsonl_response
            # will scan all files and capture the ID from the first response.
            if self.current_session_id:
                session_file: Optional[Path] = self._get_session_file_path(self.current_session_id)
                try:
                    initial_size = session_file.stat().st_size if session_file.exists() else 0
                except Exception:
                    initial_size = 0
            else:
                session_file = None
                initial_size = 0

            # Prepend any semantically relevant rules to the message.
            relevant_rules = supabase_client.fetch_relevant_rules(item.text)
            message_text = (relevant_rules + item.text) if relevant_rules else item.text

            # Inject semantically relevant dreams for real user messages.
            if item.source == "telegram":
                dream_context = supabase_client.fetch_relevant_dreams(item.text)
                if dream_context:
                    message_text = dream_context + "\n\n" + message_text

            # Inject message via PTY.
            # Claude's TUI runs in raw terminal mode: Enter = \r (not \n).
            # Write in chunks to avoid PTY buffer limits (~4096 bytes) that
            # cause long messages (e.g. transcribed voice notes) to lose the
            # trailing \r, leaving the message sitting unsubmitted in the TUI.
            try:
                encoded = message_text.encode()
                chunk_size = 256
                for i in range(0, len(encoded), chunk_size):
                    os.write(self.master_fd, encoded[i:i + chunk_size])
                    time.sleep(0.05)
                time.sleep(0.1)
                os.write(self.master_fd, b"\r")
            except OSError:
                log.error("Failed to write message to PTY")
                with self.state_lock:
                    self.state = "IDLE"
                    self.current_item = None
                self._flush_keyboard_buffer()
                continue

            # Poll JSONL for Claude's response.
            response_text = self._wait_for_jsonl_response(session_file, initial_size)
            self._publish_response(item, response_text)

            with self.state_lock:
                self.state = "IDLE"
                self.current_item = None
            self._flush_keyboard_buffer()

    def _flush_keyboard_buffer(self):
        with self.state_lock:
            buffered = self.keyboard_buffer[:]
            self.keyboard_buffer = []
        for chunk in buffered:
            self._write_to_pty(chunk)

    def _write_to_pty(self, data: bytes):
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Response publishing
    # ------------------------------------------------------------------

    def _publish_response(self, item: QueueItem, response_text: str):
        # Parse memory tags, save to Supabase, strip tags from delivered text
        clean_text = supabase_client.process_response(response_text, channel=config.SESSION_CHANNEL)
        supabase_client.save_message(
            role="assistant",
            content=clean_text,
            channel=config.SESSION_CHANNEL,
        )
        payload = json.dumps({
            "text": clean_text,
            "source": item.source,
            "user_id": item.user_id,
        }) + "\n"
        payload_bytes = payload.encode()

        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)
                log.info("Removed dead response subscriber")

        log.info(f"Published response to {len(self.response_subscribers)} subscriber(s)")

    # ------------------------------------------------------------------
    # Socket servers
    # ------------------------------------------------------------------

    def _user_input_server_thread(self):
        sock_path = config.USER_INPUT_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("user_input.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=self._handle_input_conn, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_input_conn(self, conn: socket.socket):
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
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "permission_response":
                            log.info(f"permission_response received: decision={msg.get('decision')!r}")
                            self._resolve_permission(
                                msg.get("decision", "deny"),
                                msg.get("message", ""),
                            )
                        elif msg.get("type") == "tui_response":
                            # User selected a numbered choice from Telegram
                            choice = str(msg.get("choice", "")).strip()
                            if choice:
                                log.info(f"tui_response: sending {choice!r} to PTY")
                                self._last_tui_prompt_hash = None  # reset so re-detection works
                                try:
                                    os.write(self.master_fd, (choice + "\r").encode())
                                except OSError as e:
                                    log.error(f"Failed to write tui_response to PTY: {e}")
                        else:
                            self.input_queue.put(QueueItem(
                                text=msg["text"],
                                source=msg.get("source", "unknown"),
                                user_id=msg.get("user_id", ""),
                                media_path=msg.get("media_path"),
                            ))
                    except (json.JSONDecodeError, KeyError) as e:
                        log.warning(f"Bad input message: {e}")

    def _cli_input_server_thread(self):
        sock_path = config.CLI_INPUT_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("cli_input.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("CLINode keyboard connected")
                threading.Thread(
                    target=self._handle_cli_input, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_cli_input(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(256)
                except Exception:
                    break
                if not data:
                    break

                buf += data

                while b"\x00" in buf:
                    pre, _, rest = buf.partition(b"\x00")
                    if pre:
                        self._route_keyboard_bytes(pre)
                    if b"\n" in rest:
                        line, _, buf = rest.partition(b"\n")
                        try:
                            msg = json.loads(line)
                            if msg.get("type") == "resize":
                                with self.pty_lock:
                                    if self.master_fd is not None:
                                        self._set_pty_size(
                                            self.master_fd,
                                            msg["rows"],
                                            msg["cols"],
                                        )
                        except (json.JSONDecodeError, KeyError):
                            pass
                    else:
                        buf = b"\x00" + rest
                        break
                else:
                    if buf:
                        self._route_keyboard_bytes(buf)
                        buf = b""

        log.info("CLINode keyboard disconnected")

    def _route_keyboard_bytes(self, data: bytes):
        # Always forward CLI keystrokes to PTY — buffering caused CLI to be
        # stuck when Claude showed a TUI prompt (e.g. "1. Yes / 2. No").
        self._write_to_pty(data)

    def _display_server_thread(self):
        sock_path = config.DISPLAY_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("display.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("CLINode display connected")
                with self.display_lock:
                    if self.display_client:
                        try:
                            self.display_client.close()
                        except Exception:
                            pass
                    self.display_client = conn
            except Exception:
                break

    def _response_server_thread(self):
        sock_path = config.CLAUDE_RESPONSE_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("claude_response.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("Response subscriber connected")
                with self.response_subs_lock:
                    self.response_subscribers.append(conn)
            except Exception:
                break

    def _permission_server_thread(self):
        """
        Listens for connections from permission_hook.py.

        Each connection carries one permission request (NDJSON line).
        We hold the connection open, broadcast the request to all response
        subscribers (TelegramNode, etc.), and wait for a decision.
        The decision arrives either via _handle_input_conn (from TelegramNode's
        callback) or is sent directly to the held connection by
        _resolve_permission().
        """
        sock_path = config.PERMISSION_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("permission.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=self._handle_permission_conn, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_permission_conn(self, conn: socket.socket):
        """Read one permission request, hold conn open until decision arrives."""
        buf = b""
        try:
            conn.settimeout(10)
            while b"\n" not in buf:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                buf += chunk
            conn.settimeout(None)
        except Exception as e:
            log.warning(f"Permission hook read error: {e}")
            conn.close()
            return

        line = buf.split(b"\n")[0].strip()
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Permission hook: bad JSON")
            conn.close()
            return

        tool_name = request.get("tool_name", "unknown")
        tool_input = request.get("tool_input", {})
        log.info(f"Permission request: {tool_name} {str(tool_input)[:80]}")

        with self._permission_lock:
            if self._permission_conn is not None:
                # Concurrent request — shouldn't happen with a serial queue,
                # but just in case: deny and close the old one.
                log.warning("Permission request arrived while one is pending — denying old")
                try:
                    self._permission_conn.sendall(
                        json.dumps({"decision": "deny", "message": "Superseded."}).encode() + b"\n"
                    )
                    self._permission_conn.close()
                except Exception:
                    pass
            self._permission_conn = conn

        # Broadcast to all subscribers so TelegramNode can show inline buttons
        self._publish_permission_request(tool_name, tool_input)

        # The connection is now held open; _resolve_permission() will close it
        # when the decision arrives.

    def _publish_permission_request(self, tool_name: str, tool_input: dict):
        payload = json.dumps({
            "type": "permission_request",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    def _resolve_permission(self, decision: str, message: str = ""):
        """
        Called when the user makes a permission decision (allow/deny).
        Sends the response to the waiting permission_hook.py connection.
        """
        with self._permission_lock:
            conn = self._permission_conn
            self._permission_conn = None

        log.info(f"_resolve_permission: decision={decision!r} conn={conn}")
        if conn is None:
            log.warning("_resolve_permission called but no pending permission request")
            return

        payload = {"decision": decision}
        if message:
            payload["message"] = message
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
            conn.close()
        except Exception as e:
            log.warning(f"Failed to send permission decision: {e}")

        log.info(f"Permission resolved: {decision}")

    # ------------------------------------------------------------------
    # Lock file
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        lock = Path(config.LOCK_FILE)
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
                log.error(f"Another SessionManagerNode running (PID {pid})")
                return False
            except (ProcessLookupError, ValueError):
                log.info("Stale lock found, taking over")
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        return True

    def _release_lock(self):
        try:
            Path(config.LOCK_FILE).unlink()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def run(self):
        if not self._acquire_lock():
            sys.exit(1)

        os.makedirs(config.SOCKET_DIR, exist_ok=True)
        os.makedirs(config.RELAY_DIR, exist_ok=True)

        self._spawn_claude()

        self._reader_thread = threading.Thread(
            target=self._pty_reader_thread, daemon=True
        )

        threads = [
            self._reader_thread,
            threading.Thread(target=self._queue_processor_thread, daemon=True),
            threading.Thread(target=self._user_input_server_thread, daemon=True),
            threading.Thread(target=self._cli_input_server_thread, daemon=True),
            threading.Thread(target=self._display_server_thread, daemon=True),
            threading.Thread(target=self._response_server_thread, daemon=True),
            threading.Thread(target=self._permission_server_thread, daemon=True),
        ]
        for t in threads:
            t.start()

        signal_module.signal(signal_module.SIGINT, self._shutdown)
        signal_module.signal(signal_module.SIGTERM, self._shutdown)

        log.info("SessionManagerNode running — press Ctrl+C to stop")

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self, *_):
        log.info("Shutting down...")
        self._running = False
        self.input_queue.put(None)

        if self.claude_proc:
            try:
                self.claude_proc.terminate()
            except Exception:
                pass

        for sock_path in [
            config.USER_INPUT_SOCK,
            config.CLI_INPUT_SOCK,
            config.DISPLAY_SOCK,
            config.CLAUDE_RESPONSE_SOCK,
            config.PERMISSION_SOCK,
        ]:
            try:
                os.unlink(sock_path)
            except Exception:
                pass

        self._release_lock()
        sys.exit(0)


if __name__ == "__main__":
    SessionManagerNode().run()
