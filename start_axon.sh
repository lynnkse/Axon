#!/bin/bash
# start_axon.sh — bring up the full Axon stack from a clean slate
#
# manager | telegram | cli | curator | web
#
# Always starts by running shutdown_axon.sh to kill every known Axon process
# and tmux session first, then brings everything back up fresh. This trades
# the old "leave healthy sessions alone" behavior for a simpler, more
# predictable full-restart-every-time model, per explicit request.
#
# Idempotent and safe to re-run any number of times — never crashes, never
# duplicates, always ends in a known-good state.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$AXON_DIR/shutdown_axon.sh" ]]; then
    "$AXON_DIR/shutdown_axon.sh"
    sleep 1
else
    echo "WARNING: shutdown_axon.sh not found at $AXON_DIR — skipping cleanup step."
fi

# Source .env for shell-level overrides (e.g. AXON_PYTHON_DIR) — the Python
# processes load .env themselves via python-dotenv, but this launcher script
# needs it too since it picks the interpreter before Python ever runs.
[[ -f "$AXON_DIR/.env" ]] && set -a && source "$AXON_DIR/.env" && set +a
# Multi-machine (2026-08-09): AXON_PYTHON_DIR lets each deployment point at its
# own venv (e.g. aevadim-09's pyenv-virtualenv "claude-relay") without editing
# this script. Set it in .env or export before running. Falls back to ROG's
# original hardcoded paths for backward compat.
if [[ -n "$AXON_PYTHON_DIR" ]]; then
    VENV_PYTHON="$AXON_PYTHON_DIR/python3"
    STREAMLIT="$AXON_PYTHON_DIR/streamlit"
else
    VENV_PYTHON="$HOME/.virtualenvs/lynnkse/bin/python3.12"
    STREAMLIT="$HOME/.pyenv/versions/3.12.9/bin/streamlit"
fi
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.9/bin/python3"
TTYD="$AXON_DIR/ttyd"
LOG_DIR="$AXON_DIR/logs"
DASH_LOG_DIR="$AXON_DIR/dashboard/logs"

mkdir -p "$LOG_DIR" "$DASH_LOG_DIR"

# Use venv python if available, else pyenv
[[ -x "$VENV_PYTHON" ]] && PYTHON="$VENV_PYTHON" || PYTHON="$PYENV_PYTHON"
echo "Using python: $PYTHON"

# ── Helper: check whether the process we expect is alive ─────────────────────
# Each Axon service is a singleton by design (only one instance should ever
# run system-wide), so a global pgrep for the pattern is sufficient — no need
# to walk tmux pane process trees, which is fragile across shell/exec nesting.
# match_pattern: a string that uniquely identifies this service's process
#                (e.g. the script filename) in `ps -f` output.
is_session_healthy() {
    local session="$1"
    local match_pattern="$2"

    if ! tmux has-session -t "$session" 2>/dev/null; then
        return 1
    fi

    pgrep -f "$match_pattern" >/dev/null 2>&1
}

# ── Helper: ensure session exists and process is healthy, fixing only what's broken ──
ensure_session() {
    local session="$1"
    local match_pattern="$2"
    local cmd="$3"

    if is_session_healthy "$session" "$match_pattern"; then
        echo "Session '$session' already healthy — leaving it alone."
        return 0
    fi

    if tmux has-session -t "$session" 2>/dev/null; then
        echo "Session '$session' exists but process not detected — restarting process in place..."
        # Interrupt whatever's stuck (best-effort), clear the input line, then send the real command.
        tmux send-keys -t "$session" C-c
        sleep 0.3
        tmux send-keys -t "$session" C-c
        sleep 0.3
        tmux send-keys -t "$session" C-u  # clear any partial input on the line
        sleep 0.2
    else
        echo "Session '$session' not found — creating..."
        tmux new-session -d -s "$session" -x 220 -y 50
    fi

    tmux send-keys -t "$session" "$cmd" Enter
    sleep 1

    if is_session_healthy "$session" "$match_pattern"; then
        echo "  -> '$session' started and confirmed healthy."
    else
        echo "  -> WARNING: '$session' started but process not yet detected (may still be initializing)."
    fi
}

# ── 1. manager — session manager / brain ─────────────────────────────────────
ensure_session manager "session_manager.py" \
    "cd '$AXON_DIR' && $PYTHON -u session_manager.py 2>&1 | tee '$LOG_DIR/manager.log'"

# ── 2. telegram — Telegram gateway ───────────────────────────────────────────
ensure_session telegram "telegram_node.py" \
    "cd '$AXON_DIR' && $PYTHON -u telegram_node.py 2>&1 | tee '$LOG_DIR/telegram.log'"

# ── 3. cli — CLI node (dashboard's CLI tab connects directly to the same
#            display.sock/cli_input.sock via web_cli_bridge.py, no tmux/ttyd) ──
ensure_session cli "cli_node.py" \
    "cd '$AXON_DIR' && $PYTHON -u cli_node.py 2>&1 | tee '$LOG_DIR/cli.log'"

# ── 4. curator — daily knowledge maintenance ──────────────────────────────────
ensure_session curator "curator.py" \
    "cd '$AXON_DIR' && $PYTHON -u curator.py 2>&1 | tee '$LOG_DIR/curator.log'"

# ── 5. web — Streamlit dashboard + web_cli_bridge (direct socket bridge) ─────
# Both background processes are managed here; pane shows their combined status.
# CLI tab connects straight to session_manager's display.sock/cli_input.sock
# via web_cli_bridge.py's WebSocket relay -- no tmux/ttyd involved anymore.
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo '<tailscale-ip>')"

WEB_CMD="$(cat <<WEBCMD
pkill -f 'streamlit run.*app.py' 2>/dev/null; pkill -f 'ttyd.*tmux' 2>/dev/null; pkill -f 'web_cli_bridge.py' 2>/dev/null; sleep 1
nohup $STREAMLIT run '$AXON_DIR/dashboard/app.py' \\
  --server.port 8501 --server.address 0.0.0.0 \\
  --server.headless true --browser.gatherUsageStats false \\
  > '$DASH_LOG_DIR/streamlit.log' 2>&1 &
nohup $PYTHON -u '$AXON_DIR/web_cli_bridge.py' > '$DASH_LOG_DIR/web_cli_bridge.log' 2>&1 &
echo 'Dashboard: http://$TAILSCALE_IP:8501  (CLI tab connects directly to session_manager -- full read+write, no tmux/ttyd)'
tail -f '$DASH_LOG_DIR/streamlit.log'
WEBCMD
)"

# web session's health check is based on the streamlit process specifically,
# since that pane runs a compound command (pkill+nohup+tail) not one script.
ensure_session web "streamlit run.*app.py" "$WEB_CMD"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           Axon stack status                  ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Sessions:   manager | telegram | cli         ║"
echo "║              curator | web                    ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Dashboard:  http://$TAILSCALE_IP:8501"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Attach to any session:  tmux attach -t <name>"
echo "List all sessions:      tmux ls"
