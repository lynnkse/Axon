# DeepSeek Relay Architecture

**Status:** Implemented, not yet in production  
**Deadline:** 2026-06-15 (Anthropic billing model change)  
**Files:** `deepseek_brain.py`, `executor.py`, `start_relay_deepseek.sh`

---

## Motivation

On 2026-06-15, Anthropic changes its billing model: programmatic use (e.g. Claude Code, Claude CLI) loses subsidized tokens and becomes pay-per-token at ~$20/month cap. The previous relay used Claude as the reasoning brain, which would exhaust the budget quickly in a conversational assistant context.

**Solution:** Use DeepSeek V3/R1 as the reasoning brain (cheap, ~$0.14/M tokens) and Claude CLI only for trusted state-changing operations where its code execution capability is genuinely needed.

---

## Architecture Overview

```
Telegram
   │
   ▼
telegram_node.py ──── user_input.sock ────► deepseek_brain.py
                                                    │
                       claude_response.sock ◄────── │ publish_text / response
                              │                     │
                              ▼                  Tool calls:
                         Telegram               ┌──────────────────────────────┐
                           user                 │  read_file / list_dir        │
                                                │     (auto-approve)           │
                                                │                              │
                                                │  write_file / bash /         │
                                                │  delete_file                 │
                                                │    ├─ path/cmd validation    │
                                                │    ├─ Telegram confirm       │
                                                │    └─ executor.py runs it    │
                                                │                              │
                                                │  delegate_to_claude          │
                                                │    └─ claude --print         │
                                                │       (Claude CLI, stateful) │
                                                │                              │
                                                │  query_memory                │
                                                │    └─ Supabase REST API      │
                                                └──────────────────────────────┘
```

---

## Components

### `deepseek_brain.py` — Reasoning Brain

Drop-in replacement for `session_manager.py`. Same Unix socket interface.

- Connects to **DeepSeek API** (`https://api.deepseek.com`, OpenAI-compatible)
- Default model: `deepseek-chat` (V3). Switch to `deepseek-reasoner` for R1 on hard problems.
- Maintains rolling conversation history (last 40 messages)
- Loads Supabase memory + recent 20 messages as system prompt context
- Runs up to 10 tool-call rounds per user message before forcing a text response

**Tool definitions:**

| Tool | Description | Approval |
|------|-------------|----------|
| `read_file` | Read file contents | Auto |
| `list_dir` | List directory | Auto |
| `delegate_to_claude` | Run Claude CLI for state changes | Auto (Claude itself confirms) |
| `query_memory` | SQL SELECT against Supabase | Auto |
| `write_file` | Write/overwrite file | Telegram confirm |
| `bash` | Execute shell command | Telegram confirm |
| `delete_file` | Delete file | Telegram confirm |

**`delegate_to_claude` flow:**
1. DeepSeek calls `delegate_to_claude(request="plain-language description")`
2. Brain runs `claude --print [--resume <session_id>] <request>` as subprocess
3. Stdout streamed live to Telegram (`🔧 <line>` prefix)
4. Session ID extracted and saved to `/tmp/cognitive-hq/claude_executor_session.txt` for continuity across calls
5. Request + truncated response logged to Supabase `relay_actions` channel
6. Claude's output returned to DeepSeek as tool result

**Self-service redirect:** If `delegate_to_claude` is called with keywords like `memory`, `supabase`, `recent`, `tasks`, etc., the brain redirects DeepSeek back to `query_memory` instead, avoiding unnecessary Claude invocations.

---

### `executor.py` — Trusted Action Layer

Handles `write_file`, `bash`, `delete_file` with security guardrails.

**Path blocklist (always blocked regardless of allowed roots):**
- `.env`, `.env.*`
- `.ssh`, `.gnupg`, `id_rsa`, `id_ed25519`
- `credentials`, `secrets`
- `/etc/`, `/proc/`, `/sys/`
- `.git/config`

**Command blocklist (bash):**
- `rm -rf`
- `dd`, `mkfs`, `format`, `shred`
- Fork bombs (`:(){`)
- Curl/wget pipe-to-shell (`curl ... | bash`)

**Confirmation flow:**
1. Action hits executor → blocked patterns checked
2. `send_confirm(action_id, summary)` sends Telegram message with Approve/Deny buttons
3. Waits up to 60 seconds for user tap
4. `resolve_confirmation(action_id, approved)` called by Telegram handler
5. Executes on Approve, rejects on Deny or timeout

---

### Optional: WATCHDOG (Claude Haiku review)

When `WATCHDOG=1` in `.env`:
- Before Telegram confirmation is sent, Claude Haiku reviews the action
- Prompt: "Is this action safe and sensible for a developer assistant?"
- Returns `{"safe": bool, "reason": "..."}` 
- If Haiku says unsafe → action blocked without reaching user
- Default: disabled (`WATCHDOG=0`)

---

### `start_relay_deepseek.sh` — Process Manager

Starts three processes in order:
1. `deepseek_brain.py` — waits for socket to appear before continuing
2. `telegram_node.py` — connects to brain via Unix sockets
3. `proactive_node.py` — background check-ins

PID lock at `/tmp/cognitive-hq/relay_deepseek.pid`. Checks `DEEPSEEK_API_KEY` before starting.

---

## Configuration (`.env`)

```bash
DEEPSEEK_API_KEY=sk-...          # required
DEEPSEEK_MODEL=deepseek-chat     # V3 (default) or deepseek-reasoner (R1)
ALLOWED_ROOTS=/home/lynnkse/cognitive-hq,/home/lynnkse/.claude-relay
WATCHDOG=0                       # set to 1 to enable Haiku review
ANTHROPIC_API_KEY=sk-ant-...     # required only if WATCHDOG=1
```

---

## Cost Model

| Component | Usage | Cost |
|-----------|-------|------|
| DeepSeek V3 | All reasoning, planning, reading | ~$0.14/M input tokens |
| DeepSeek R1 | Complex reasoning (opt-in) | ~$0.55/M input tokens |
| Claude CLI | State changes only (via `delegate_to_claude`) | From $20/month budget |
| Claude Haiku | Watchdog review (optional) | Negligible |

DeepSeek handles ~95% of traffic. Claude is invoked only when the user needs a file written, code changed, or a shell command run.

---

## Comparison: Old vs New

| | Old relay (`session_manager.py`) | New relay (`deepseek_brain.py`) |
|--|--|--|
| Reasoning | Claude Sonnet | DeepSeek V3/R1 |
| State changes | Claude (same process) | Claude CLI via `delegate_to_claude` |
| Cost | High (burns $20 budget fast) | Low (DeepSeek cheap, Claude only for actions) |
| Security | Claude's built-in judgment | `executor.py` allowlist + blocklist + Telegram confirm |
| Session memory | In-process history | Rolling 40-message history + Supabase context |

---

## Start Command

```bash
ssh lynnkse@100.73.56.102
cd ~/cognitive-hq/claude-telegram-relay/relay_v2
bash start_relay_deepseek.sh
```

---

## TODO Before Production

- [ ] Test end-to-end: send Telegram message → DeepSeek response
- [ ] Test `delegate_to_claude` with a simple file write
- [ ] Test Telegram confirmation flow for `bash` action
- [ ] Verify session continuity (Claude resumes session across calls)
- [ ] Rotate DeepSeek API key if any concern about prior exposure
- [ ] Add `proactive_node.py` interval check (20-min Haiku check-ins per separate task)
