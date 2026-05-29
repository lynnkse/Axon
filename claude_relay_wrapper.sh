#!/bin/bash
# Wrapper that launches Claude Code with CLAUDE_RELAY_SESSION=1 set.
# Used by SessionManagerNode so permission_hook.py can distinguish relay
# tool calls from interactive sessions.
export CLAUDE_RELAY_SESSION=1
exec claude --dangerously-skip-permissions --model claude-sonnet-4-6 "$@"
