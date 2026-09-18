#!/bin/bash
# start_axon.sh — bring up the full Axon stack from a clean slate
#
# manager | telegram | cli | curator | usage | web
#
# Always starts by running shutdown_axon.sh to kill every known Axon process
# and tmux session first, then brings everything back up fresh. This trades
# the old "leave healthy sessions alone" behavior for a simpler, more
# predictable full-restart-every-time model, per explicit request.
#
# Idempotent and safe to re-run any number of times — never crashes, never
# duplicates, always ends in a known-good state.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLEAR_CODEX_SESSION=0
for arg in "$@"; do
    case "$arg" in
        --clear)
            CLEAR_CODEX_SESSION=1
            ;;
        -h|--help)
            echo "Usage: $0 [--clear]"
            echo "  --clear  Start Codex in a new thread instead of resuming the saved thread."
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $arg" >&2
            echo "Usage: $0 [--clear]" >&2
            exit 2
            ;;
    esac
done

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

# The Codex session manager normally resumes the thread ID saved here. Clearing
# only this pointer makes the next manager launch create a genuinely new Codex
# thread; the old rollout remains on disk and can still be inspected manually.
if (( CLEAR_CODEX_SESSION )); then
    AXON_RELAY_DIR="${RELAY_DIR:-$HOME/.claude-relay}"
    SAVED_CODEX_THREAD_ID="$AXON_RELAY_DIR/codex_thread_id"
    SAVED_CODEX_THREAD_REGISTRY="$AXON_RELAY_DIR/codex_thread_registry.json"
    rm -f -- "$SAVED_CODEX_THREAD_ID" "$SAVED_CODEX_THREAD_REGISTRY"
    echo "Cleared saved Codex thread ID; starting a new Codex session."
fi

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

# ── Helper: launch a service without an interactive login shell ────────────
#
# Do not create an empty tmux shell and inject the command with send-keys.  On
# this host interactive shell startup can take several seconds; sending during
# that window races with .bashrc and can leave the command merely echoed or
# queued.  It also lets unrelated shell-startup side effects interfere with
# Axon.  Running bash directly makes the service command the pane's process and
# deliberately skips all profile/rc files.
ensure_session() {
    local session="$1"
    local match_pattern="$2"
    local cmd="$3"

    if is_session_healthy "$session" "$match_pattern"; then
        echo "Session '$session' already healthy — leaving it alone."
        return 0
    fi

    if tmux has-session -t "$session" 2>/dev/null; then
        echo "Session '$session' exists but process not detected — recreating..."
        tmux kill-session -t "$session"
    else
        echo "Session '$session' not found — creating..."
    fi

    tmux new-session -d -s "$session" -x 220 -y 50 \
        /bin/bash --noprofile --norc -o pipefail -c "$cmd"

    local attempt
    for attempt in {1..30}; do
        if is_session_healthy "$session" "$match_pattern"; then
            echo "  -> '$session' started and confirmed healthy."
            return 0
        fi
        sleep 0.5
    done

    echo "  -> WARNING: '$session' did not become healthy within 15 seconds."
    return 1
}

# ── 1. manager — session manager / brain ─────────────────────────────────────
# Keep the surrounding Axon interfaces identical while allowing the engine
# owner behind their shared socket contract to change per deployment.
if [[ "${AXON_ENGINE:-claude}" == "codex" ]]; then
    ensure_session manager "session_manager_codex.py" \
        "cd '$AXON_DIR' && $PYTHON -u session_manager_codex.py 2>&1 | tee '$LOG_DIR/manager.log'"
else
    ensure_session manager "session_manager.py" \
        "cd '$AXON_DIR' && $PYTHON -u session_manager.py 2>&1 | tee '$LOG_DIR/manager.log'"
fi

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

# ── 5. usage — deterministic hourly Codex quota monitor ─────────────────────
ensure_session usage "codex_usage_monitor.py" \
    "cd '$AXON_DIR' && $PYTHON -u codex_usage_monitor.py 2>&1 | tee '$LOG_DIR/codex_usage_monitor.log'"

# ── 6. web — Streamlit dashboard + web_cli_bridge (direct socket bridge) ─────
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
echo "║              curator | usage | web            ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Dashboard:  http://$TAILSCALE_IP:8501"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Attach to any session:  tmux attach -t <name>"
echo "List all sessions:      tmux ls"
