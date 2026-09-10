#!/usr/bin/env python3
import os
import socket
import subprocess
import sys
import urllib.request

required = {"manager"}
for env_name, process in (
    ("AXON_ENABLE_TELEGRAM", "telegram"),
    ("AXON_ENABLE_CURATOR", "curator"),
    ("AXON_ENABLE_USAGE_MONITOR", "usage-monitor"),
    ("AXON_ENABLE_DASHBOARD", "dashboard"),
    ("AXON_ENABLE_WEB_CLI", "web-cli-bridge"),
    ("AXON_ENABLE_CLI", "cli"),
):
    if os.environ.get(env_name, "1") == "1":
        required.add(process)

result = subprocess.run(
    ["supervisorctl", "-c", "/run/axon/supervisord.conf", "status"],
    text=True, capture_output=True, timeout=4,
)
if result.returncode:
    print(result.stderr.strip() or result.stdout.strip())
    sys.exit(1)

states = {}
for line in result.stdout.splitlines():
    fields = line.split()
    if len(fields) >= 2:
        states[fields[0]] = fields[1]
bad = {name: states.get(name, "MISSING") for name in required if states.get(name) != "RUNNING"}
if bad:
    print(f"unhealthy supervisor states: {bad}")
    sys.exit(1)

for name in ("user_input.sock", "cli_input.sock", "display.sock", "claude_response.sock", "permission.sock"):
    path = os.path.join(os.environ.get("SOCKET_DIR", "/run/axon"), name)
    if not os.path.exists(path):
        print(f"missing socket: {path}")
        sys.exit(1)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(path)
    except OSError as exc:
        print(f"socket not connectable: {path}: {exc}")
        sys.exit(1)
    finally:
        probe.close()

if os.environ.get("AXON_ENABLE_DASHBOARD", "1") == "1":
    with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=2) as response:
        if response.status != 200:
            raise SystemExit("dashboard health endpoint failed")
if os.environ.get("AXON_ENABLE_WEB_CLI", "1") == "1":
    with socket.create_connection(("127.0.0.1", 7690), timeout=2):
        pass

print("healthy")
