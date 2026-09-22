#!/bin/bash
# shutdown_axon.sh — find and stop every Axon process and tmux session.
#
# Safe to run any time, including when nothing is running (every step is a
# no-op if there's nothing to kill). Intended as a clean-slate reset, called
# automatically at the start of start_axon.sh, or run standalone to fully
# stop Axon.

echo "Stopping Axon processes..."

AXON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Return the complete process tree owned by one tmux session. This is the
# ownership boundary: Axon sessions are manager/telegram/etc.; Ailin uses
# ailin_manager, ailin_telegram, and ailin_watchdog even though both can run
# scripts from the same checkout.
session_tree_pids() {
    local session="$1"
    local pid child
    local -a queue children

    mapfile -t queue < <(tmux list-panes -t "$session" -F '#{pane_pid}' 2>/dev/null)
    while (( ${#queue[@]} )); do
        pid="${queue[0]}"
        queue=("${queue[@]:1}")
        echo "$pid"
        mapfile -t children < <(ps -eo pid=,ppid= | awk -v parent="$pid" '$2 == parent {print $1}')
        for child in "${children[@]}"; do
            queue+=("$child")
        done
    done
}

# ── Stop only Axon's exact tmux sessions and their process trees ─────────────
# cli is the persistent public attach target for the native Codex
# mirror.  "cli" is included once to remove old deployments that created a
# second mirror under the temporary name used during the migration.
SESSIONS=(manager telegram cli curator usage web browser_view)
declare -a axon_pids=()

for session in "${SESSIONS[@]}"; do
    if tmux has-session -t "$session" 2>/dev/null; then
        mapfile -t tree_pids < <(session_tree_pids "$session")
        axon_pids+=("${tree_pids[@]}")
        echo "  closing tmux session: $session"
        tmux kill-session -t "$session" 2>/dev/null
    fi
done

# A tmux session normally sends SIGHUP to its full pane tree. Explicitly stop
# any captured descendants that outlive the pane, such as nohup web workers.
if (( ${#axon_pids[@]} )); then
    kill "${axon_pids[@]}" 2>/dev/null || true
    sleep 1
    for pid in "${axon_pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
fi

# The dashboard bridge is started with nohup so an older deployment can leave
# it reparented after its tmux pane dies.  Stop only bridge processes executing
# from this Axon checkout; do not touch similarly named services elsewhere.
declare -a bridge_pids=()
while IFS= read -r pid; do
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || continue
    [[ "$cmdline" == *"$AXON_DIR/web_cli_bridge.py"* ]] && bridge_pids+=("$pid")
done < <(pgrep -f 'web_cli_bridge\.py' 2>/dev/null || true)

if (( ${#bridge_pids[@]} )); then
    echo "  stopping Axon web bridge: ${bridge_pids[*]}"
    kill "${bridge_pids[@]}" 2>/dev/null || true
    sleep 1
    for pid in "${bridge_pids[@]}"; do
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
fi

echo "Axon stack stopped."
