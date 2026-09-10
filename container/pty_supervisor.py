#!/usr/bin/env python3
"""Give a supervised terminal-oriented child a stable PTY and drain output."""
import os
import pty
import signal
import subprocess
import sys

master, slave = pty.openpty()
child = subprocess.Popen(
    [sys.executable, "-u", *sys.argv[1:]],
    stdin=slave, stdout=slave, stderr=slave, close_fds=True,
)
os.close(slave)


def forward(signum, _frame):
    if child.poll() is None:
        child.send_signal(signum)


signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
try:
    while True:
        data = os.read(master, 4096)
        if not data:
            break
        os.write(sys.stdout.fileno(), data)
except OSError:
    pass
finally:
    os.close(master)
sys.exit(child.wait())
