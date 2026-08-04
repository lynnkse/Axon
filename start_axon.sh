#!/bin/bash
# start_axon.sh — bring up / restart the full Axon stack
#
# 5 tmux sessions: manager | telegram | cli | curator | web
#
# Logic per session:
#   - Already exists → leave it open, kill what's running, restart the process
#   - Doesn't exist  → create it, start the process
#
# Never kills sessions. Always ends with all 5 sessions alive and processes running.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$HOME/.virtualenvs/lynnkse/bin/python3.12"
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.9/bin/python3"
STREAMLIT="$HOME/.pyenv/versions/3.12.9/bin/streamlit"
TTYD="$AXON_DIR/ttyd"
LOG_DIR="$AXON_DIR/logs"
DASH_LOG_DIR="$AXON_DIR/dashboard/logs"

mkdir -p "$LOG_DIR" "$DASH_LOG_DIR"

# Use venv python if available, else pyenv
[[ -x "$VENV_PYTHON" ]] && PYTHON="$VENV_PYTHON" || PYTHON="$PYENV_PYTHON"
echo "Using python: $PYTHON"

# ── Helper: ensure session exists, kill current process, run new command ──────
start_in_session() {
    local session="$1"
    local cmd="$2"

    if tmux has-session -t "$session" 2>/dev/null; then
        echo "Session '$session' exists — restarting process..."
        # Interrupt whatever is running (two C-c for safety)
        tmux send-keys -t "$session" "" ""      # send Ctrl-C
        sleep 0.3
        tmux send-keys -t "$session" "" ""
        sleep 0.3
        # Clear the prompt line
        tmux send-keys -t "$session" "q" Enter 2>/dev/null
        sleep 0.4
    else
        echo "Session '$session' not found — creating..."
        tmux new-session -d -s "$session" -x 220 -y 50
    fi

    tmux send-keys -t "$session" "$cmd" Enter
    echo "  → '$session' started"
}

# ── 1. manager — session manager / brain ─────────────────────────────────────
start_in_session manager \
    "cd '$AXON_DIR' && $PYTHON -u session_manager.py 2>&1 | tee '$LOG_DIR/manager.log'"

# ── 2. telegram — Telegram gateway ───────────────────────────────────────────
start_in_session telegram \
    "cd '$AXON_DIR' && $PYTHON -u telegram_node.py 2>&1 | tee '$LOG_DIR/telegram.log'"

# ── 3. cli — CLI node (also streamed via ttyd to browser) ────────────────────
start_in_session cli \
    "cd '$AXON_DIR' && $PYTHON -u cli_node.py 2>&1 | tee '$LOG_DIR/cli.log'"

# ── 4. curator — daily knowledge maintenance ──────────────────────────────────
start_in_session curator \
    "cd '$AXON_DIR' && $PYTHON -u curator.py 2>&1 | tee '$LOG_DIR/curator.log'"

# ── 5. ralph — autonomous execution agent ─────────────────────────────────────
start_in_session ralph \
    "cd '$AXON_DIR' && $PYTHON -u ralph_node.py 2>&1 | tee '$LOG_DIR/ralph.log'"

# ── 6. web — Streamlit dashboard + ttyd CLI stream ───────────────────────────
# Both background processes are managed here; pane shows their combined status.
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo '<tailscale-ip>')"

WEB_CMD="$(cat <<WEBCMD
pkill -f 'streamlit run.*app.py' 2>/dev/null; pkill -f 'ttyd.*tmux' 2>/dev/null; sleep 1
nohup $STREAMLIT run '$AXON_DIR/dashboard/app.py' \\
  --server.port 8501 --server.address 0.0.0.0 \\
  --server.headless true --browser.gatherUsageStats false \\
  > '$DASH_LOG_DIR/streamlit.log' 2>&1 &
/usr/bin/tmux new-session -d -s browser_view -t cli 2>/dev/null || true
/usr/bin/tmux new-session -d -s ralph_view   -t ralph 2>/dev/null || true
[[ -x '$TTYD' ]] && nohup '$TTYD' --port 7681 --interface 0.0.0.0 --writable \\
  /usr/bin/tmux attach-session -t browser_view > '$DASH_LOG_DIR/ttyd.log' 2>&1 &
[[ -x '$TTYD' ]] && nohup '$TTYD' --port 7682 --interface 0.0.0.0 --writable \\
  /usr/bin/tmux attach-session -t ralph_view > '$DASH_LOG_DIR/ttyd_ralph.log' 2>&1 &
echo 'Dashboard: http://$TAILSCALE_IP:8501  |  CLI stream: http://$TAILSCALE_IP:7681'
tail -f '$DASH_LOG_DIR/streamlit.log'
WEBCMD
)"

start_in_session web "$WEB_CMD"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           Axon stack is running              ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Sessions:   manager | telegram | cli         ║"
echo "║              curator | ralph | web           ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Dashboard:  http://$TAILSCALE_IP:8501"
echo "║  CLI stream: http://$TAILSCALE_IP:7681"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Attach to any session:  tmux attach -t <name>"
echo "List all sessions:      tmux ls"
