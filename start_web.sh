#!/bin/bash
# start_web.sh — bring up just the web dashboard (Streamlit + CLI/RALPH ttyd streams)
#
# Standalone version of start_axon.sh's "web" pane, for running by hand in a
# single tmux pane without the full 5-pane orchestration. Same portability
# pattern: honors AXON_PYTHON_DIR from .env for the venv, falls back to
# ROG's original hardcoded paths.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$AXON_DIR/.env" ]] && set -a && source "$AXON_DIR/.env" && set +a

if [[ -n "$AXON_PYTHON_DIR" ]]; then
    STREAMLIT="$AXON_PYTHON_DIR/streamlit"
else
    STREAMLIT="$HOME/.pyenv/versions/3.12.9/bin/streamlit"
fi
TTYD="$AXON_DIR/ttyd"
DASH_LOG_DIR="$AXON_DIR/dashboard/logs"
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo '127.0.0.1')"

mkdir -p "$DASH_LOG_DIR"
cd "$AXON_DIR"

echo "Stopping any existing dashboard/ttyd..."
pkill -f 'streamlit run.*app.py' 2>/dev/null
pkill -f 'ttyd.*tmux' 2>/dev/null
sleep 1

echo "Starting Streamlit dashboard on :8501..."
nohup "$STREAMLIT" run "$AXON_DIR/dashboard/app.py" \
  --server.port 8501 --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false \
  > "$DASH_LOG_DIR/streamlit.log" 2>&1 &

# CLI/RALPH view sessions — mirror sessions ttyd attaches to. Require the
# real "cli" / "ralph" tmux sessions to already exist under those exact names.
tmux new-session -d -s browser_view -t cli 2>/dev/null || echo "  (no 'cli' tmux session found yet — browser_view not created)"
tmux new-session -d -s ralph_view   -t ralph 2>/dev/null || echo "  (no 'ralph' tmux session found yet — ralph_view not created)"

if [[ -x "$TTYD" ]]; then
    echo "Starting ttyd CLI stream on :7681..."
    nohup "$TTYD" --port 7681 --interface 0.0.0.0 --writable \
      /usr/bin/tmux attach-session -t browser_view > "$DASH_LOG_DIR/ttyd.log" 2>&1 &
    echo "Starting ttyd RALPH stream on :7682..."
    nohup "$TTYD" --port 7682 --interface 0.0.0.0 --writable \
      /usr/bin/tmux attach-session -t ralph_view > "$DASH_LOG_DIR/ttyd_ralph.log" 2>&1 &
else
    echo "  ttyd binary not found/executable at $TTYD — skipping CLI/RALPH streams"
fi

sleep 1
echo
echo "Dashboard:   http://$TAILSCALE_IP:8501"
echo "CLI stream:  http://$TAILSCALE_IP:7681"
echo "RALPH stream: http://$TAILSCALE_IP:7682"
