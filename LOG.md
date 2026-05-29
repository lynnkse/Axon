# Relay v2 — Dev Log

## 2026-04-07 — Permission deadlock solved + concurrent_updates fix

### Problem
When a message came from Telegram (photo/text), tapping Allow/Deny on the inline keyboard did nothing. The same buttons worked when the message originated from CLI.

### Root cause
python-telegram-bot processes updates from the same chat **sequentially** by default. While `on_photo` (or any handler) was blocking on `await _wait_for_response(...)`, the callback query update (button tap) from the same chat was queued behind it. `on_photo` couldn't finish without permission; the callback couldn't run; classic deadlock.

CLI worked because no Telegram handler was blocking — callback ran freely.

### Fix
Added `concurrent_updates(True)` to the Application builder in `telegram_node.py`:
```python
app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).build()
```

### Also built this session (from previous context)
- `permission_hook.py` — PermissionRequest hook that forwards to permission.sock, waits for decision, returns allow/deny JSON to Claude Code
- `claude_relay_wrapper.sh` — sets `CLAUDE_RELAY_SESSION=1` before exec'ing Claude; used as CLAUDE_PATH so relay's Claude process has the env var, distinguishing it from interactive sessions
- `.claude/settings.json` — registers PermissionRequest hook at project level
- `session_manager.py` — permission.sock server, `_handle_permission_conn`, `_resolve_permission`, `_publish_permission_request`
- `telegram_node.py` — `_permission_dispatcher` background task, `on_permission_callback`, inline Allow/Deny keyboard

### Known issue
`CLAUDE_RELAY_SESSION=1` leaks into interactive terminal sessions if started from the same shell as the relay. Need to ensure wrapper-set env var doesn't pollute interactive Claude Code sessions. Mitigation: use a separate terminal or `unset CLAUDE_RELAY_SESSION` before interactive use.

## 2026-04-08 — Telegram slash commands + permission hook fallback fix

### Slash commands implemented
Intercepted via python-telegram-bot `CommandHandler` before reaching Claude PTY.
All read local files directly — no Claude involvement, instant replies.

- `/help` — lists commands
- `/status` — relay health (SessionManager PID, session ID, sockets)
- `/usage` — 5h window + 7-day totals from session JSONL, timezone-aware reset times (USER_TIMEZONE), countdown ("in 2h 15m"), % display when USAGE_*_LIMIT set in .env
- `/model` — current Claude model from session JSONL
- `/clear` — deletes session ID file (fresh session on next restart)

**Bug found during /usage:** usage data is in `obj['message']['usage']`, not `obj['usage']`. Fixed.

**Usage limits:** set `USAGE_5H_LIMIT` and `USAGE_WEEK_LIMIT` in .env for percentage display. Limits are enforced server-side by Anthropic, not stored locally.

### Permission hook fallback fix
When `permission.sock` is unreachable (relay not running), hook now exits 1 so Claude Code falls through to its TUI instead of auto-denying all tool calls.

### Architecture clarification
This Claude (the interactive assistant) IS the relay's Claude — the same process receives messages from both Telegram and CLI. `CLAUDE_RELAY_SESSION=1` is correctly set; permission requests from tool use route through Telegram for approval.

## 2026-04-08 — Auto-allow safe permissions

Added `_auto_decision()` to `permission_hook.py`. Runs before connecting to `permission.sock`, so safe actions never reach Telegram.

**Auto-allowed:** Read/Glob/Grep, WebFetch/WebSearch, Edit/Write within `~` (except sensitive paths like ~/.ssh, ~/.aws), Bash commands without dangerous patterns.

**Always asks:** `sudo`, `rm -rf`, `git reset --hard`, `git push --force`, `dd`, `mkfs`, writes outside home dir or to sensitive paths, unknown tools.

**Kill switch:** `CLAUDE_AUTO_ALLOW=0` in environment disables all auto-allow.

