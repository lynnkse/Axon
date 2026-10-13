#!/usr/bin/env bash
set -euo pipefail
while true; do
  clear
  printf "Native Codex TUI on ROG\n\n"
  /home/lynnkse/.npm-global/bin/codex -C /home/lynnkse/Axon -m gpt-5.6-terra --dangerously-bypass-approvals-and-sandbox || true
  printf "\nCodex session ended. Reconnecting in 3 seconds (Ctrl-C to leave this launcher).\n"
  sleep 3
done
