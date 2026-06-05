#!/bin/bash
# start_axon.sh — Start Axon relay: orchestrator + telegram + cli + claude session.
# Run each in its own tmux pane, or background all.
# Usage: bash start_axon.sh [--tmux]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

set -a
source "$PROJECT_ROOT/.env" 2>/dev/null || true
set +a

PYTHON="${PYTHON:-$HOME/.pyenv/versions/cognitive-hq/bin/python}"
RELAY_LOCK="/tmp/cognitive-hq-relay.lock"

# ── Check .env ────────────────────────────────────────────────────────────────
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "[axon] ERROR: DEEPSEEK_API_KEY not set in .env"
    exit 1
fi

# ── Kill existing ─────────────────────────────────────────────────────────────
echo "[axon] Cleaning up old processes..."
pkill -f orchestrator.py  2>/dev/null
pkill -f telegram_node.py 2>/dev/null
pkill -f session_manager.py 2>/dev/null
pkill -f proactive_node.py 2>/dev/null
rm -f /tmp/cognitive-hq/*.sock /tmp/cognitive-hq/deepseek_brain.lock "$RELAY_LOCK" 2>/dev/null
sleep 1

if [[ "${1:-}" == "--tmux" ]]; then
    SESSION="axon"
    tmux new-session -d -s "$SESSION" -x 220 -y 50 2>/dev/null || tmux kill-session -t "$SESSION" && tmux new-session -d -s "$SESSION" -x 220 -y 50

    # Pane 0: orchestrator (brain)
    tmux send-keys -t "$SESSION" "cd $SCRIPT_DIR && $PYTHON orchestrator.py 2>&1 | tee /tmp/orchestrator.log" Enter

    # Pane 1: Claude session_manager
    tmux split-window -t "$SESSION" -v
    tmux send-keys -t "$SESSION" "sleep 5 && cd $SCRIPT_DIR && $PYTHON session_manager.py 2>&1 | tee /tmp/session_manager.log" Enter

    # Pane 2: Telegram node
    tmux split-window -t "$SESSION" -h
    tmux send-keys -t "$SESSION" "sleep 8 && cd $SCRIPT_DIR && $PYTHON telegram_node.py 2>&1 | tee /tmp/tg.log" Enter

    # Pane 3: CLI node
    tmux new-window -t "$SESSION"
    tmux send-keys -t "$SESSION" "sleep 10 && cd $SCRIPT_DIR && $PYTHON cli_node.py" Enter

    echo "[axon] Launched in tmux session: $SESSION"
    echo "[axon] Attach: tmux attach -t $SESSION"
    exit 0
fi

# ── Background mode ───────────────────────────────────────────────────────────
echo $$ > "$RELAY_LOCK"
trap "rm -f $RELAY_LOCK; kill $ORCH_PID $SM_PID $TG_PID $PRO_PID 2>/dev/null" EXIT INT TERM

cd "$SCRIPT_DIR"

echo "[axon] Starting orchestrator (DeepSeek brain)..."
$PYTHON orchestrator.py >> /tmp/orchestrator.log 2>&1 &
ORCH_PID=$!

echo "[axon] Waiting for sockets..."
for i in $(seq 1 30); do
    [ -S "/tmp/cognitive-hq/user_input.sock" ] && echo "[axon] Sockets ready (${i}s)" && break
    sleep 1
done

echo "[axon] Starting Claude session_manager..."
$PYTHON session_manager.py >> /tmp/session_manager.log 2>&1 &
SM_PID=$!
sleep 3

echo "[axon] Starting Telegram node..."
$PYTHON telegram_node.py >> /tmp/tg.log 2>&1 &
TG_PID=$!

echo "[axon] Starting proactive node..."
$PYTHON proactive_node.py >> /tmp/proactive.log 2>&1 &
PRO_PID=$!

echo "[axon] All running. Logs: /tmp/orchestrator.log /tmp/tg.log"
echo "[axon] CLI: $PYTHON $SCRIPT_DIR/cli_node.py"
wait $ORCH_PID
