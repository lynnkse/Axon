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
DS_REASONER  = "deepseek-reasoner"   # R1 — reasoning/planning/analysis
HISTORY_FILE = Path(os.environ.get("RELAY_DIR", str(Path.home() / ".claude-relay"))) / "ds_history.json"
MAX_TURNS    = 40   # full turns kept before compression
KEEP_TURNS   = 20   # turns kept after compression

# Intent keywords that route to R1 (reasoning model)
_R1_KEYWORDS = [
    "why", "explain", "analyze", "analyse", "plan", "strategy", "design",
    "what should", "should i", "should we", "think about", "compare",
    "difference between", "pros and cons", "architecture", "approach",
    "how does", "how do", "what do you think", "evaluate", "assess",
    "recommend", "tradeoff", "trade-off", "decide", "decision",
    "understand", "what is the best", "what would", "is it better",
]

CLAUDE_REQUEST_RE = re.compile(r"<CLAUDE_REQUEST>(.*?)</CLAUDE_REQUEST>", re.DOTALL)

# ── DeepSeek tool definitions ─────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_memory",
            "description": "Query or write to the Supabase database. Use for food logging, task queries, memory facts, recent messages, roadmap, etc. Accepts SQL SELECT (any table) or INSERT/UPDATE on approved tables.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL query. SELECT from any table. INSERT/UPDATE allowed on: food_entries, fitness_log, roadmap, personal_tasks, frequent_foods."}
                },
                "required": ["sql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_ssh",
            "description": "Run a shell command on a remote machine via SSH. Use for reading files, listing directories, running scripts, checking ROS topics, catkin_make, git operations on aevadim-09 or other configured hosts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "SSH host alias or IP. Known hosts: aevadim-09 (anton@100.114.29.37), Leonid (anpl@100.98.191.76)"},
                    "command": {"type": "string", "description": "Shell command to run on the remote host"}
                },
                "required": ["host", "command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_local",
            "description": "Run a shell command locally on ROG (this machine). Use for file operations, git, starting/stopping processes, checking system state on ROG itself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run locally on ROG"}
                },
                "required": ["command"]
            }
        }
    }
]

