#!/bin/bash
# Axon Dashboard launcher
# Starts: Streamlit dashboard (:8501) + ttyd CLI stream (:7681)
# Access via Tailscale: http://<ROG-tailscale-ip>:<port>

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$HOME/.pyenv/versions/3.12.9/bin/python3"
STREAMLIT="$HOME/.pyenv/versions/3.12.9/bin/streamlit"
TTYD="$AXON_DIR/ttyd"
LOG_DIR="$AXON_DIR/dashboard/logs"

mkdir -p "$LOG_DIR"

echo "Starting Axon Dashboard..."

# Kill any existing instances
pkill -f "streamlit run.*app.py" 2>/dev/null
pkill -f "ttyd.*tmux" 2>/dev/null
sleep 1

# Start Streamlit dashboard
nohup "$STREAMLIT" run "$AXON_DIR/dashboard/app.py" \
  --server.port 8501 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false \
  > "$LOG_DIR/streamlit.log" 2>&1 &
echo "Dashboard started (PID $!) → http://localhost:8501"

# Start ttyd CLI stream (attaches to manager tmux session)
if [ -f "$TTYD" ]; then
  nohup "$TTYD" \
    --port 7681 \
    --interface 0.0.0.0 \
    --writable \
    tmux attach-session -t manager \
    > "$LOG_DIR/ttyd.log" 2>&1 &
  echo "CLI stream started (PID $!) → http://localhost:7681"
else
  echo "ttyd not found at $TTYD — skipping CLI stream"
fi

echo ""
echo "Tailscale access:"
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "<tailscale-ip>")
echo "  Dashboard: http://$TAILSCALE_IP:8501"
echo "  CLI:       http://$TAILSCALE_IP:7681"
