#!/bin/bash
# shutdown_axon.sh — find and stop every Axon process and tmux session.
#
# Safe to run any time, including when nothing is running (every step is a
# no-op if there's nothing to kill). Intended as a clean-slate reset, called
# automatically at the start of start_axon.sh, or run standalone to fully
# stop Axon.

echo "Stopping Axon processes..."

# ── Kill the known long-running Axon processes by pattern ────────────────────
PATTERNS=(
    "session_manager.py"
    "telegram_node.py"
    "cli_node.py"
    "curator.py"
    "streamlit run.*app.py"
    "ttyd.*browser_view"
)

for pattern in "${PATTERNS[@]}"; do
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        echo "  killing: $pattern"
        pkill -f "$pattern" 2>/dev/null
    fi
done

# Give processes a moment to exit cleanly before force-killing stragglers.
sleep 1
for pattern in "${PATTERNS[@]}"; do
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        echo "  force-killing (still alive after SIGTERM): $pattern"
        pkill -9 -f "$pattern" 2>/dev/null
    fi
done

# ── Kill the tmux sessions themselves ─────────────────────────────────────────
SESSIONS=(manager telegram cli curator web browser_view)

for session in "${SESSIONS[@]}"; do
    if tmux has-session -t "$session" 2>/dev/null; then
        echo "  closing tmux session: $session"
        tmux kill-session -t "$session" 2>/dev/null
    fi
done

echo "Axon stack stopped."
