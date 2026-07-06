#!/usr/bin/env python3
"""
orchestrator.py — Axon relay central router.

Manages DeepSeek session (direct API with sliding-window context compression)
and Claude session (via session_manager.py). Broadcasts all activity to all
connected output subscribers (telegram_node, cli_node) with source labels.

Sockets:
  user_input.sock       — NDJSON in from any frontend (telegram, cli)
  claude_response.sock  — NDJSON out to all subscribers {text, source, user_id}

Claude delegation:
  When DeepSeek outputs <CLAUDE_REQUEST>...</CLAUDE_REQUEST>, the orchestrator
  routes it to Claude via session_manager sockets, returns result to DeepSeek.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from openai import OpenAI

sys.path.insert(0, os.path.dirname(__file__))
import config
import supabase_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [orchestrator] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── DeepSeek session config ───────────────────────────────────────────────────
DS_API_KEY   = os.environ.get("DEEPSEEK_API_KEY", config.get("DEEPSEEK_API_KEY"))
DS_MODEL     = os.environ.get("DEEPSEEK_MODEL",   config.get("DEEPSEEK_MODEL", "deepseek-chat"))
HISTORY_FILE = Path(os.environ.get("RELAY_DIR", str(Path.home() / ".claude-relay"))) / "ds_history.json"
MAX_TURNS    = 40   # full turns kept before compression
KEEP_TURNS   = 20   # turns kept after compression

CLAUDE_REQUEST_RE = re.compile(r"<CLAUDE_REQUEST>(.*?)</CLAUDE_REQUEST>", re.DOTALL)

# ── Broadcaster: publish to all connected response sockets ────────────────────
class Broadcaster:
    def __init__(self):
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()

    def add(self, sock: socket.socket):
        with self._lock:
            self._clients.append(sock)
        log.info(f"Subscriber connected (total: {len(self._clients)})")

    def broadcast(self, text: str, source: str = "axon", user_id: str | None = None):
        msg = (json.dumps({"text": text, "source": source, "user_id": user_id}) + "\n").encode()
        with self._lock:
            dead = []
            for s in self._clients:
                try:
                    s.sendall(msg)
                except Exception:
                    dead.append(s)
            for s in dead:
                self._clients.remove(s)
                log.info(f"Subscriber disconnected (total: {len(self._clients)})")


# ── DeepSeek session with sliding-window context compression ──────────────────
class DeepSeekSession:
    def __init__(self, system_prompt: str):
        self.client   = OpenAI(api_key=DS_API_KEY, base_url="https://api.deepseek.com/v1")
        self.system   = system_prompt
        self.messages: list[dict] = []
        self._load()

    def _load(self):
        try:
            if HISTORY_FILE.exists():
                self.messages = json.loads(HISTORY_FILE.read_text())
                log.info(f"Loaded {len(self.messages)} turns from disk")
        except Exception as e:
            log.warning(f"Could not load history: {e}")
            self.messages = []

    def _save(self):
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(self.messages, ensure_ascii=False, indent=2))
        except Exception as e:
            log.warning(f"Could not save history: {e}")

    def _compress(self):
        """Summarize oldest turns, keep recent KEEP_TURNS."""
        old    = self.messages[:-KEEP_TURNS]
        recent = self.messages[-KEEP_TURNS:]
        snippet = "\n".join(
            f"{m['role']}: {str(m.get('content',''))[:400]}" for m in old
        )
        try:
            resp = self.client.chat.completions.create(
                model=DS_MODEL,
                messages=[{"role": "user", "content":
                    f"Summarize this conversation concisely, preserving key facts:\n{snippet}"}],
                max_tokens=800,
            )
            summary = resp.choices[0].message.content
        except Exception:
            summary = f"[{len(old)} earlier turns omitted]"
        self.messages = [{"role": "user", "content": f"[Context summary: {summary}]"},
                         {"role": "assistant", "content": "Understood, I have that context."}] + recent
        log.info(f"Compressed history to {len(self.messages)} turns")

    def chat(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        if len(self.messages) > MAX_TURNS:
            self._compress()
        try:
            resp = self.client.chat.completions.create(
                model=DS_MODEL,
                messages=[{"role": "system", "content": self.system}] + self.messages,
            )
            reply = resp.choices[0].message.content or ""
        except Exception as e:
            reply = f"[DeepSeek error: {e}]"
        self.messages.append({"role": "assistant", "content": reply})
        self._save()
        return reply

    def inject(self, text: str):
        """Feed a message back without triggering a new DS response."""
        self.messages.append({"role": "user", "content": text})
        self._save()


# ── Claude delegation via session_manager ────────────────────────────────────
class ClaudeProxy:
    def __init__(self):
        self._lock = threading.Lock()

    def send(self, request: str) -> str:
        """Send a request to Claude via user_input.sock and wait for response."""
        # We use a secondary socket dir for the Claude session.
        # Claude session_manager listens on CLAUDE_INPUT_SOCK.
        claude_input  = config.SOCKET_DIR + "/claude_input.sock"
        claude_output = config.SOCKET_DIR + "/claude_output.sock"
        try:
            with self._lock:
                # Send to Claude
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(claude_input)
                payload = (json.dumps({"text": request, "source": "orchestrator", "user_id": "system"}) + "\n").encode()
                s.sendall(payload)
                s.close()
                # Wait for response on dedicated output socket
                r = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                r.connect(claude_output)
                r.settimeout(120)
                chunks = []
                while True:
                    data = r.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                r.close()
                raw = b"".join(chunks).decode()
                try:
                    return json.loads(raw.splitlines()[0]).get("text", raw)
                except Exception:
                    return raw
        except Exception as e:
            return f"[Claude error: {e}]"


# ── Main orchestrator ─────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self):
        self.broadcaster  = Broadcaster()
        self.claude       = ClaudeProxy()
        self._build_system()
        self.ds           = DeepSeekSession(self._system)
        self._input_lock  = threading.Lock()

    def _build_system(self):
        name     = config.USER_NAME or "Anton"
        tz       = config.USER_TIMEZONE
        profile  = ""
        try:
            profile = config.PROFILE_PATH.read_text()[:3000]
        except Exception:
            pass
        memory = ""
        try:
            memory = supabase_client.fetch_memory_context()[:2000]
        except Exception:
            pass
        self._system = f"""You are Axon — an AI assistant for {name} (timezone: {tz}).

