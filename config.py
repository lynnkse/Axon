"""
Shared config for relay v2.
Reads from claude-telegram-relay/.env (same file as v1).
"""

import os
from pathlib import Path

# Locate .env relative to this file: relay_v2/../.env
_ENV_PATH = Path(__file__).parent.parent / ".env"

# Locate profile.md: relay_v2/../config/profile.md (overridable via PROFILE_PATH env)
_default_profile = str(Path(__file__).parent.parent / "config" / "profile.md")
PROFILE_PATH: Path = Path(os.environ.get("PROFILE_PATH") or _default_profile)


def _load_env(path: Path) -> dict:
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                result[key] = value
    except FileNotFoundError:
        pass
    return result


_env = _load_env(_ENV_PATH)


def get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _env.get(key, default)


# Resolved values
CLAUDE_PATH: str = get("CLAUDE_PATH", "claude")
PROJECT_DIR: str = get("PROJECT_DIR", str(Path.cwd()))
USER_NAME: str = get("USER_NAME", "")
USER_TIMEZONE: str = get("USER_TIMEZONE", "UTC")
USER_ID: str = get("TELEGRAM_USER_ID", "lynn")

SUPABASE_URL: str = get("SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = get("SUPABASE_ANON_KEY", "")

# Socket paths (SOCKET_DIR overridable for multiple session instances)
SOCKET_DIR: str = get("SOCKET_DIR", "/tmp/cognitive-hq")
USER_INPUT_SOCK: str = f"{SOCKET_DIR}/user_input.sock"
CLAUDE_RESPONSE_SOCK: str = f"{SOCKET_DIR}/claude_response.sock"
DISPLAY_SOCK: str = f"{SOCKET_DIR}/display.sock"
CLI_INPUT_SOCK: str = f"{SOCKET_DIR}/cli_input.sock"
PERMISSION_SOCK: str = f"{SOCKET_DIR}/permission.sock"

# Runtime state dir
RELAY_DIR: str = get("RELAY_DIR", str(Path.home() / ".claude-relay"))
SESSION_ID_FILE: str = f"{RELAY_DIR}/session_id"
LOCK_FILE: str = f"{RELAY_DIR}/session_manager.lock"
SENTINEL_FILE: str = f"{RELAY_DIR}/sentinel"

# Optional usage limits (set in .env to enable % display in /usage)
# e.g. USAGE_5H_LIMIT=10000  USAGE_WEEK_LIMIT=100000
# Leave unset (0) to show raw counts only.

# Set SKIP_MEMORY_FETCH=1 to disable personal memory injection (e.g. for isolated sessions)
SKIP_MEMORY_FETCH: bool = get("SKIP_MEMORY_FETCH", "").lower() in ("1", "true", "yes")

# Channel name used when saving messages to Supabase. Override per-session to avoid cross-contamination.
SESSION_CHANNEL: str = get("SESSION_CHANNEL", "telegram")

# Comma-separated extra Telegram user IDs allowed to send messages (beyond TELEGRAM_USER_ID)
EXTRA_USER_IDS: str = get("TELEGRAM_EXTRA_USER_IDS", "")

# Proactive check-in interval in seconds (default: 10 min)
PROACTIVE_INTERVAL: int = int(get("PROACTIVE_INTERVAL", "600") or "600")
PROACTIVE_ENABLED: bool = get("PROACTIVE_ENABLED", "1").lower() not in ("0", "false", "no")