def _run_ssh(host: str, command: str) -> str:
    import subprocess as _sp
    # Safety: block destructive commands
    danger = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
    if any(d in command for d in danger):
        return "[run_ssh: blocked dangerous command]"
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", host, command]
    try:
        r = _sp.run(ssh_cmd, capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        err = r.stderr.strip()
        result = out
        if err and not out:
            result = err
        elif err:
            result = out + "\n[stderr: " + err + "]"
        return result[:8000] or "(no output)"
    except _sp.TimeoutExpired:
        return "[run_ssh: timeout after 60s]"
    except Exception as e:
        return f"[run_ssh error: {e}]"


def _run_local(command: str) -> str:
    import subprocess as _sp
    danger = ["rm -rf /", "mkfs", "dd if=", ":(){"]
    if any(d in command for d in danger):
        return "[run_local: blocked dangerous command]"
    try:
        r = _sp.run(command, shell=True, capture_output=True, text=True, timeout=60,
                    cwd=str(Path.home() / "Axon"))
        out = r.stdout.strip()
        err = r.stderr.strip()
        result = out
        if err and not out:
            result = err
        elif err:
            result = out + "\n[stderr: " + err + "]"
        return result[:8000] or "(no output)"
    except _sp.TimeoutExpired:
        return "[run_local: timeout after 60s]"
    except Exception as e:
        return f"[run_local error: {e}]"


_WRITABLE_TABLES = {"food_entries", "fitness_log", "roadmap", "personal_tasks", "frequent_foods"}



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



def _run_query_memory(sql: str) -> str:
    import re as _re, json as _json, urllib.request, urllib.parse
    from datetime import date as _date
    sql_stripped = sql.strip().upper()
    sql_lower    = sql.strip().lower()

    if sql_stripped.startswith(("INSERT", "UPDATE")):
        table_m = _re.search(r"(?:into|update)\s+(\w+)", sql_lower)
        table   = table_m.group(1) if table_m else ""
        if table not in _WRITABLE_TABLES:
            return f"[query_memory: writes not allowed on '{table}']"
        if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
            return "[query_memory: DB not configured]"
        try:
            cols_m = _re.search(r"\((.*?)\)\s*values\s*\((.*?)\)", sql_lower, _re.DOTALL)
            if not cols_m:
                return "[query_memory: cannot parse INSERT]"
            cols = [c.strip().strip("'\"") for c in cols_m.group(1).split(",")]
            vals_m = _re.search(r"values\s*\((.*)\)", sql.strip(), _re.DOTALL | _re.IGNORECASE)
            if not vals_m:
                return "[query_memory: cannot parse INSERT values]"
            raw = vals_m.group(1)
            vals, cur, in_q, qc = [], "", False, None
            for ch in raw:
                if ch in ("'", '"') and not in_q:
                    in_q, qc = True, ch
                elif ch == qc and in_q:
                    in_q, qc = False, None
                    cur += ch; continue
                if ch == "," and not in_q:
                    vals.append(cur.strip()); cur = ""; continue
                cur += ch
            if cur.strip(): vals.append(cur.strip())
            row = {}
            for col, val in zip(cols, vals):
                v = val.strip().strip("'\"")
                if v.upper() in ("NULL", ""): continue
                if v.replace(".", "").replace("-", "").lstrip("-").isdigit():
                    row[col] = float(v) if "." in v else int(v)
                elif v.upper() == "CURRENT_DATE" or v.lower() in ("today", "now()"):
                    row[col] = str(_date.today())
                else:
                    row[col] = v
            payload = _json.dumps(row).encode()
            req_url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
            req = urllib.request.Request(req_url, data=payload, method="POST", headers={
                "apikey": config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                result = _json.loads(r.read().decode())
                return f"Inserted into {table}: {result}"
        except Exception as e:
            return f"[query_memory INSERT error: {e}]"

    if not sql_stripped.startswith("SELECT"):
        return "[query_memory: only SELECT/INSERT/UPDATE allowed]"

    if _re.search(r"from\s+messages", sql_lower):
        lim = _re.search(r"limit\s+(\d+)", sql_lower)
        n   = min(int(lim.group(1)) if lim else 50, 200)
        try:
            return supabase_client.fetch_recent_messages(n=n) or "(no messages)"
        except Exception as e:
            return f"[messages error: {e}]"

    if _re.search(r"from\s+(memory|facts)", sql_lower):
        try:
            return supabase_client.fetch_memory_context() or "(no memory)"
        except Exception as e:
            return f"[memory error: {e}]"

    m = _re.search(r"from\s+(\w+)", sql_lower)
    if not m:
        return "[query_memory: cannot parse table]"
    table = m.group(1)
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        return "[query_memory: DB not configured]"
    try:
        lim     = _re.search(r"limit\s+(\d+)", sql_lower)
        limit   = int(lim.group(1)) if lim else 50
        qs      = f"?limit={limit}&order=created_at.desc"
        req_url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}{qs}"
        req = urllib.request.Request(req_url, headers={
            "apikey": config.SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = _json.loads(r.read().decode())
            if not rows:
                return f"(no rows in {table})"
            return _json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"[query_memory SELECT error: {e}]"

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
        def _clean(ms):
            return [m for m in ms if m.get("role") != "tool" and not (m.get("role")=="assistant" and not m.get("content") and m.get("tool_calls"))]
        self.messages = [{"role": "user", "content": f"[Context summary: {summary}]"},
                         {"role": "assistant", "content": "Understood, I have that context."}] + _clean(recent)
        log.info(f"Compressed history to {len(self.messages)} turns")

    @staticmethod
    def _classify_intent(text: str) -> str:
        """Returns 'reasoner' (R1) or 'chat' (V3) based on message content."""
        t = text.lower()
        for kw in _R1_KEYWORDS:
            if kw in t:
                return "reasoner"
        return "chat"

    def chat(self, user_text: str, broadcaster: "Broadcaster | None" = None, user_id: str | None = None) -> str:
        self.messages.append({"role": "user", "content": user_text})
        if len(self.messages) > MAX_TURNS:
            self._compress()

        intent = self._classify_intent(user_text)
        use_reasoner = (intent == "reasoner")
        model  = DS_REASONER if use_reasoner else DS_MODEL
        label  = "R1🧠" if use_reasoner else "V3⚡"
        log.info(f"[router] intent={intent} → {model}")
        if broadcaster:
            broadcaster.broadcast(f"[⏳ {label} thinking...]", "status", user_id)

        reply = ""
        ephemeral_msgs: list = []  # tool exchanges — ephemeral, not persisted
        for _round in range(10):
            try:
                kwargs: dict = {
                    "model": model,
                    "messages": [{"role": "system", "content": self.system}] + self.messages + ephemeral_msgs,
                }
                # R1 does not support function calling — skip tools
                if not use_reasoner:
                    kwargs["tools"] = TOOLS
                    kwargs["tool_choice"] = "auto"

                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                reply = f"[DeepSeek error: {e}]"
                break

            msg = resp.choices[0].message

            # Stream R1 thinking tokens to broadcaster before final answer
            if use_reasoner and broadcaster:
                thinking = getattr(msg, "reasoning_content", None)
                if thinking and thinking.strip():
                    broadcaster.broadcast(f"[🧠 thinking]\n{thinking.strip()}", "thinking", user_id)
                    log.info(f"[R1 thinking] {thinking[:80]}")

            # No tool calls — final response
            if not msg.tool_calls:
                reply = msg.content or ""
                self.messages.append({"role": "assistant", "content": reply})
                # Prefix label on final answer
                reply = f"[{label}] {reply}"
                break

            # Tool calls present (V3 only) — execute each and feed results back
            tool_results = []
            for tc in msg.tool_calls:
                fn   = tc.function.name
                args = json.loads(tc.function.arguments)
                sql_preview = args.get("sql", "")[:120]
                if broadcaster:
                    broadcaster.broadcast(f"[🔧 {fn}] {sql_preview}", "tool_call", user_id)
                if fn == "query_memory":
                    result = _run_query_memory(args.get("sql", ""))
                elif fn == "run_ssh":
                    result = _run_ssh(args.get("host", ""), args.get("command", ""))
                elif fn == "run_local":
                    result = _run_local(args.get("command", ""))
                else:
                    result = f"[unknown tool: {fn}]"
                result_preview = str(result)[:200]
                log.info(f"[tool] {fn}({args}) → {str(result)[:120]}")
                if broadcaster:
                    broadcaster.broadcast(f"[🔧✓ {fn}] {result_preview}", "tool_result", user_id)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })
            # Do NOT persist tool msgs to history — causes 400 on reload
            ephemeral_msgs.append(msg.model_dump(exclude_unset=True))
            ephemeral_msgs.extend(tool_results)

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
        import os as _os
        claude_input  = config.SOCKET_DIR + "/claude_input.sock"
        if not _os.path.exists(claude_input):
            return "[Claude delegation unavailable — session_manager socket not found. Answer directly from your own knowledge.]"
        claude_output = config.SOCKET_DIR + "/claude_output.sock"
        try:
            with self._lock:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(claude_input)
                payload = (json.dumps({"text": request, "source": "orchestrator", "user_id": "system"}) + "\n").encode()
                s.sendall(payload)
                s.close()
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
            return f"[Claude delegation error: {e}. Answer directly from your own knowledge.]"


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
        from datetime import date as _today_date
        _today = str(_today_date.today())
        rules_text = ""
        try:
            r = _run_query_memory("SELECT content FROM rules ORDER BY created_at LIMIT 50")
            if r and "error" not in r.lower() and len(r) > 10:
                rules_text = "\n\nACTIVE RULES (verify before AND after each interaction):\n" + r
        except Exception:
            pass
        self._system = f"""You are Axon — an AI assistant powered by DeepSeek (NOT Claude, NOT GPT). Your underlying model is DeepSeek. You are running for {name} (timezone: {tz}).
Today's date is {_today}. Use this date when querying food_entries or any date-sensitive tables.{rules_text}
INTERACTION PROTOCOL:
- START of each interaction: if rules not in context, call query_memory("SELECT content FROM rules ORDER BY created_at LIMIT 50").
- END of each interaction: silently verify your response against active rules before sending.

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
        self.broadcaster.broadcast(f"[You] {text}", "user_echo", user_id)

        log.info(f"[DS] processing: {text[:80]}")
        ds_response = self.ds.chat(text, broadcaster=self.broadcaster, user_id=user_id)
        log.info(f"[DS] response: {ds_response[:120]}")

        claude_matches = CLAUDE_REQUEST_RE.findall(ds_response)
        clean_response = CLAUDE_REQUEST_RE.sub("", ds_response).strip()

        if claude_matches:
            if clean_response:
                self.broadcaster.broadcast(clean_response, "deepseek", user_id)

            for req in claude_matches:
                req = req.strip()
                self.broadcaster.broadcast(f"[CC→] {req[:300]}", "claude_request", user_id)
                log.info(f"[CC] delegating: {req[:80]}")
                result = self.claude.send(req)
                self.broadcaster.broadcast(f"[CC✓] {result[:500]}", "claude_result", user_id)
                log.info(f"[CC] result: {result[:80]}")
                self.ds.inject(f"[Claude result]: {result}")

            final = self.ds.chat("Summarize what was done and give the final result to the user.", broadcaster=self.broadcaster, user_id=user_id)
            self.broadcaster.broadcast(final, "deepseek", user_id)
        else:
            self.broadcaster.broadcast(ds_response, "deepseek", user_id)

    # ── Socket servers ────────────────────────────────────────────────────────

    def _serve_input(self):
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
        log.info(f"Orchestrator ready — V3: {DS_MODEL} | R1: {DS_REASONER}")
        self.broadcaster.broadcast("Axon ready. Dual-model active: V3⚡ for tasks, R1🧠 for reasoning.", "status")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    Orchestrator().run()
