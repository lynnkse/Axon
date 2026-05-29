#!/usr/bin/env python3
"""
CLINode — Relay v2

Thin terminal client for SessionManagerNode.
- Connects to display.sock → streams raw PTY bytes to stdout (includes ANSI)
- Reads stdin in raw mode → forwards every byte to cli_input.sock
- Forwards SIGWINCH (terminal resize) to SessionManagerNode via resize message

Usage:
    python3 cli_node.py

SessionManagerNode must be running first.
"""

import os
import sys
import tty
import termios
import fcntl
import struct
import socket
import threading
import signal
import time
import json

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))
import config

RECONNECT_DELAY = 2  # seconds between reconnect attempts


class CLINode:

    def __init__(self):
        self.display_conn: socket.socket | None = None
        self.input_conn: socket.socket | None = None
        self._running = True
        self._old_tty_settings = None

    # ------------------------------------------------------------------
    # Terminal raw mode
    # ------------------------------------------------------------------

    def _enter_raw_mode(self):
        fd = sys.stdin.fileno()
        self._old_tty_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

    def _restore_tty(self):
        if self._old_tty_settings is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(),
                    termios.TCSADRAIN,
                    self._old_tty_settings,
                )
            except Exception:
                pass
            self._old_tty_settings = None

    # ------------------------------------------------------------------
    # Terminal size
    # ------------------------------------------------------------------

    def _get_terminal_size(self) -> tuple[int, int]:
        try:
            size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols = struct.unpack("HHHH", size)[:2]
            return rows, cols
        except Exception:
            return 24, 80

    def _send_resize(self):
        """Send terminal size to SessionManagerNode as NDJSON on cli_input.sock."""
        if self.input_conn is None:
            return
        rows, cols = self._get_terminal_size()
        # Embed resize as a special JSON line prefixed with a null byte so
        # SessionManagerNode can distinguish it from raw keyboard bytes.
        # Format: \x00{"type":"resize","rows":N,"cols":N}\n
        msg = b"\x00" + json.dumps({"type": "resize", "rows": rows, "cols": cols}).encode() + b"\n"
        try:
            self.input_conn.sendall(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> bool:
        """Connect to both sockets. Returns True on success."""
        try:
            display = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            display.connect(config.DISPLAY_SOCK)

            inp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            inp.connect(config.CLI_INPUT_SOCK)

            self.display_conn = display
            self.input_conn = inp
            return True
        except Exception as e:
            return False

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _display_thread(self):
        """Read raw PTY bytes from display.sock, write directly to stdout."""
        while self._running:
            try:
                data = self.display_conn.recv(4096)
            except Exception:
                break
            if not data:
                break
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except Exception:
                break

        # SessionManagerNode disconnected or died
        self._running = False

    def _input_thread(self):
        """
        Read stdin in raw mode, forward every byte to cli_input.sock.
        Ctrl+C (0x03) and Ctrl+\\ (0x1c) are forwarded as-is so they
        reach Claude's PTY and can interrupt running tools.
        """
        while self._running:
            try:
                data = os.read(sys.stdin.fileno(), 256)
            except Exception:
                break
            if not data:
                break
            try:
                self.input_conn.sendall(data)
            except Exception:
                break

        self._running = False

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------

    def run(self):
        # Wait for SessionManagerNode to be ready
        sys.stderr.write("Connecting to SessionManagerNode")
        sys.stderr.flush()

        while not self._connect():
            sys.stderr.write(".")
            sys.stderr.flush()
            time.sleep(RECONNECT_DELAY)

        sys.stderr.write(" connected.\n")
        sys.stderr.flush()

        # Enter raw mode before starting threads
        self._enter_raw_mode()

        # Forward terminal resize signals
        signal.signal(signal.SIGWINCH, lambda s, f: self._send_resize())
        self._send_resize()

        display_t = threading.Thread(target=self._display_thread, daemon=True)
        input_t = threading.Thread(target=self._input_thread, daemon=True)

        display_t.start()
        input_t.start()

        try:
            # Run until either thread exits (SessionManagerNode gone or user exits)
            while self._running:
                time.sleep(0.1)
        finally:
            self._running = False
            self._restore_tty()

            if self.display_conn:
                try:
                    self.display_conn.close()
                except Exception:
                    pass
            if self.input_conn:
                try:
                    self.input_conn.close()
                except Exception:
                    pass

            # Print newline so shell prompt appears on a clean line
            sys.stdout.write("\r\n")
            sys.stdout.flush()


if __name__ == "__main__":
    CLINode().run()
