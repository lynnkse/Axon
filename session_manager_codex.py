#!/usr/bin/env python3
from __future__ import annotations
"""
SessionManagerCodexNode — Codex-backed engine for Axon's relay v2 protocol.

Unlike session_manager.py (one long-lived Claude Code PTY process for the
relay's whole lifetime, response detection by polling a growing JSONL file),
Codex CLI has a proper non-interactive mode: `codex exec [resume <id>] --json
"<prompt>"` streams structured JSONL events to stdout and *exits* when the
turn is done. So this engine is a per-turn subprocess spawner, not a
persistent-PTY driver -- no PTY, no TUI-scraping, no stall-fallback timer.

Confirmed live event shapes (codex 0.149.1, 2026-09-05 -- these differ from
the on-disk ~/.codex/sessions/*.jsonl rollout-file format, which is a
separate serialization; do not confuse the two):
  {"type":"thread.started","thread_id":"..."}
  {"type":"turn.started"}
  {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
  {"type":"turn.completed","usage":{...}}
  {"type":"turn.failed","error":{"message":"..."}}

Sockets (same protocol as session_manager.py -- consumers don't know or care
which engine produced a response):
  user_input.sock      — NDJSON in:  {text, source, user_id, media_path?, request_id?}
  cli_input.sock       — not implemented (no live PTY to type into mid-turn)
  display.sock         — raw text lines out: one human-readable line per parsed event
  claude_response.sock — NDJSON out: {text, source, user_id, request_id?}
  permission.sock      — listens but unused (codex runs with
                         --dangerously-bypass-approvals-and-sandbox, matching
                         Axon's existing --dangerously-skip-permissions posture)

Generalization note: unlike session_manager.py's instance hooks (which fall
back to hardcoded `if config.INSTANCE == "ailin"` branches when no plugin is
configured), this engine supports ONLY the generic instance_plugin.py
contract -- no hardcoded per-instance behavior. Any instance running this
engine must supply a plugin via AXON_EXTENSIONS_PATH.
"""

