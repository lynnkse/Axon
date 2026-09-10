#!/usr/bin/env python3
"""Minimal text chat client for session_manager_codex.py (or any engine using
the same NDJSON socket protocol). Unlike cli_node.py, this doesn't assume a
live PTY to stream raw bytes from -- it's a plain request/response loop:
type a line, get a line back.
"""
import os
import sys
import json
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


def listen(sock):
    buf = b""
    while True:
        try:
            data = sock.recv(4096)
        except Exception:
            break
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(f"\n[axon] {msg.get('text', '')}\n> ", end="", flush=True)


def main():
    resp_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    resp_sock.connect(config.CLAUDE_RESPONSE_SOCK)
    threading.Thread(target=listen, args=(resp_sock,), daemon=True).start()

    print(f"Connected to {config.SOCKET_DIR}. Type a message and press Enter.")
    while True:
        try:
            text = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            continue
        msg_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        msg_sock.connect(config.USER_INPUT_SOCK)
        payload = json.dumps({"text": text, "source": "telegram", "user_id": "anton"})
        msg_sock.sendall((payload + "\n").encode())
        msg_sock.close()


if __name__ == "__main__":
    main()
