#!/bin/bash
# Start relay with DeepSeek brain (replaces session_manager).
# Launches deepseek_brain + telegram_node + proactive_node.
# Requires DEEPSEEK_API_KEY in .env.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

set -a
source "$PROJECT_ROOT/.env" 2>/dev/null || true
set +a

if [[ "${1:-}" == "--tmux" ]]; then
    SESSION_NAME="relay-main"
    tmux new-session -d -s "$SESSION_NAME" -x 220 -y 50 2>/dev/null || true
    tmux send-keys -t "$SESSION_NAME" "cd $SCRIPT_DIR && bash start_relay.sh" Enter
    echo "[relay] Launched in tmux session: $SESSION_NAME"
    echo "[relay] Attach with: tmux attach -t $SESSION_NAME"
    exit 0
fi

cd "$SCRIPT_DIR"

# Use pyenv python if available (needed on machines with system python < 3.9)
PYTHON="${PYTHON:-$HOME/.pyenv/versions/cognitive-hq/bin/python}"

# PID lock — prevent multiple relay instances
RELAY_LOCK="/tmp/cognitive-hq-relay.lock"
if [ -f "$RELAY_LOCK" ]; then
    OLD_PID=$(cat "$RELAY_LOCK")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[relay] ERROR: relay already running (PID $OLD_PID). Kill it first: kill $OLD_PID"
        exit 1
    else
        echo "[relay] Stale lock found, removing."
        rm -f "$RELAY_LOCK"
    fi
fi
echo $$ > "$RELAY_LOCK"
trap "rm -f $RELAY_LOCK" EXIT

if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "[relay] ERROR: DEEPSEEK_API_KEY not set in .env"
    exit 1
fi

echo "[relay] Starting DeepSeek brain..."

$PYTHON deepseek_brain.py &
SM_PID=$!
echo "[relay] DeepSeekBrain PID: $SM_PID"

echo "[relay] Waiting for socket..."
for i in $(seq 1 30); do
    [ -S "/tmp/cognitive-hq/user_input.sock" ] && echo "[relay] Socket ready after ${i}s" && break
    sleep 1
done

$PYTHON telegram_node.py &
TG_PID=$!
echo "[relay] TelegramNode PID: $TG_PID"

$PYTHON proactive_node.py &
PRO_PID=$!
echo "[relay] ProactiveNode PID: $PRO_PID"

echo "[relay] All running. Ctrl+C to stop."
trap "kill $SM_PID $TG_PID $PRO_PID 2>/dev/null; exit 0" INT TERM
wait