import os
import sys
import socket
import threading
import queue
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
import supabase_client
from instance_plugin import TurnContext, _load_instance_plugin
from actor_model.prompt_blocks import (
    ActorBlockError, CODE_HASH_TURN_WINDOW, output_instructions,
    parse_actor_updates, prompt_actor_rows, render_actor_inputs,
    strip_actor_blocks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [session_manager_codex] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# How long to wait for a turn to finish before giving up (seconds). Unlike
# Claude's PTY approach there's no polling/stall-fallback needed -- the codex
# exec subprocess itself exits when the turn completes; this is just a safety
# net against a genuinely hung process.
_RESPONSE_TIMEOUT = 600


@dataclass
class QueueItem:
    text: str
    source: str        # "telegram" | "proactive" | "reflection"
    user_id: str
    media_path: Optional[str] = None
    request_id: Optional[str] = None
    prompt_actor_rows: Optional[list[dict]] = None


class SessionManagerCodexNode:

    def __init__(self):
        self.instance_plugin = _load_instance_plugin(config.EXTENSIONS_PATH)
        if self.instance_plugin is None:
            raise RuntimeError(
                "session_manager_codex.py requires AXON_EXTENSIONS_PATH to be "
                "set to a valid instance plugin -- no hardcoded per-instance "
                "fallback behavior exists in this engine."
            )
        self.input_queue: queue.Queue[Optional[QueueItem]] = queue.Queue()
        self.state = "IDLE"
        self.current_item: Optional[QueueItem] = None
        self.state_lock = threading.Lock()

        self.current_thread_id: Optional[str] = None
        self._first_turn_done = False

        self.display_clients: list[socket.socket] = []
        self.display_lock = threading.Lock()
        self.response_subscribers: list[socket.socket] = []
        self.response_subs_lock = threading.Lock()

        self._running = True

    # ------------------------------------------------------------------
    # Instance plugin glue (identical contract to session_manager.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _turn_context(item: QueueItem) -> TurnContext:
        return TurnContext(
            text=item.text,
            source=item.source,
            user_id=item.user_id,
            request_id=item.request_id,
        )

    def _on_instance_turn_received(self, item: QueueItem) -> None:
        self.instance_plugin.on_turn_received(self._turn_context(item))

    def _instance_context_for_turn(self, item: QueueItem) -> str:
        return self.instance_plugin.context_for_turn(self._turn_context(item))

    def _transform_instance_response(self, item: QueueItem, response_text: str) -> str:
        return self.instance_plugin.transform_response(self._turn_context(item), response_text)

    def _on_instance_turn_completed(self, item: QueueItem, clean_text: str) -> None:
        self.instance_plugin.on_turn_completed(self._turn_context(item), clean_text)

    def _load_profile(self) -> str:
        try:
            return config.PROFILE_PATH.read_text()
        except Exception:
            return ""

    def _build_first_turn_prefix(self) -> str:
        """System-prompt-equivalent content, sent only on the first turn of a
        thread (Codex's `resume` keeps server/local-side context alive for
        every turn after that, same principle as Claude's --append-system-prompt
        being baked into the process at spawn time)."""
        profile = self._load_profile()
        parts = []
        if config.USER_NAME:
            parts.append(f"You are speaking with {config.USER_NAME}.")
        if config.USER_TIMEZONE:
            parts.append(f"User timezone: {config.USER_TIMEZONE}")
        if profile:
            parts.append(f"\nProfile:\n{profile}")
        instance_context = self.instance_plugin.system_prompt_context()
        if instance_context:
            parts.append(f"\n{instance_context}")
        if config.ACTORS_ENABLED:
            parts.append("\n" + output_instructions())
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Thread ID tracking
    # ------------------------------------------------------------------

    def _get_saved_thread_id(self) -> Optional[str]:
        try:
            return Path(config.CODEX_THREAD_ID_FILE).read_text().strip() or None
        except Exception:
            return None

    def _save_thread_id(self, thread_id: str):
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.CODEX_THREAD_ID_FILE).write_text(thread_id)
        log.info(f"Thread ID saved: {thread_id[:8]}...")

    # ------------------------------------------------------------------
    # Codex turn execution
    # ------------------------------------------------------------------

    def _run_codex_turn(self, message_text: str) -> tuple[str, Optional[str]]:
        """Spawn one `codex exec` (or `codex exec resume`) subprocess for a
        single turn, stream its JSONL stdout, and return (response_text,
        error_message). error_message is None on success."""
        cmd = [config.CODEX_PATH, "exec"]
        if self.current_thread_id:
            # `codex exec resume` does not accept -C (its cwd is already
            # fixed from the first turn) -- confirmed via direct testing;
            # passing it makes the whole subprocess fail to parse args, which
            # (combined with discarding stderr) silently looked like an empty
            # response rather than a hard error.
            cmd += ["resume", self.current_thread_id]
        else:
            cmd += ["-C", config.PROJECT_DIR]
        cmd += [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            message_text,
        ]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        response_text = ""
        error_message: Optional[str] = None
        deadline = time.time() + _RESPONSE_TIMEOUT

        try:
            for line in iter(proc.stdout.readline, ""):
                if time.time() > deadline:
                    error_message = "Codex turn exceeded timeout"
                    proc.kill()
                    break
                line = line.strip()
                if not line:
                    continue
                self._forward_display(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "thread.started" and not self.current_thread_id:
                    tid = event.get("thread_id")
                    if tid:
                        self.current_thread_id = tid
                        self._save_thread_id(tid)
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        response_text = item["text"]
                elif etype == "turn.failed":
                    err = event.get("error") or {}
                    error_message = err.get("message", "unknown codex error")
                elif etype == "turn.completed":
                    break
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        if not response_text and not error_message:
            # No agent_message and no turn.failed event parsed -- either the
            # process exited before emitting JSONL (bad args, crash) or
            # produced unparseable output. Surface stderr so this never
            # silently looks like a valid empty response.
            stderr_text = ""
            try:
                stderr_text = (proc.stderr.read() or "").strip()
            except Exception:
                pass
            error_message = (
                f"codex exited (code={proc.returncode}) with no response: "
                f"{stderr_text[:500]}" if stderr_text else
                f"codex exited (code={proc.returncode}) with no response and no stderr"
            )

        return response_text, error_message

    def _forward_display(self, line: str):
        data = (line + "\n").encode()
        with self.display_lock:
            dead = []
            for conn in self.display_clients:
                try:
                    conn.sendall(data)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.display_clients.remove(conn)

    # ------------------------------------------------------------------
    # Queue processor
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

            supabase_client.save_message(
                role="user", content=item.text, channel=config.SESSION_CHANNEL,
            )
            self._on_instance_turn_received(item)

            permanent_rules = supabase_client.fetch_permanent_rules()
            relevant_rules = supabase_client.fetch_relevant_rule_names(item.text)
            if relevant_rules:
                import re as _re
                fired_names = _re.findall(r'^- ([\w_-]+)', relevant_rules, _re.MULTILINE)
                if fired_names:
                    supabase_client.bump_rule_usage(fired_names)
            prefix = (permanent_rules + "\n\n" if permanent_rules else "") + (relevant_rules or "")

            reflection_context = ""
            if item.source == "telegram":
                reflection_context = self._instance_context_for_turn(item)

            actor_rows = []
            actor_context = ""
            is_real_user_prompt = item.source not in {"reflection", "proactive", "system"}
            if config.ACTORS_ENABLED and is_real_user_prompt:
                fetched_actor_rows = supabase_client.fetch_prompt_actor_states()
                if fetched_actor_rows is None:
                    log.error("Prompt actor injection skipped because actor_state fetch failed")
                else:
                    try:
                        actor_rows = prompt_actor_rows(
                            fetched_actor_rows,
                            supabase_client.prompt_actor_relevance_changed,
                            max_slots=config.MAX_ACTOR_SLOTS,
                        )
                        actor_context = render_actor_inputs(actor_rows, {}, current_turn=1)
                    except ActorBlockError as exc:
                        actor_rows = []
                        log.error("Prompt actor inputs rejected: %s", exc)
            item.prompt_actor_rows = actor_rows

            message_parts = []
            if not self._first_turn_done:
                message_parts.append(self._build_first_turn_prefix())
            if reflection_context:
                message_parts.append(reflection_context)
            if prefix.strip():
                message_parts.append(prefix.rstrip())
            if actor_context:
                message_parts.append(actor_context)
            message_parts.append(item.text)
            message_text = "\n\n".join(part for part in message_parts if part)

            response_text, error_message = self._run_codex_turn(message_text)
            self._first_turn_done = True

            if error_message:
                log.error(f"Codex turn failed: {error_message}")
                response_text = response_text or f"[codex engine error: {error_message}]"

            self._publish_response(item, response_text)

            with self.state_lock:
                self.state = "IDLE"
                self.current_item = None

    # ------------------------------------------------------------------
    # Response publishing
    # ------------------------------------------------------------------

    def _publish_response(self, item: QueueItem, response_text: str):
        actor_rows = item.prompt_actor_rows or []
        expected_actor_ids = {str(row.get("actor_id")) for row in actor_rows}
        if config.ACTORS_ENABLED and expected_actor_ids:
            try:
                updates = parse_actor_updates(response_text, expected_actor_ids)
            except ActorBlockError as exc:
                log.error("Prompt actor response rejected; no actor rows written: %s", exc)
            else:
                rows_by_id = {str(row.get("actor_id")): row for row in actor_rows}
                failures = []
                for update in updates:
                    if not supabase_client.save_prompt_actor_update(rows_by_id[update.actor_id], update):
                        failures.append(update.actor_id)
                if failures:
                    log.error("Prompt actor persistence incomplete; failed actor_ids=%s", failures)
                else:
                    log.info("Prompt actor response persisted: count=%d", len(updates))

        response_without_actor_blocks = strip_actor_blocks(response_text)
        self._publish_clean_response(item, response_without_actor_blocks)

    def _publish_clean_response(self, item: QueueItem, response_text: str):
        response_text = self._transform_instance_response(item, response_text)
        clean_text = supabase_client.process_response(response_text, channel=config.SESSION_CHANNEL)
        supabase_client.save_message(
            role="assistant", content=clean_text, channel=config.SESSION_CHANNEL,
        )
        self._on_instance_turn_completed(item, clean_text)

        payload = json.dumps({
            "text": clean_text,
            "source": item.source,
            "user_id": item.user_id,
            "request_id": item.request_id,
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
                threading.Thread(target=self._handle_input_conn, args=(conn,), daemon=True).start()
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
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "permission_response":
                        continue
                    item = QueueItem(
                        text=msg.get("text", ""),
                        source=msg.get("source", "unknown"),
                        user_id=msg.get("user_id", "unknown"),
                        media_path=msg.get("media_path"),
                        request_id=msg.get("request_id"),
                    )
                    self.input_queue.put(item)

    def _cli_input_server_thread(self):
        # No live PTY to type into mid-turn (each turn is a fire-and-forget
        # subprocess) -- listen so consumers can connect without erroring,
        # but there's nothing to forward.
        sock_path = config.CLI_INPUT_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("cli_input.sock listening (no-op engine)")
        while self._running:
            try:
                conn, _ = server.accept()
                conn.close()
            except Exception:
                break

    def _display_server_thread(self):
        sock_path = config.DISPLAY_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("display.sock listening")
        while self._running:
            try:
                conn, _ = server.accept()
                log.info("Display client connected (%d total)", len(self.display_clients) + 1)
                with self.display_lock:
                    self.display_clients.append(conn)
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
        # Unused -- codex runs with --dangerously-bypass-approvals-and-sandbox,
        # matching Axon's existing fully-autonomous posture. Kept listening
        # only for interface parity with consumers that might connect.
        sock_path = config.PERMISSION_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("permission.sock listening (unused by codex engine)")
        while self._running:
            try:
                conn, _ = server.accept()
                conn.close()
            except Exception:
                break

    # ------------------------------------------------------------------
    # Lock / lifecycle
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        lock = Path(config.CODEX_LOCK_FILE)
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
                log.error(f"Another SessionManagerCodexNode running (PID {pid})")
                return False
            except (ProcessLookupError, ValueError):
                log.info("Stale lock found, taking over")
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        return True

    def _release_lock(self):
        try:
            Path(config.CODEX_LOCK_FILE).unlink()
        except Exception:
            pass

    def run(self):
        if not self._acquire_lock():
            sys.exit(1)

        os.makedirs(config.SOCKET_DIR, exist_ok=True)
        os.makedirs(config.RELAY_DIR, exist_ok=True)

        self.current_thread_id = self._get_saved_thread_id()
        if self.current_thread_id:
            log.info(f"Resuming thread: {self.current_thread_id[:8]}...")
            self._first_turn_done = True

        import signal as signal_module
        threads = [
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

        log.info("SessionManagerCodexNode running — press Ctrl+C to stop")

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
        for sock_path in [
            config.USER_INPUT_SOCK, config.CLI_INPUT_SOCK, config.DISPLAY_SOCK,
            config.CLAUDE_RESPONSE_SOCK, config.PERMISSION_SOCK,
        ]:
            try:
                os.unlink(sock_path)
            except Exception:
                pass
        self._release_lock()
        sys.exit(0)


if __name__ == "__main__":
    SessionManagerCodexNode().run()
