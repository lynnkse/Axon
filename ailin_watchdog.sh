#!/bin/bash
# ailin_watchdog.sh — poll Ailin's manager/telegram tmux panes and restart
# on silent death (process exits with no traceback, pane drops to a bare
# shell). Logs every detected death with a timestamp so recurrence/pattern
# can be diagnosed later, since the underlying cause of these silent kills
# (no OOM, no cgroup limit, no traceback -- see 2026-08-23 investigation)
# is not yet confirmed.

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$HOME/ailin/logs/watchdog.log"
POLL_SECONDS=30

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $1" | tee -a "$LOG_FILE"
}

pane_alive() {
    local session="$1"
    local cmd
    cmd=$(tmux list-panes -t "$session" -F "#{pane_current_command}" 2>/dev/null)
    [[ "$cmd" == "python3.12" ]]
}

log "Watchdog started (polling every ${POLL_SECONDS}s)"

while true; do
    dead=""
    if ! tmux has-session -t ailin_manager 2>/dev/null || ! pane_alive ailin_manager; then
        dead="$dead ailin_manager"
    fi
    if ! tmux has-session -t ailin_telegram 2>/dev/null || ! pane_alive ailin_telegram; then
        dead="$dead ailin_telegram"
    fi

    if [[ -n "$dead" ]]; then
        log "DEAD:$dead — restarting via start_ailin.sh"
        cd "$AXON_DIR" && bash start_ailin.sh >> "$LOG_FILE" 2>&1
        log "Restart triggered for:$dead"
    fi

    sleep "$POLL_SECONDS"
done
