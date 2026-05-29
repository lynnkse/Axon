#!/bin/bash
# Start Irina's AutoCAD bot (second persistent Claude session)
# Runs session_manager + telegram_node with Irina-specific config.
#
# Usage:
#   ./start_irina.sh          # foreground, Ctrl+C to stop
#   ./start_irina.sh --tmux   # launch in a new tmux window

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load shared settings first (.env has SUPABASE_URL, CLAUDE_PATH, GROQ_API_KEY, etc.)
set -a
# shellcheck disable=SC1090
source "$PROJECT_ROOT/.env" 2>/dev/null || true

# Override with Irina-specific settings
# shellcheck disable=SC1090
source "$PROJECT_ROOT/irina.env"
set +a

# Create runtime dirs
mkdir -p "$SOCKET_DIR"
mkdir -p "$RELAY_DIR"
mkdir -p "$PROJECT_DIR"

echo "[irina] Starting AutoCAD session for Irina"
echo "[irina] SOCKET_DIR=$SOCKET_DIR"
echo "[irina] RELAY_DIR=$RELAY_DIR"
echo "[irina] PROJECT_DIR=$PROJECT_DIR"
echo "[irina] PROFILE_PATH=$PROFILE_PATH"

if [[ "${1:-}" == "--tmux" ]]; then
    SESSION_NAME="irina-autocad"
    tmux new-session -d -s "$SESSION_NAME" -x 220 -y 50 2>/dev/null || true
    tmux send-keys -t "$SESSION_NAME" "cd $SCRIPT_DIR && bash start_irina.sh" Enter
    echo "[irina] Launched in tmux session: $SESSION_NAME"
    echo "[irina] Attach with: tmux attach -t $SESSION_NAME"
    exit 0
fi

cd "$SCRIPT_DIR"

# Start session_manager
python3 session_manager.py &
SM_PID=$!
echo "[irina] SessionManager started (PID $SM_PID)"

# Wait for the user_input.sock to appear (up to 30s)
echo "[irina] Waiting for session_manager socket..."
for i in $(seq 1 30); do
    if [ -S "$SOCKET_DIR/user_input.sock" ]; then
        echo "[irina] Socket ready after ${i}s"
        break
    fi
    sleep 1
done

if [ ! -S "$SOCKET_DIR/user_input.sock" ]; then
    echo "[irina] WARNING: socket not ready — starting telegram_node anyway"
fi

# Start telegram_node
python3 telegram_node.py &
TG_PID=$!
echo "[irina] TelegramNode started (PID $TG_PID)"

# Start proactive node (set PROACTIVE_ENABLED=0 in irina.env to disable)
python3 proactive_node.py &
PRO_PID=$!
echo "[irina] ProactiveNode started (PID $PRO_PID)"

echo "[irina] All processes running. Ctrl+C or kill $SM_PID $TG_PID $PRO_PID to stop."

# Forward signals to all children
trap "kill $SM_PID $TG_PID $PRO_PID 2>/dev/null; exit 0" INT TERM
wait
