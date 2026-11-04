#!/usr/bin/env python3
"""Private Slack Socket Mode bridge for Axon's localhost-only gateway.

It accepts only ordinary messages from one configured private channel, creates
an idempotent gateway task, then writes the terminal Axon reply into the same
Slack thread. Socket Mode makes the connection outbound-only: no public HTTP
listener or OpenAI API credential is involved.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("axon.slack")
TERMINAL = frozenset({"succeeded", "failed", "timed_out"})


@dataclass(frozen=True)
class Settings:
    bot_token: str
    app_token: str
    team_id: str
    channel_id: str
    gateway_token: str
    gateway_url: str
    allowed_users: frozenset[str]
    allowed_bot_ids: frozenset[str]
    allowed_app_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", ""),
            "SLACK_APP_TOKEN": os.environ.get("SLACK_APP_TOKEN", ""),
            "SLACK_AXON_TEAM_ID": os.environ.get("SLACK_AXON_TEAM_ID", ""),
            "SLACK_AXON_CHANNEL_ID": os.environ.get("SLACK_AXON_CHANNEL_ID", ""),
            "AXON_GATEWAY_TOKEN": os.environ.get("AXON_GATEWAY_TOKEN", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))
        if len(required["AXON_GATEWAY_TOKEN"]) < 32:
            raise ValueError("AXON_GATEWAY_TOKEN must contain at least 32 characters")
        def id_set(name: str) -> frozenset[str]:
            return frozenset(value.strip() for value in os.environ.get(name, "").split(",") if value.strip())

        users = id_set("SLACK_AXON_ALLOWED_USER_IDS")
        bot_ids = id_set("SLACK_AXON_ALLOWED_BOT_IDS")
        app_ids = id_set("SLACK_AXON_ALLOWED_APP_IDS")
        if not (users or bot_ids or app_ids):
            raise ValueError(
                "configure at least one authorized Slack sender in "
                "SLACK_AXON_ALLOWED_USER_IDS, SLACK_AXON_ALLOWED_BOT_IDS, or "
                "SLACK_AXON_ALLOWED_APP_IDS"
            )
        return cls(
            bot_token=required["SLACK_BOT_TOKEN"], app_token=required["SLACK_APP_TOKEN"],
            team_id=required["SLACK_AXON_TEAM_ID"],
            channel_id=required["SLACK_AXON_CHANNEL_ID"], gateway_token=required["AXON_GATEWAY_TOKEN"],
            gateway_url=os.environ.get("AXON_GATEWAY_URL", "http://127.0.0.1:8767").rstrip("/"),
            allowed_users=users, allowed_bot_ids=bot_ids, allowed_app_ids=app_ids,
        )


def accepted_event(
    event: dict[str, Any], settings: Settings, *, own_bot_id: str | None = None,
    event_team_id: str | None = None,
) -> bool:
    """Accept only an explicitly authorized sender in the configured channel.

    ChatGPT Work may post through either the user's Slack identity or an app/bot
    identity. Both forms therefore have explicit allowlists. The bridge's own
    bot identity is always rejected to prevent reply loops.
    """
    if (
        event.get("type") != "message"
        or event_team_id != settings.team_id
        or event.get("channel") != settings.channel_id
        or event.get("channel_type") != "group"
        or not str(event.get("text", "")).strip()
        or event.get("hidden")
    ):
        return False

    bot_id = str(event.get("bot_id") or "")
    app_id = str(event.get("app_id") or "")
    subtype = event.get("subtype")
    if bot_id or app_id:
        return bool(
            subtype in {None, "bot_message"}
            and bot_id != own_bot_id
            and (bot_id in settings.allowed_bot_ids or app_id in settings.allowed_app_ids)
        )
    return bool(subtype is None and event.get("user") in settings.allowed_users)


class GatewayClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.settings.gateway_url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.settings.gateway_token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            raise RuntimeError(f"gateway HTTP {exc.code}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("gateway unavailable") from exc

    def submit(self, event: dict[str, Any]) -> str:
        ts = str(event["ts"])
        channel = str(event["channel"])
        payload = {
            "conversation_id": f"slack:{channel}:{event.get('thread_ts', ts)}",
            "idempotency_key": f"slack:{channel}:{ts}",
            "target": "axon-main", "text": str(event["text"]).strip(), "timeout_seconds": 600,
        }
        return str(self._request("POST", "/v1/tasks", payload)["task_id"])

    def task_events(self, task_id: str, after: int) -> dict[str, Any]:
        return self._request("GET", f"/v1/tasks/{task_id}/events?after={after}")

    def relay_task(self, task_id: str, on_event) -> dict[str, Any]:
        """Poll the bounded public event feed and return the terminal result."""
        deadline = time.monotonic() + 620
        cursor = 0
        while time.monotonic() < deadline:
            event_batch = self.task_events(task_id, cursor)
            for event in event_batch.get("events", []):
                on_event(event)
            cursor = int(event_batch.get("next_after", cursor))
            task = self._request("GET", f"/v1/tasks/{task_id}/result")
            if task.get("status") in TERMINAL:
                return task
            time.sleep(0.8)
        return {"status": "timed_out", "error": "Axon gateway wait timed out"}


def format_progress(event: dict[str, Any]) -> str:
    """Format session-manager public activity for a Slack thread."""
    message = str(event.get("message") or "").strip()
    detail = str(event.get("detail") or "").strip()
    kind = str(event.get("kind") or "progress").strip()
    if message:
        return "[progress]\n" + message[:3500]
    if detail:
        label = "running" if kind in {"phase", "running"} else "activity"
        return f"[{label}] {detail[:1000]}"
    return ""


def build_app(settings: Settings):
    try:
        from slack_bolt import App
    except ImportError as exc:  # permits static/unit checks without tokens or Slack installed
        raise RuntimeError("install slack-bolt from requirements.txt") from exc
    app = App(token=settings.bot_token)
    client = GatewayClient(settings)
    identity = app.client.auth_test()
    if str(identity.get("team_id") or "") != settings.team_id:
        raise RuntimeError("Slack bot token belongs to the wrong workspace")
    own_bot_id = str(identity.get("bot_id") or "")
    if not own_bot_id:
        raise RuntimeError("Slack bot identity could not be verified")

    @app.event("message")
    def on_message(event: dict[str, Any], body: dict[str, Any], say) -> None:
        if not accepted_event(
            event,
            settings,
            own_bot_id=own_bot_id,
            event_team_id=str(body.get("team_id") or ""),
        ):
            return
        thread_ts = str(event.get("thread_ts") or event["ts"])
        try:
            task_id = client.submit(event)
        except Exception:
            LOG.exception("Slack gateway submission failed")
            say(text="Axon bridge could not accept that request.", thread_ts=thread_ts)
            return
        say(text=f"Axon accepted task `{task_id}`. The result will appear in this thread.", thread_ts=thread_ts)

        def relay_result() -> None:
            try:
                last_progress = None

                def post_progress(event: dict[str, Any]) -> None:
                    nonlocal last_progress
                    text = format_progress(event)
                    if text and text != last_progress:
                        say(text=text, thread_ts=thread_ts)
                        last_progress = text

                result = client.relay_task(task_id, post_progress)
                text = result.get("result") if result.get("status") == "succeeded" else f"Axon request failed: {result.get('error', 'unknown error')}"
                say(text="[result]\n" + str(text)[:16000], thread_ts=thread_ts)
            except Exception:
                LOG.exception("Slack gateway result relay failed")
                say(text="Axon bridge lost the result while waiting.", thread_ts=thread_ts)

        threading.Thread(target=relay_result, name=f"slack-axon-{task_id[:8]}", daemon=True).start()

    return app


def main() -> None:
    settings = Settings.from_env()
    if not settings.gateway_url.startswith("http://127.0.0.1:"):
        raise SystemExit("AXON_GATEWAY_URL must remain localhost-only")
    app = build_app(settings)
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    LOG.info("Starting private Slack Socket Mode adapter for configured channel")
    SocketModeHandler(app, settings.app_token).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
