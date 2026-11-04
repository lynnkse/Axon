"""Route private fitness data through the existing server-side writer on ROG.

The work instance has a public Supabase key: a successful GET can therefore
hide all fitness rows. Never interpret that filtered result as empty history.
Secrets remain on the writer. Other instances may explicitly configure
SUPABASE_FITNESS_WRITER_SSH, SUPABASE_FITNESS_WRITER_REPO and
SUPABASE_FITNESS_WRITER_PYTHON in their existing Axon environment.
"""
import base64
import json
import shlex
import subprocess

import config

PROTECTED_TABLES = frozenset({"food_entries", "fitness_log", "frequent_foods"})


class FitnessTransportError(RuntimeError):
    """Authoritative fitness data is unavailable; this never means empty."""


def is_server_key(key):
    if key.startswith("sb_secret_"):
        return True
    try:
        claims = json.loads(base64.urlsafe_b64decode(key.split(".")[1] + "==="))
        return isinstance(claims, dict) and claims.get("role") == "service_role"
    except (IndexError, ValueError, TypeError):
        return False


# Request data is delivered on stdin, never interpolated into shell code.
# This script uses the remote config but does not depend on a remote upgrade.
WRITER_SCRIPT = r'''
import base64, json, sys, urllib.error, urllib.request
sys.path.insert(0, sys.argv[1])
import config
request = json.load(sys.stdin)
if request["path"].split("?", 1)[0] not in {"food_entries", "fitness_log", "frequent_foods"}:
    raise SystemExit("unsupported fitness table")
if request["method"] not in {"GET", "POST", "PATCH"}:
    raise SystemExit("unsupported fitness operation")
if config.SUPABASE_URL.rstrip("/") != request["project_url"].rstrip("/"):
    raise SystemExit("fitness writer project mismatch")
key = config.SUPABASE_SERVICE_ROLE_KEY
server_key = key.startswith("sb_secret_")
if not server_key:
    try:
        claims = json.loads(base64.urlsafe_b64decode(key.split(".")[1] + "==="))
        server_key = isinstance(claims, dict) and claims.get("role") == "service_role"
    except (IndexError, ValueError, TypeError):
        pass
if not server_key:
    raise SystemExit("fitness writer is not using its server credential")
headers = {"apikey": key, "Authorization": "Bearer " + key,
           "Content-Type": "application/json", "Prefer": "return=representation"}
data = None if request["payload"] is None else json.dumps(request["payload"]).encode()
req = urllib.request.Request(config.SUPABASE_URL.rstrip("/") + "/rest/v1/" + request["path"],
                             data=data, method=request["method"], headers=headers)
try:
    with urllib.request.urlopen(req, timeout=request["timeout"]) as response:
        body = response.read().decode()
        print(json.dumps({"status": response.status, "rows": json.loads(body) if body else []}))
except urllib.error.HTTPError as error:
    try:
        code = json.loads(error.read()).get("code", "unknown")
    except (ValueError, AttributeError):
        code = "unknown"
    print(json.dumps({"error": "database_request_failed", "status": error.code, "code": code}))
    raise SystemExit(1)
'''


def via_writer(method, path, payload=None, timeout=10):
    """None means use local transport; otherwise return verified writer result."""
    if path.split("?", 1)[0] not in PROTECTED_TABLES:
        return None
    if is_server_key(config.SUPABASE_SERVICE_ROLE_KEY):
        return None
    writer = config.get("SUPABASE_FITNESS_WRITER_SSH", "")
    if not writer and config.INSTANCE in {"aevadim09", "aevadim-09"}:
        writer = "ROG"
    if not writer or writer.startswith("-"):
        raise FitnessTransportError("Fitness requires the authoritative writer route; public-key results are not valid history")
    repo = config.get("SUPABASE_FITNESS_WRITER_REPO", "/home/lynnkse/Axon")
    python = config.get("SUPABASE_FITNESS_WRITER_PYTHON", "/home/lynnkse/.pyenv/versions/claude-relay/bin/python3")
    command = " ".join(shlex.quote(part) for part in (python, "-c", WRITER_SCRIPT, repo))
    request = {"method": method, "path": path, "payload": payload,
               "timeout": timeout, "project_url": config.SUPABASE_URL}
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", writer, command],
            input=json.dumps(request), text=True, capture_output=True,
            timeout=timeout + 15, check=False,
        )
        response = json.loads(result.stdout) if result.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise FitnessTransportError("Authoritative fitness writer transport failed; history is unknown") from exc
    if result.returncode or not isinstance(response, dict) or response.get("error"):
        diagnostic = {k: response.get(k) for k in ("status", "code")} if isinstance(response, dict) else {}
        raise FitnessTransportError(f"Authoritative fitness writer request failed ({diagnostic}); history is unknown")
    if response.get("status") not in (200, 201, 204) or not isinstance(response.get("rows"), list):
        raise FitnessTransportError("Authoritative fitness writer returned an invalid response")
    return response
