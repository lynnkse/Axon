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
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


PROMPT = "You > "
PRINT_LOCK = threading.Lock()


def write_status(text):
    """Render connection state without pretending it is an assistant turn."""
    with PRINT_LOCK:
        print(f"\n[status] {text}\n{PROMPT}", end="", flush=True)


def write_reply(text, source="cli"):
    """Render one complete assistant turn in the terminal transcript."""
    with PRINT_LOCK:
        label = "Axon [Telegram]" if source == "telegram" else "Axon"
        print(f"\n{label} > {text}\n\n{PROMPT}", end="", flush=True)


class ChatStream:
    """Display only correlated public conversation events, never raw JSON."""
    def __init__(self):
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.active = {}
        self.finished = set()
        self.finished_order = []

    def register(self, request_id):
        with self.lock:
            self.active[("cli", request_id)] = {"last_activity": time.monotonic(), "last_display": 0.0}

    def handle(self, message):
        source, request_id = message.get("source"), message.get("request_id")
        if source not in {"cli", "telegram"} or not request_id:
            return
        key = (source, request_id)
        with self.lock:
            if key in self.finished:
                return
            kind = message.get("type")
            if kind == "activity":
                now = time.monotonic()
                state = self.active.setdefault(key, {"last_activity": now, "last_display": 0.0})
                phase = {"actor": "Project actors", "main": "Your request", "preparing": "Preparing"}.get(
                    message.get("phase"), "Request")
                prefix = "Telegram · " if source == "telegram" else ""
                if message.get("growing"):
                    state["last_activity"] = now
                    state["quiet"] = False
                    prose = str(message.get("message") or "").strip()
                    if prose:
                        write_status(f"{prefix}{phase}: {prose}")
                    elif message.get("kind") == "phase" or now - state["last_display"] >= 1.0:
                        write_status(f"{prefix}{phase}: {message.get('detail') or 'activity received'}")
                    if prose or message.get("kind") == "phase" or now - state["last_display"] >= 1.0:
                        state["last_display"] = now
                elif now - state["last_activity"] >= 75 and not state.get("quiet"):
                    write_status(f"{prefix}{phase}: no new task activity for {int(now - state['last_activity'])}s; connection alive")
                    state["quiet"] = True
                return
            if kind or key not in self.active:
                return
            self.active.pop(key, None)
            self.finished.add(key)
            self.finished_order.append(key)
            if len(self.finished_order) > 1024:
                self.finished.discard(self.finished_order.pop(0))
            text = str(message.get("text") or "").strip()
            if text:
                write_reply(text, source)


def listen_forever(stop_event, stream):
    """Keep the CLI subscribed across response-socket reconnects.

    The response socket is a subscription stream.  A manager restart or a
    transient socket replacement used to terminate the one-shot listener,
    leaving the input prompt alive but silently dropping every response.
    """
    while not stop_event.is_set():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(config.CLAUDE_RESPONSE_SOCK)
            stream.ready.set()
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
                    stream.handle(msg)
        except (OSError, socket.error) as exc:
            if not stop_event.is_set():
                write_status(f"response listener disconnected; reconnecting in 2s ({exc})")
        finally:
            stream.ready.clear()
            sock.close()
        if not stop_event.is_set():
            time.sleep(2)


def main():
    stop_event = threading.Event()
    stream = ChatStream()
    threading.Thread(target=listen_forever, args=(stop_event, stream), daemon=True).start()

    print(
        "\n"
        "╭──────────────────────────────────────────────╮\n"
        "│ Axon CLI · connected conversation             │\n"
        "│ Live phases, tool activity, and final replies.│\n"
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
            if not stream.ready.wait(timeout=2):
                write_status("manager response stream is unavailable; message was not sent")
                continue
            msg_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                msg_sock.settimeout(5)
                msg_sock.connect(config.USER_INPUT_SOCK)
                request_id = uuid.uuid4().hex
                stream.register(request_id)
                payload = json.dumps({"text": text, "source": "cli", "user_id": "anton", "request_id": request_id})
                msg_sock.sendall((payload + "\n").encode())
                write_status("Request sent; waiting for the manager…")
            except OSError as exc:
                write_status(f"message was not sent ({exc})")
            finally:
                msg_sock.close()
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
