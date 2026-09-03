#!/bin/bash
# start_ailin.sh — bring up Ailin's fully standalone deployment.
#
# Reuses Axon's exact same session_manager.py / telegram_node.py engine
# (same "gas": spawned claude subprocess, PTY, socket bridge) but as a
# second, wholly separate pair of processes: own sockets, own session file,
# own working directory, own Telegram bot, own Supabase project. She shares
# no live state with Axon's own running session — the only link between
# them is the one-way internal-tick pulse Axon's own actor mechanism sends
# into her socket (see session_manager.py's _pulse_ailin).
#
# Needs AILIN_TELEGRAM_BOT_TOKEN set in .env (own bot, created via BotFather)
# before this will actually come up — telegram_node.py exits immediately
# without a token.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$AXON_DIR/.env" ]] && set -a && source "$AXON_DIR/.env" && set +a

if [[ -n "$AXON_PYTHON_DIR" ]]; then
    VENV_PYTHON="$AXON_PYTHON_DIR/python3"
else
    VENV_PYTHON="$HOME/.virtualenvs/lynnkse/bin/python3.12"
fi
PYENV_PYTHON="$HOME/.pyenv/versions/3.12.9/bin/python3"
[[ -x "$VENV_PYTHON" ]] && PYTHON="$VENV_PYTHON" || PYTHON="$PYENV_PYTHON"

AILIN_DIR="$HOME/cognitive-hq/ailin"
AILIN_SOCKET_DIR="/tmp/ailin"
AILIN_RELAY_DIR="$HOME/.claude-relay-ailin"
LOG_DIR="$AILIN_DIR/logs"
mkdir -p "$AILIN_SOCKET_DIR" "$AILIN_RELAY_DIR" "$LOG_DIR"

if [[ -z "$AILIN_TELEGRAM_BOT_TOKEN" ]]; then
    echo "AILIN_TELEGRAM_BOT_TOKEN not set in .env — create her bot via @BotFather first."
    exit 1
fi

# Env overrides that make this a fully separate instance of the same engine.
COMMON_ENV=(
    "SOCKET_DIR=$AILIN_SOCKET_DIR"
    "RELAY_DIR=$AILIN_RELAY_DIR"
    "PROJECT_DIR=$AILIN_DIR"
    "PROFILE_PATH=$AILIN_DIR/ailin_profile.md"
    "SESSION_CHANNEL=ailin_telegram"
    "SKIP_MEMORY_FETCH=1"
    "AXON_ACTORS=0"
    "AXON_INSTANCE=ailin"
    "TELEGRAM_BOT_TOKEN=$AILIN_TELEGRAM_BOT_TOKEN"
    "TELEGRAM_USER_ID=$TELEGRAM_USER_ID"
    "USER_TIMEZONE=$USER_TIMEZONE"
    "USER_NAME=Lynn"
    "AILIN_SUPABASE_URL=$AILIN_SUPABASE_URL"
    "AILIN_SUPABASE_SERVICE_ROLE_KEY=$AILIN_SUPABASE_SERVICE_ROLE_KEY"
)

start_in_session() {
    local session="$1"
    local cmd="$2"
    if tmux has-session -t "$session" 2>/dev/null; then
        echo "Session '$session' exists — restarting process..."
        tmux send-keys -t "$session" C-c
        sleep 0.3
        tmux send-keys -t "$session" C-c
        sleep 0.3
        tmux send-keys -t "$session" "q" Enter 2>/dev/null
        sleep 0.4
    else
        echo "Session '$session' not found — creating..."
        tmux new-session -d -s "$session" -x 220 -y 50
    fi
    tmux send-keys -t "$session" "$cmd" Enter
    echo "  → '$session' started"
}

ENV_PREFIX="$(printf '%s ' "${COMMON_ENV[@]}")"

start_in_session ailin_manager \
    "cd '$AXON_DIR' && env $ENV_PREFIX $PYTHON -u session_manager.py 2>&1 | tee '$LOG_DIR/manager.log'"

start_in_session ailin_telegram \
    "cd '$AXON_DIR' && env $ENV_PREFIX $PYTHON -u telegram_node.py 2>&1 | tee '$LOG_DIR/telegram.log'"

echo ""
echo "Ailin standalone deployment starting."
echo "  Sessions: ailin_manager | ailin_telegram"
echo "  Sockets:  $AILIN_SOCKET_DIR"
echo "  Logs:     $LOG_DIR"
echo "  Attach:   tmux attach -t ailin_manager   (or ailin_telegram)"