You have access to Claude Code (a powerful AI that can read/write files, run bash, SSH, etc.)
via a delegation mechanism. When you need Claude to DO something (not just reason about it),
output exactly:
<CLAUDE_REQUEST>
Your detailed request to Claude here. Be specific about files, commands, actions needed.
</CLAUDE_REQUEST>

The orchestrator will route this to Claude, execute it, and return the result to you.
You can then incorporate the result in your response.

For pure reasoning, planning, answering questions — respond directly without delegation.

{f"User profile:{chr(10)}{profile}" if profile else ""}
{f"Memory context:{chr(10)}{memory}" if memory else ""}"""

    def _handle_message(self, text: str, source: str, user_id: str):
        """Process one user message through DeepSeek, route Claude calls."""
        # Show user message in both channels
        self.broadcaster.broadcast(f"[You] {text}", "user_echo", user_id)

        # DeepSeek turn
        log.info(f"[DS] processing: {text[:80]}")
        ds_response = self.ds.chat(text)
        log.info(f"[DS] response: {ds_response[:120]}")

        # Check for Claude delegation requests
        claude_matches = CLAUDE_REQUEST_RE.findall(ds_response)
        clean_response = CLAUDE_REQUEST_RE.sub("", ds_response).strip()

        if claude_matches:
            # Show DeepSeek's response without the raw tags
            if clean_response:
                self.broadcaster.broadcast(clean_response, "deepseek", user_id)

            for req in claude_matches:
                req = req.strip()
                self.broadcaster.broadcast(f"[CC→] {req[:300]}", "claude_request", user_id)
                log.info(f"[CC] delegating: {req[:80]}")
                result = self.claude.send(req)
                self.broadcaster.broadcast(f"[CC✓] {result[:500]}", "claude_result", user_id)
                log.info(f"[CC] result: {result[:80]}")
                # Feed result back to DeepSeek
                self.ds.inject(f"[Claude result]: {result}")

            # Get DeepSeek's final synthesis
            final = self.ds.chat("Summarize what was done and give the final result to the user.")
            self.broadcaster.broadcast(final, "deepseek", user_id)
        else:
            self.broadcaster.broadcast(ds_response, "deepseek", user_id)

    # ── Socket servers ────────────────────────────────────────────────────────

    def _serve_input(self):
        """Accept messages from telegram_node / cli_node on user_input.sock."""
        sock_path = config.USER_INPUT_SOCK
        Path(sock_path).unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(8)
        log.info(f"Listening on {sock_path}")
        while True:
            conn, _ = server.accept()
            threading.Thread(target=self._read_input, args=(conn,), daemon=True).start()

    def _read_input(self, conn: socket.socket):
        try:
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        text    = msg.get("text", "")
                        source  = msg.get("source", "unknown")
                        user_id = msg.get("user_id", "")
                        if text:
                            threading.Thread(
                                target=self._handle_message,
                                args=(text, source, user_id),
                                daemon=True,
                            ).start()
                    except Exception as e:
                        log.warning(f"Bad input: {e}")
        finally:
            conn.close()

    def _serve_response(self):
        """Accept response subscribers on claude_response.sock."""
        sock_path = config.CLAUDE_RESPONSE_SOCK
        Path(sock_path).unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(8)
        log.info(f"Response socket on {sock_path}")
        while True:
            conn, _ = server.accept()
            self.broadcaster.add(conn)

    def run(self):
        threading.Thread(target=self._serve_input,    daemon=True).start()
        threading.Thread(target=self._serve_response, daemon=True).start()
        log.info(f"Orchestrator ready — model: {DS_MODEL}")
        self.broadcaster.broadcast("Axon ready.", "status")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    Orchestrator().run()
