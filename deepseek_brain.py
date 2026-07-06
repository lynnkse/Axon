#!/usr/bin/env python3
"""
deepseek_brain.py — DeepSeek-powered brain for relay v2.

Drop-in replacement for session_manager.py.
Same socket interface: reads user_input.sock, publishes to claude_response.sock.

Architecture:
  - DeepSeek API handles all reasoning and planning (sandbox — no FS/network access)
  - executor.py handles all file/bash/SSH operations (trusted layer)
  - Haiku (Claude API) used as watchdog for risky actions (optional, set WATCHDOG=1)

DeepSeek uses OpenAI-compatible tool calling to request actions.
Executor validates, confirms via Telegram if needed, then executes.

Environment variables (.env):
  DEEPSEEK_API_KEY      — required
  DEEPSEEK_MODEL        — default: deepseek-chat (V3). Use deepseek-reasoner for R1.
  ALLOWED_ROOTS         — comma-separated dirs DeepSeek may read/write (default: PROJECT_DIR)
  WATCHDOG              — set to 1 to enable Claude Haiku review of risky actions
  ANTHROPIC_API_KEY     — required if WATCHDOG=1
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import supabase_client
import time
from pathlib import Path
from typing import Optional

import config
from executor import Action, Result, configure as configure_executor, execute as executor_execute

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [deepseek] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY: str = config.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = config.get("DEEPSEEK_MODEL", "deepseek-chat")
ALLOWED_ROOTS: list[str] = [
    r.strip() for r in config.get("ALLOWED_ROOTS", config.PROJECT_DIR).split(",") if r.strip()
]
WATCHDOG_ENABLED: bool = config.get("WATCHDOG", "0") == "1"

MAX_TOOL_ROUNDS = 10       # max consecutive tool calls before forcing text response
MAX_HISTORY = 40           # messages to keep in context (older ones dropped)

# ── Tool definitions (OpenAI function calling format) ─────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_claude",
            "description": "Delegate a task to Claude for execution. Use this for ALL actions that modify state: writing files, running shell commands, SSH operations, git commits, installs, restarts, deletions, or any complex multi-step code change. Claude will execute the task, stream output to the user, and return the result. Do NOT use for reading files or listing directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "Plain-language description of what to do. Include all context: file paths, what to change, why."}
                },
                "required": ["request"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_memory",
            "description": "Query long-term memory: facts, goals, preferences, recent messages, food log, tasks, or any stored data. Use this instead of asking the user — the answer is often already saved. Accepts raw SQL for the underlying database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL SELECT query to run against the memory database. Tables: memory, messages, personal_tasks, food_entries, fitness_log, frequent_foods, machines, insights, projects, documents."}
                },
                "required": ["sql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load full instructions for a named skill. The system prompt lists available skills with one-line descriptions. Call this when the user's message triggers a skill — before following its protocol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name as listed in the system prompt skills index (e.g. 'fitness_protocol', 'russia_tracker')"}
                },
                "required": ["name"]
            }
        }
    },
]

SUMMARY_EVERY_N = 20   # summarize after every N messages saved


def _build_system_prompt(summaries: list | None = None) -> str:
    base = (
        "You are an AI assistant powered by DeepSeek. "
        "You are NOT Claude and NOT made by Anthropic - do not claim otherwise if asked. "
        "You have access to the user's computer via tools.\n\n"
        "CRITICAL RULES — read these first:\n"
        "1. For greetings, simple questions, status checks, or anything you can answer from "
        "the context already in this system prompt: respond DIRECTLY with no tool calls.\n"
        "2. Recent conversation history and memory facts are ALREADY loaded below. "
        "Do NOT use read_file to explore the relay codebase to answer memory questions. "
        "Do NOT read supabase_client.py or any relay source files unless the user explicitly "
        "asks you to look at the relay code.\n"
        "3. To fetch fresh data from memory/Supabase, use the query_memory tool with a SQL "
        "SELECT — do not read source files to figure out how.\n\n"
        "You can read files freely. For writes, deletes, and shell commands the user will be "
        "asked to confirm before anything executes - so propose actions freely, "
        "they won't happen without approval.\n\n"
        f"Working context:\n"
        f"- User: {config.USER_NAME or 'Lynn'}\n"
        f"- This relay runs on ROG (lynnkse@100.73.56.102). Code is at ~/cognitive-hq/. NOT on Leonid.\n"
        f"- Leonid (anpl@100.98.191.76) is a separate machine running Ailin/creature bot.\n"
        f"- Allowed directories: {', '.join(ALLOWED_ROOTS)}\n"
        f"- Timezone: {config.USER_TIMEZONE}\n\n"
        "Memory & messages:\n"
        "- query_memory with 'FROM messages' fetches real Supabase messages table (user+assistant turns). "
        "The result is a formatted transcript — this IS the messages table, not a different database.\n"
        "- query_memory with 'FROM memory' fetches long-term facts and goals.\n\n"
        "When working on code or files:\n"
        "1. Use read_file / list_dir freely to understand the current state\n"
        "2. For ANY action that changes state (write, delete, bash, SSH, git, installs): "
        "use delegate_to_claude with a clear plain-language request\n"
        "3. Claude will execute the action and report back - "
        "you continue the conversation with the result\n\n"
        "Be concise. No need to narrate every tool call - just do the work and summarize results."
    )
    parts = [base]
    try:
        skills_index = supabase_client.fetch_skills_index()
        if skills_index:
            parts.append(skills_index)
            log.info(f"[prompt] skills index loaded: {len(skills_index)} chars")
    except Exception as e:
        log.warning(f"Failed to load skills index: {e}")
    try:
        mem = supabase_client.fetch_memory_context()
        if mem:
            parts.append(mem)
            log.info(f"[prompt] memory loaded: {len(mem)} chars")
        else:
            log.warning("[prompt] memory empty or unavailable")
    except Exception as e:
        log.warning(f"Failed to load Supabase context: {e}")
    if summaries:
        summary_block = "Conversation history summaries (oldest → newest, each covers ~20 messages):\n" + "\n\n".join(
            f"[Summary {i+1}] {s}" for i, s in enumerate(summaries)
        )
        parts.append(summary_block)
        log.info(f"[prompt] {len(summaries)} summaries injected")
    total = len("\n\n".join(parts))
    log.info(f"[prompt] system prompt built: {total} chars total")
    return "\n\n".join(parts)




# ── DeepSeek client ───────────────────────────────────────────────────────────

def _get_openai_client():
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai package not installed. Run: pip install openai")
        sys.exit(1)
    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )


# ── Watchdog (Claude Haiku review of risky actions) ───────────────────────────

def _watchdog_review(action: Action) -> tuple[bool, str]:
    """Returns (approved, reason). Only called for non-auto-approve actions."""
    if not WATCHDOG_ENABLED:
        return True, "watchdog disabled"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.get("ANTHROPIC_API_KEY", ""))
        prompt = (
            f"Review this action requested by DeepSeek:\n"
            f"Type: {action.type}\nParams: {json.dumps(action.params, indent=2)}\n\n"
            f"Is this action safe and sensible for a developer assistant? "
            f"Reply with JSON: {{\"safe\": true/false, \"reason\": \"...\"}}"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if '{' in raw:
            raw = raw[raw.index('{'):raw.rindex('}')+1]
        result = json.loads(raw)
        return bool(result.get("safe")), result.get("reason", "")
    except Exception as e:
        log.warning(f"Watchdog error: {e} — defaulting to approve")
        return True, f"watchdog error: {e}"


# ── Persistent Claude executor session ───────────────────────────────────────

class ClaudeExecutorSession:
    """
    Persistent Claude Code session used by delegate_to_claude.

    Spawns session_manager.py as a subprocess with its own SOCKET_DIR so it
    does not interfere with the main relay sockets.  Communicates via the same
    JSON newline-delimited protocol telegram_node uses — no PTY code duplicated.

    Socket dir: /tmp/cognitive-hq/claude-exec
    """
    SOCKET_DIR   = "/tmp/cognitive-hq/claude-exec"
    INPUT_SOCK   = f"{SOCKET_DIR}/user_input.sock"
    RESPONSE_SOCK = f"{SOCKET_DIR}/claude_response.sock"
    RESPONSE_TIMEOUT = 180   # seconds to wait for Claude to reply

    def __init__(self):
        import subprocess as _sp
        self._proc: Optional[_sp.Popen] = None
        self._response_conn: Optional[socket.socket] = None
        self._send_lock = threading.Lock()   # only one delegate call at a time
        os.makedirs(self.SOCKET_DIR, exist_ok=True)
        self._start()

    def _start(self) -> None:
        import subprocess as _sp
        sm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_manager.py")
        env = {**os.environ, "SOCKET_DIR": self.SOCKET_DIR}
        log.info(f"[claude-exec] spawning session_manager: {sm_path}")
        self._proc = _sp.Popen(
            [sys.executable, sm_path],
            env=env,
            cwd=os.path.dirname(sm_path),
        )
        # Wait for input socket to appear (session_manager signals readiness this way)
        for _ in range(40):
            if os.path.exists(self.INPUT_SOCK):
                break
            time.sleep(1)
        else:
            raise RuntimeError("[claude-exec] session_manager socket did not appear in 40s")
        # Subscribe to the response socket (stays open — receives all messages)
        self._subscribe_response()
        log.info("[claude-exec] session ready")

    def _subscribe_response(self) -> None:
        for _ in range(20):
            if os.path.exists(self.RESPONSE_SOCK):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("[claude-exec] response socket did not appear in 10s")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.RESPONSE_SOCK)
        self._response_conn = s
        # Drain any buffered startup messages
        s.setblocking(False)
        try:
            while s.recv(4096):
                pass
        except BlockingIOError:
            pass
        s.setblocking(True)
        log.info("[claude-exec] subscribed to response socket")

    def _restart_if_dead(self) -> None:
        if self._proc and self._proc.poll() is None:
            return   # still running
        log.warning("[claude-exec] session_manager died — restarting")
        if self._response_conn:
            try:
                self._response_conn.close()
            except Exception:
                pass
            self._response_conn = None
        self._start()

    def send(self, request: str, publish_text) -> str:
        """Send request to the persistent Claude session, stream output, return final text."""
        with self._send_lock:
            self._restart_if_dead()

            # Deliver request
            payload = json.dumps({"text": request, "user_id": "deepseek"}) + "\n"
            inp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            inp.connect(self.INPUT_SOCK)
            inp.sendall(payload.encode())
            inp.close()

            # Read from response socket until we get a final (non-growing) message
            buf = b""
            self._response_conn.settimeout(self.RESPONSE_TIMEOUT)
            try:
                while True:
                    chunk = self._response_conn.recv(4096)
                    if not chunk:
                        # Socket closed — session died mid-response
                        return "[Claude executor session closed unexpectedly]"
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line.decode())
                        except Exception:
                            continue
                        if msg.get("type") == "confirm_request":
                            # Permission request from Claude — forward to user via publish_text
                            summary = msg.get("summary", "Claude is requesting permission")
                            publish_text(f"🔑 Claude permission request:\n{summary}")
                            continue
                        if msg.get("growing"):
                            publish_text(f"🔧 {msg.get('text', '')}")
                            continue
                        text = msg.get("text", "")
                        return text or "done"
            except socket.timeout:
                return "[Claude executor session timed out after {}s]".format(self.RESPONSE_TIMEOUT)

    def close(self) -> None:
        if self._response_conn:
            try:
                self._response_conn.close()
            except Exception:
                pass
        if self._proc:
            self._proc.terminate()


# ── Brain session ─────────────────────────────────────────────────────────────

class DeepSeekBrain:
    def __init__(self):
        self.client = _get_openai_client()
        self._lock = threading.Lock()
        configure_executor(ALLOWED_ROOTS)
        self._claude_session = ClaudeExecutorSession()
        # Load summaries for long-range context (injected into system prompt)
        try:
            self._summaries = supabase_client.fetch_recent_summaries(n=5, channel="deepseek")
            log.info(f"[init] loaded {len(self._summaries)} summaries")
        except Exception as e:
            log.warning(f"[init] failed to load summaries: {e}")
            self._summaries = []
        # Seed recent raw turns for immediate conversational continuity
        try:
            self.history = supabase_client.fetch_recent_messages_as_turns(n=10, channel="deepseek")
            log.info(f"[init] history seeded: {len(self.history)} turns")
        except Exception as e:
            log.warning(f"[init] failed to seed history: {e}")
            self.history = []
        log.info(f"Brain ready. Model: {DEEPSEEK_MODEL}, allowed roots: {ALLOWED_ROOTS}")

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

    def maybe_summarize(self, channel: str = "deepseek") -> None:
        """Summarize the last N unsummarized messages if threshold reached. Non-blocking — call in thread."""
        try:
            last_ts = supabase_client.get_last_summary_time(channel)
            msgs = supabase_client.fetch_messages_since(last_ts, channel=channel, limit=SUMMARY_EVERY_N)
            if len(msgs) < SUMMARY_EVERY_N:
                return
            transcript = "\n".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'][:400]}"
                for m in msgs
            )
            prompt = (
                "Summarize the following conversation in 3-5 sentences. "
                "Be specific: mention file names, machine names, project names, decisions made, and tasks completed. "
                "Write in past tense as a factual record.\n\n" + transcript
            )
            response = self.client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                timeout=30,
            )
            summary_text = response.choices[0].message.content.strip()
            supabase_client.save_summary(channel, summary_text, len(msgs))
            # Refresh in-memory summaries so next system prompt includes this one
            self._summaries = supabase_client.fetch_recent_summaries(n=5, channel=channel)
            log.info(f"[summary] saved ({len(msgs)} msgs): {summary_text[:100]}")
        except Exception as e:
            log.warning(f"[summary] failed: {e}")

    def _run_claude_delegate(self, request: str, publish_text) -> str:
        """Delegate to the persistent Claude Code executor session."""
        # Redirect self-serviceable requests back to DeepSeek's own tools
        _req_lower = request.lower()
        _self_service_keywords = [
            'supabase', 'memory', 'messages', 'context', 'latest', 'recent',
            'conversation', 'history', 'what have we', 'pick up', 'recall',
            'food', 'tasks', 'goals', 'facts', 'insights', 'personal_tasks',
        ]
        if any(kw in _req_lower for kw in _self_service_keywords):
            return (
                "[redirect] This request is about memory or Supabase data - "
                "use the query_memory tool with an appropriate SQL SELECT instead of delegating to Claude. "
                "Example: SELECT content, role, created_at FROM messages ORDER BY created_at DESC LIMIT 50"
            )
        log.info(f"[delegate→Claude] {request[:120]}")
        try:
            result = self._claude_session.send(request, publish_text)
            try:
                supabase_client.save_message("user",      f"[delegate→Claude] {request}",       channel="relay_actions")
                supabase_client.save_message("assistant", f"[Claude→DeepSeek] {result[:2000]}", channel="relay_actions")
            except Exception as le:
                log.warning(f"Failed to log delegate to DB: {le}")
            return result
        except Exception as e:
            log.error(f"Claude delegate error: {e}")
            return f"[delegate error: {e}]"

    def _run_query_memory(self, sql: str) -> str:
        """Execute SQL against the memory database via supabase_client helpers."""
        import re as _re
        sql_stripped = sql.strip().upper()
        if not sql_stripped.startswith("SELECT"):
            return "[query_memory: only SELECT queries allowed]"

        # Route well-known queries to supabase_client helpers for reliability
        sql_lower = sql.strip().lower()

        # messages table → use fetch_recent_messages
        if _re.search(r"from\s+messages", sql_lower):
            limit_m = _re.search(r"limit\s+(\d+)", sql_lower)
            n = int(limit_m.group(1)) if limit_m else 50
            n = min(n, 200)
            try:
                result = supabase_client.fetch_recent_messages(n=n)
                log.info(f"[query_memory] messages: fetched n={n}, got {len(result or '')} chars")
                return result or "(no messages)"
            except Exception as e:
                return f"[query_memory error fetching messages: {e}]"

        # memory/facts table → use fetch_memory_context
        if _re.search(r"from\s+(memory|facts)", sql_lower):
            try:
                result = supabase_client.fetch_memory_context()
                log.info(f"[query_memory] memory: got {len(result or '')} chars")
                return result or "(no memory)"
            except Exception as e:
                return f"[query_memory error fetching memory: {e}]"

        # Generic fallback — PostgREST simple table fetch
        m = _re.search(r"from\s+(\w+)", sql_lower)
        if not m:
            return "[query_memory: could not parse table name from SQL]"
        table = m.group(1)
        if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
            return "[query_memory: database not configured]"
        import urllib.request
        try:
            limit_m = _re.search(r"limit\s+(\d+)", sql_lower)
            limit = int(limit_m.group(1)) if limit_m else 50
            req_url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?limit={limit}&order=created_at.desc"
            req = urllib.request.Request(req_url, headers={
                "apikey": config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                import json as _json
                rows = _json.loads(resp.read().decode())
                if not rows:
                    return f"(no rows in {table})"
                lines = []
                for r in rows[:50]:
                    lines.append(", ".join(f"{k}={v}" for k, v in r.items() if v is not None))
                log.info(f"[query_memory] {table}: {len(rows)} rows")
                return f"{table} ({len(rows)} rows):\n" + "\n".join(lines)
        except Exception as e:
            return f"[query_memory error: {e}]"

    RELAY_SOURCE_DIR = "/home/lynnkse/cognitive-hq/claude-telegram-relay"

    def _run_tool(self, name: str, args: dict, send_confirm, publish_text=None) -> str:
        if name == "load_skill":
            skill_name = args.get("name", "")
            log.info(f"[tool] load_skill: {skill_name}")
            return supabase_client.fetch_skill_by_name(skill_name)
        if name == "query_memory":
            return self._run_query_memory(args["sql"])
        if name == "delegate_to_claude":
            return self._run_claude_delegate(args["request"], publish_text or (lambda x: None))

        # Block read_file/list_dir on relay source files — brain should never read its own code
        if name == "read_file":
            path = args.get("path", "")
            BLOCKED_FILES = (
                "deepseek_brain.py", "session_manager.py", "telegram_node.py",
                "executor.py", "supabase_client.py", "config.py", "permission_hook.py",
            )
            if any(path.endswith(f) for f in BLOCKED_FILES):
                log.warning(f"[tool] blocked read_file on relay source: {path}")
                return (
                    "[blocked] Cannot read relay Python source files directly. "
                    "MD files (DEEPSEEK_ARCHITECTURE.md, TODO.md, LOG.md, etc.) are readable. "
                    "For memory/messages: use query_memory tool."
                )

        action = Action(id=f"{name}_{int(time.time())}", type=name, params=args)

        # Optional Haiku watchdog review before Telegram confirmation
        if WATCHDOG_ENABLED and name not in ("read_file", "list_dir"):
            safe, reason = _watchdog_review(action)
            if not safe:
                log.warning(f"Watchdog blocked {name}: {reason}")
                return f"[blocked by watchdog: {reason}]"

        result: Result = executor_execute(action, send_confirm)
        if result.success:
            return result.output or "done"
        return f"[error: {result.error}]"

    def chat(self, user_text: str, send_confirm, publish_text) -> str:
        """
        Process a user message. May call tools multiple times.
        send_confirm(action_id, summary) — sends Telegram confirmation request
        publish_text(text) — streams intermediate text back to user
        Returns final text response.
        """
        with self._lock:
            log.info(f"[chat] incoming ({len(user_text)} chars): {user_text[:120]!r}")
            self.history.append({"role": "user", "content": user_text})
            self._trim_history()

            sys_prompt = _build_system_prompt(self._summaries)
            messages = [{"role": "system", "content": sys_prompt}] + self.history
            log.info(f"[chat] sending {len(messages)} messages to DeepSeek (sys={len(sys_prompt)} chars, history={len(self.history)})")
            final_text = ""

            for round_num in range(MAX_TOOL_ROUNDS):
                response = self.client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    timeout=90,
                )
                msg = response.choices[0].message

                # Accumulate any text content — don't publish intermediate messages,
                # Telegram typing indicator already shows the user something is happening
                if msg.content:
                    final_text = msg.content

                # No tool calls → done
                if not msg.tool_calls:
                    break

                # Execute tool calls
                messages.append(msg)
                tool_results = []
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = json.loads(tc.function.arguments)
                    log.info(f"Tool call: {fn_name}({list(fn_args.keys())})")
                    output = self._run_tool(fn_name, fn_args, send_confirm, publish_text)
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": output[:8000],  # cap tool output
                    })

                messages.extend(tool_results)

            else:
                final_text = "[max tool rounds reached — stopping]"

            log.info(f"[chat] final response ({len(final_text)} chars): {final_text[:120]!r}")
            # Only save clean responses to history — skip error/timeout artifacts
            if final_text and not final_text.startswith(("[max tool", "[error", "[blocked", "[delegate error")):
                self.history.append({"role": "assistant", "content": final_text})
            else:
                # Still append something so the user turn has a pair, but mark it minimal
                self.history.append({"role": "assistant", "content": "(previous attempt failed — please retry)"})
            return final_text


# ── Socket server (same interface as session_manager.py) ─────────────────────

class BrainServer:
    def __init__(self):
        self.brain = DeepSeekBrain()
        self._response_subscribers: list[socket.socket] = []
        self._subs_lock = threading.Lock()
        # Buffer for responses that had no subscribers at publish time.
        # Delivered immediately when the next subscriber connects.
        self._pending_payloads: list[bytes] = []
        self._pending_lock = threading.Lock()

    def _publish(self, text: str, user_id: str):
        payload = json.dumps({"text": text, "source": "deepseek", "user_id": user_id}) + "\n"
        data = payload.encode()
        with self._subs_lock:
            if not self._response_subscribers:
                log.warning(f"_publish: no subscribers — buffering response ({len(data)} bytes)")
                with self._pending_lock:
                    self._pending_payloads.append(data)
                return
            log.info(f"_publish: {len(self._response_subscribers)} sub(s), bytes={len(data)}")
            dead = []
            for s in self._response_subscribers:
                try:
                    s.sendall(data)
                except Exception:
                    dead.append(s)
            for s in dead:
                self._response_subscribers.remove(s)
            # If all sends failed, buffer for the next subscriber
            if dead and not self._response_subscribers:
                log.warning(f"_publish: all subscribers dead — buffering response")
                with self._pending_lock:
                    self._pending_payloads.append(data)

    def _handle_response_subscriber(self, conn: socket.socket):
        # Flush any buffered responses to the new subscriber immediately
        with self._pending_lock:
            pending = list(self._pending_payloads)
            self._pending_payloads.clear()
        for data in pending:
            try:
                conn.sendall(data)
                log.info(f"Flushed buffered response to new subscriber ({len(data)} bytes)")
            except Exception as e:
                log.warning(f"Failed to flush buffered response: {e}")
        with self._subs_lock:
            self._response_subscribers.append(conn)
        # Keep connection open until client disconnects
        try:
            while conn.recv(1):
                pass
        except Exception:
            pass
        with self._subs_lock:
            if conn in self._response_subscribers:
                self._response_subscribers.remove(conn)

    def _handle_user_input(self, data: bytes):
        try:
            msg = json.loads(data.decode())
        except Exception:
            return

        # Handle executor confirmation responses (from Telegram inline buttons)
        if msg.get("type") == "confirm_response":
            from executor import resolve_confirmation
            action_id = msg.get("action_id", "")
            approved = bool(msg.get("approved", False))
            resolve_confirmation(action_id, approved)
            log.info(f"Confirmation resolved: action_id={action_id} approved={approved}")
            return

        text = msg.get("text", "").strip()
        user_id = str(msg.get("user_id", ""))
        if not text:
            return
        supabase_client.save_message("user", text, channel="deepseek")

        def send_confirm(action_id: str, summary: str):
            payload = json.dumps({"type": "confirm_request", "action_id": action_id, "summary": summary, "user_id": user_id}) + "\n"
            data = payload.encode()
            with self._subs_lock:
                dead = []
                for s in self._response_subscribers:
                    try:
                        s.sendall(data)
                    except Exception:
                        dead.append(s)
                for s in dead:
                    self._response_subscribers.remove(s)

        def publish_intermediate(text: str):
            self._publish(text, user_id)

        def run():
            try:
                response = self.brain.chat(text, send_confirm, publish_intermediate)
                supabase_client.save_message("assistant", response, channel="deepseek")
                self._publish(response, user_id)
                # Summarize in background if enough new messages have accumulated
                threading.Thread(target=self.brain.maybe_summarize, args=("deepseek",), daemon=True).start()
            except Exception as e:
                log.error(f"Brain error: {e}", exc_info=True)
                self._publish(f"[error: {e}]", user_id)

        threading.Thread(target=run, daemon=True).start()

    def _serve_user_input(self):
        sock_path = config.USER_INPUT_SOCK
        Path(sock_path).unlink(missing_ok=True)
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(5)
        log.info(f"Listening on {sock_path}")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._recv_conn, args=(conn,), daemon=True).start()

    def _recv_conn(self, conn: socket.socket):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle_user_input(line.strip())
        except Exception:
            pass
        finally:
            conn.close()

    def _serve_response_subscribers(self):
        sock_path = config.CLAUDE_RESPONSE_SOCK
        Path(sock_path).unlink(missing_ok=True)
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(10)
        log.info(f"Response socket: {sock_path}")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle_response_subscriber, args=(conn,), daemon=True).start()

    def run(self):
        if not DEEPSEEK_API_KEY:
            log.error("DEEPSEEK_API_KEY not set in .env")
            sys.exit(1)
        threading.Thread(target=self._serve_response_subscribers, daemon=True).start()
        self._serve_user_input()  # blocks


LOCK_FILE = "/tmp/cognitive-hq/deepseek_brain.lock"

def _acquire_exclusive_lock():
    import fcntl
    Path(LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another brain instance is already running (lock held). Exiting.")
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    log.info(f"Exclusive lock acquired (PID {os.getpid()})")
    return lock_fd  # keep fd open — released on process exit

if __name__ == "__main__":
    _lock_fd = _acquire_exclusive_lock()
    BrainServer().run()
