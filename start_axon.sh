#!/bin/bash
# start_axon.sh — bring up the full Axon stack
#
# Tmux sessions:  manager | telegram | cli | curator
# Background:     Streamlit dashboard (:8501) + ttyd CLI stream (:7681)

set -e

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$HOME/.virtualenvs/lynnkse/bin/python3.12"
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.9/bin/python3"
STREAMLIT="$HOME/.pyenv/versions/3.12.9/bin/streamlit"
TTYD="$AXON_DIR/ttyd"
LOG_DIR="$AXON_DIR/logs"

mkdir -p "$LOG_DIR" "$AXON_DIR/dashboard/logs"

# Use venv python if available, else pyenv
[[ -x "$PYTHON" ]] || PYTHON="$PYENV_PYTHON"

echo "Starting Axon with python: $PYTHON"

# ── Kill stale processes ─────────────────────────────────────────────────────
pkill -f "session_manager.py" 2>/dev/null || true
pkill -f "telegram_node.py"   2>/dev/null || true
pkill -f "cli_node.py"        2>/dev/null || true
pkill -f "curator.py"         2>/dev/null || true
pkill -f "streamlit run.*app.py" 2>/dev/null || true
pkill -f "ttyd.*tmux"         2>/dev/null || true
sleep 1

# ── Kill and recreate tmux sessions ─────────────────────────────────────────
for s in manager telegram cli curator; do
    tmux kill-session -t "$s" 2>/dev/null || true
done

# manager — brain / session manager
tmux new-session -d -s manager -x 220 -y 50 \
    "cd '$AXON_DIR' && $PYTHON session_manager.py 2>&1 | tee '$LOG_DIR/manager.log'"

# telegram — Telegram gateway
tmux new-session -d -s telegram -x 220 -y 50 \
    "cd '$AXON_DIR' && $PYTHON telegram_node.py 2>&1 | tee '$LOG_DIR/telegram.log'"

# cli — CLI node (also streamed via ttyd)
tmux new-session -d -s cli -x 220 -y 50 \
    "cd '$AXON_DIR' && $PYTHON cli_node.py 2>&1 | tee '$LOG_DIR/cli.log'"

# curator — knowledge maintenance (runs once/day, logs and sleeps)
tmux new-session -d -s curator -x 220 -y 50 \
    "cd '$AXON_DIR' && $PYTHON curator.py 2>&1 | tee '$LOG_DIR/curator.log'"

echo "Tmux sessions started: manager, telegram, cli, curator"

# ── Dashboard (background, no tmux pane needed — browser accessible) ─────────
nohup "$STREAMLIT" run "$AXON_DIR/dashboard/app.py" \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false \
    > "$AXON_DIR/dashboard/logs/streamlit.log" 2>&1 &
echo "Dashboard started → http://localhost:8501"

if [[ -x "$TTYD" ]]; then
    nohup "$TTYD" \
        --port 7681 \
        --interface 0.0.0.0 \
        --writable \
        tmux attach-session -t cli \
        > "$AXON_DIR/dashboard/logs/ttyd.log" 2>&1 &
    echo "CLI stream started → http://localhost:7681"
fi

# ── Print access URLs ────────────────────────────────────────────────────────
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "<tailscale-ip>")
echo ""
echo "╔══════════════════════════════════════╗"
echo "║        Axon stack is running         ║"
echo "╠══════════════════════════════════════╣"
echo "║  Dashboard:  http://$TAILSCALE_IP:8501"
echo "║  CLI stream: http://$TAILSCALE_IP:7681"
echo "║  Sessions:   manager | telegram | cli | curator"
echo "╚══════════════════════════════════════╝"
echo ""
echo "Attach: tmux attach -t manager"
