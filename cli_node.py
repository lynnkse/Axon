#!/usr/bin/env python3
"""
cli_node.py — Text terminal interface to Axon orchestrator.

Connects to user_input.sock (send) and claude_response.sock (receive).
Simple stdin/stdout — no PTY, no raw mode.

Displays messages with colored source labels:
  [DS]      DeepSeek response
  [CC→]     Claude delegation request
  [CC✓]     Claude result
  [You]     Echo of your input
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
import config

COLORS = {
    "deepseek":       "\033[36m",    # cyan
    "claude_request": "\033[33m",    # yellow
    "claude_result":  "\033[32m",    # green
    "user_echo":      "\033[90m",    # grey
    "status":         "\033[35m",    # magenta
}
RESET = "\033[0m"
LABELS = {
    "deepseek":       "[DS] ",
    "claude_request": "[CC→] ",
    "claude_result":  "[CC✓] ",
    "user_echo":      "",
    "status":         "[•] ",
}


def _recv_loop():
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(config.CLAUDE_RESPONSE_SOCK)
            buf = b""
            while True:
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg    = json.loads(line)
                        text   = msg.get("text", "")
                        source = msg.get("source", "axon")
                        color  = COLORS.get(source, "")
                        label  = LABELS.get(source, f"[{source}] ")
                        if text:
                            print(f"\n{color}{label}{text}{RESET}", flush=True)
                    except Exception:
                        pass
        except Exception:
            time.sleep(1)


def _send(text: str):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(config.USER_INPUT_SOCK)
        payload = (json.dumps({"text": text, "source": "cli", "user_id": config.USER_ID}) + "\n").encode()
        s.sendall(payload)
        s.close()
    except Exception as e:
        print(f"[error] Could not send: {e}", file=sys.stderr)


def main():
    print("Axon CLI — type to send, Ctrl+C to quit", flush=True)
    threading.Thread(target=_recv_loop, daemon=True).start()
    try:
        while True:
            line = input("> ")
            if line.strip():
                _send(line.strip())
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")


if __name__ == "__main__":
    main()
