#!/usr/bin/env python3
"""Read-only relay of one confirmed Codex JSONL session into one Slack thread.

The watcher never writes to the source host or tmux pane. It forwards only
assistant message records explicitly marked as commentary or final_answer;
reasoning records, tool calls, and tool outputs are ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("axon.astra_slack_watcher")
ALLOWED_PHASES = frozenset({"commentary", "final_answer"})
TOKEN_PATTERNS = (
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*\s*[=:]\s*)[^\s]+"),
)


def redact(text: str) -> str:
    value = text
    for pattern in TOKEN_PATTERNS:
        if pattern.groups:
            value = pattern.sub(lambda m: m.group(1) + "[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value


def extract_public_message(record: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    if payload.get("role") != "assistant" or payload.get("phase") not in ALLOWED_PHASES:
        return None
    pieces: List[str] = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("type") in {"output_text", "text"}:
            value = item.get("text")
            if isinstance(value, str):
                pieces.append(value)
    text = "".join(pieces).strip()
    if not text:
        return None
    return str(payload["phase"]), redact(text)


def chunks(text: str, limit: int = 3400) -> Iterable[str]:
    remaining = text
    while len(remaining) > limit:
        split = remaining.rfind("\n", 0, limit)
        if split < limit // 2:
            split = limit
        yield remaining[:split]
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        yield remaining


class SlackPoster:
    def __init__(self, token: str, channel: str, thread_ts: str):
        self.token = token
        self.channel = channel
        self.thread_ts = thread_ts

    def post(self, text: str) -> str:
        body = json.dumps({
            "channel": self.channel,
            "thread_ts": self.thread_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }).encode("utf-8")
        request = Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("Slack post failed") from exc
        if not result.get("ok"):
            raise RuntimeError("Slack rejected post: " + str(result.get("error", "unknown_error")))
        return str(result.get("ts") or "")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError("missing required environment variable: " + name)
    return value


def load_state(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        LOG.exception("Could not read watcher state")
        return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(str(temporary), 0o600)
    temporary.replace(path)


def remote_max_ordinal(host: str, session_file: str) -> int:
    command = "tail -n 1 " + shlex.quote(session_file)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
        check=True, capture_output=True, text=True, timeout=30,
    )
    record = json.loads(result.stdout)
    return int(record.get("ordinal", -1))


def verify_source(host: str, pane: str, session_file: str, session_id: str) -> None:
    if session_id not in session_file:
        raise RuntimeError("configured transcript does not match the session id")
    read_check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
         "test -r " + shlex.quote(session_file)],
        capture_output=True, text=True, timeout=30,
    )
    if read_check.returncode != 0:
        raise RuntimeError("remote transcript is not readable")
    command = (
        "tmux display-message -p -t " + shlex.quote(pane) + " " +
        shlex.quote("#{session_name}:#{window_index}.#{pane_index}")
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
        check=True, capture_output=True, text=True, timeout=30,
    )
    if result.stdout.strip() != pane:
        raise RuntimeError("remote tmux pane identity mismatch")


def source_process(host: str, session_file: str) -> subprocess.Popen:
    command = "exec tail -n +1 -F " + shlex.quote(session_file)
    return subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
         "-o", "ServerAliveCountMax=3", host, command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )


def run() -> None:
    source_host = required("ASTRA_RELAY_SOURCE_HOST")
    pane = required("ASTRA_RELAY_PANE")
    session_id = required("ASTRA_RELAY_SESSION_ID")
    session_file = required("ASTRA_RELAY_SESSION_FILE")
    channel = required("ASTRA_RELAY_CHANNEL_ID")
    thread_ts = required("ASTRA_RELAY_THREAD_TS")
    state_path = Path(os.environ.get(
        "ASTRA_RELAY_STATE_FILE",
        str(Path.home() / ".local/state/axon/astra_slack_watcher.json"),
    ))
    poster = SlackPoster(required("SLACK_BOT_TOKEN"), channel, thread_ts)
    label = f"host={source_host} | pane={pane} | session={session_id}"

    verify_source(source_host, pane, session_file, session_id)
    state = load_state(state_path)
    if state.get("session_id") not in {None, session_id}:
        raise RuntimeError("state belongs to a different session")
    if "last_ordinal" not in state:
        state = {
            "session_id": session_id,
            "last_ordinal": remote_max_ordinal(source_host, session_file),
            "health_posted": False,
        }
        save_state(state_path, state)
    if not state.get("health_posted"):
        poster.post(
            f"[Astra relay health | {label}]\n"
            "ACTIVE — read-only structured-output watcher connected. Earlier history was not replayed; "
            "forwarding begins after this health marker. Only public commentary/final output is forwarded. "
            "Reasoning and raw tool payloads are excluded. Artifact upload is unavailable with the bot's "
            "current Slack scopes; referenced artifact paths remain in forwarded text."
        )
        state["health_posted"] = True
        save_state(state_path, state)

    while True:
        process = source_process(source_host, session_file)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    record = json.loads(line)
                    ordinal = int(record.get("ordinal", -1))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if ordinal <= int(state.get("last_ordinal", -1)):
                    continue
                public = extract_public_message(record)
                if public:
                    phase, message = public
                    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
                    if digest != state.get("last_message_sha256"):
                        parts = list(chunks(message))
                        for index, part in enumerate(parts, 1):
                            suffix = f" | part={index}/{len(parts)}" if len(parts) > 1 else ""
                            poster.post(f"[Astra relay | {label} | phase={phase}{suffix}]\n{part}")
                        state["last_message_sha256"] = digest
                state["last_ordinal"] = ordinal
                save_state(state_path, state)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        LOG.warning("Remote transcript stream ended; reconnecting")
        time.sleep(3)


def self_test() -> None:
    record = {
        "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "public xoxb-secret"}],
        },
    }
    assert extract_public_message(record) == ("commentary", "public [REDACTED]")
    record["payload"]["phase"] = "analysis"
    assert extract_public_message(record) is None
    assert list(chunks("a" * 7000, 3400)) == ["a" * 3400, "a" * 3400, "a" * 200]
    print("self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        LOG.exception("Astra Slack watcher stopped")
        sys.exit(1)
