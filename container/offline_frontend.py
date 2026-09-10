#!/usr/bin/env python3
"""Credential-free offline stand-in for Telegram transport lifecycle tests."""
import os
import socket
import time

socket_dir = os.environ.get("SOCKET_DIR", "/run/axon")
paths = ("user_input.sock", "claude_response.sock")
while not all(os.path.exists(os.path.join(socket_dir, name)) for name in paths):
    time.sleep(0.1)

while True:
    subscriber = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        subscriber.connect(os.path.join(socket_dir, "claude_response.sock"))
        subscriber.settimeout(1)
        print("offline Telegram frontend attached to claude_response.sock", flush=True)
        while True:
            try:
                if not subscriber.recv(4096):
                    break
            except TimeoutError:
                continue
    except OSError:
        time.sleep(0.1)
    finally:
        subscriber.close()