**Bug fixed:** heredoc content containing dangerous-looking strings (e.g. "git reset --hard" in a log entry) was incorrectly triggering the ask-user path. Now only the first line (the actual shell command) is pattern-matched.

Logged to `/tmp/permission_hook.log` with `AUTO-ALLOW:` prefix for audit trail.

## 2026-04-08 — Fix missing final Telegram response after multi-tool runs

**Symptom:** After multiple permission approvals, CLI showed the summary but Telegram received nothing.

**Root cause:** `_RESPONSE_TIMEOUT` was 180s. Multi-tool runs with several 30-60s permission prompts exceeded this, causing the JSONL poller to time out and return a timeout message (or the actual response arrived after the poller had already given up).

**Fix 1:** Raised `_RESPONSE_TIMEOUT` to 600s (10 minutes).

**Fix 2:** Added `_STALL_FALLBACK` (30s): if the JSONL file stops growing but the last entry is not "text" type, return the best text we have rather than waiting for deadline. Handles edge case where Claude finishes without a closing text message.

## 2026-04-10 — Supabase persistence wired up + validated

### What was done
- Restored paused Supabase project (`jcwdfuusolpxnciqgstl`, `eu-central-1`). Schema was already applied from a previous session — `messages`, `memory`, `logs` tables with vector extension, RLS, and helper functions all present.
- Built `relay_v2/supabase_client.py`: fire-and-forget REST writes using stdlib `urllib` (no new dependencies). All writes run in daemon threads so the relay is never blocked on DB I/O.
- Hooked into `session_manager.py` at two points:
  - User message enters queue → saved to `messages` (role: user, channel: source)
  - Claude response received → tags parsed, response saved to `messages` (role: assistant), tags saved to `memory`
- Memory tag parsing: `[REMEMBER: ...]` → fact, `[GOAL: ... | DEADLINE: ...]` → goal, `[DONE: ...]` → completed_goal. Tags stripped from text before delivery to Telegram (user never sees them).

### Validated
Live test confirmed both directions writing correctly to Supabase:
- "Check now, i restarted it" (user, telegram) → saved ✓
- Full assistant response (assistant, telegram) → saved clean, no tags ✓

### Architecture note: what is NOT yet wired
- Embeddings: `memory` and `messages` tables have `embedding VECTOR(1536)` column but it is empty. Requires Supabase Edge Function (`embed`) + OpenAI API key secret + database INSERT webhook. Not set up yet.
- Claude reading from memory: nothing feeds past context back into Claude's system prompt yet. Memory is write-only at this stage.
- CLI messages: CLI keyboard input bypasses the queue and goes straight to PTY — not saved to Supabase. Only Telegram and other socket-based frontends are persisted.

### Design decision: embeddings on memory only
Raw message history grows fast and degrades semantic search quality. Decision: when embeddings are set up, apply them only to the `memory` table (facts, goals, preferences) not to `messages`. This keeps the search index small and high-signal. Full dreaming mode consolidation (see `DREAMING_MODE.md`) is the long-term path for turning raw history into durable knowledge.

## 2026-04-11 — Memory injection validated end-to-end

- Deployed `embed` Edge Function to Supabase, stored OpenAI API key as secret
- Database webhooks already existed (`embed_memory`, `embed_messages`) — both firing correctly
- Added `supabase_client.fetch_memory_context()` — fetches facts, preferences, goals at startup
- `_build_system_prompt()` now injects long-term memory into Claude's system prompt on every session spawn
- **Validated:** sent "do you know anything about me?" from Telegram — Claude responded with "Lynn likes to travel" (from memory table) without being told in that session. Full loop confirmed working:
  `[REMEMBER: tag] → memory table → embedded → injected at next startup → Claude knows it`

### Added to TODO
- Dreaming mode / memory consolidation R&D (see `DREAMING_MODE.md`)
- GLM-Z1-32B evaluation for heavy coding tasks as free sub-agent under Claude supervision
