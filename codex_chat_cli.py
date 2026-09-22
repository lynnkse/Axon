#!/usr/bin/env python3
"""Interactive text chat client for session_manager_codex.py.

The terminal prompt is the local conversation transcript: submitted lines
remain visible, in-flight turns are marked, and responses survive transient
response-socket reconnects.
"""
import os
import sys
import json
import socket
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


PROMPT = "You > "
PRINT_LOCK = threading.Lock()


def write_status(text):
    """Render connection state without pretending it is an assistant turn."""
    with PRINT_LOCK:
        print(f"\n[status] {text}\n{PROMPT}", end="", flush=True)


def write_reply(text):
    """Render one complete assistant turn in the terminal transcript."""
    with PRINT_LOCK:
        print(f"\nAxon > {text}\n\n{PROMPT}", end="", flush=True)


def listen_forever(stop_event):
    """Keep the CLI subscribed across response-socket reconnects.

    The response socket is a subscription stream.  A manager restart or a
    transient socket replacement used to terminate the one-shot listener,
    leaving the input prompt alive but silently dropping every response.
    """
    while not stop_event.is_set():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(config.CLAUDE_RESPONSE_SOCK)
            write_status("response listener connected")
            buf = b""
            while not stop_event.is_set():
                data = sock.recv(4096)
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
                    text = str(msg.get("text", "")).strip()
                    if text:
                        write_reply(text)
        except (OSError, socket.error) as exc:
            if not stop_event.is_set():
                write_status(f"response listener disconnected; reconnecting in 2s ({exc})")
        finally:
            sock.close()
        if not stop_event.is_set():
            time.sleep(2)


def main():
    stop_event = threading.Event()
    threading.Thread(target=listen_forever, args=(stop_event,), daemon=True).start()

    print(
        "\n"
        "╭──────────────────────────────────────────────╮\n"
        "│ Axon CLI · connected conversation             │\n"
        "│ Your messages and new Axon replies stay here. │\n"
        "╰──────────────────────────────────────────────╯\n"
        f"Socket: {config.SOCKET_DIR}\n"
        "Type a message and press Enter. Ctrl-C exits.\n"
    )
    try:
        while True:
            try:
                text = input(PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            if not text.strip():
                continue
            msg_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                msg_sock.connect(config.USER_INPUT_SOCK)
                payload = json.dumps({"text": text, "source": "cli", "user_id": "anton"})
                msg_sock.sendall((payload + "\n").encode())
                write_status("Axon is thinking…")
            except OSError as exc:
                write_status(f"message was not sent ({exc})")
            finally:
                msg_sock.close()
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
