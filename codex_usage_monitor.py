#!/usr/bin/env python3
"""Deterministic hourly Codex weekly-usage monitor.

The monitor advances the shared Supabase actor without invoking the language
model. It sends one Telegram alert per weekly quota window when usage first
reaches the configured threshold.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import config
import supabase_client


log = logging.getLogger(__name__)
ACTOR_ID = "codex-usage-monitor"
CHECK_INTERVAL_SECONDS = 60 * 60
RETRY_INTERVAL_SECONDS = 60
DEFAULT_THRESHOLD_PERCENT = 85.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _actor_row() -> dict | None:
    rows = supabase_client.fetch_prompt_actor_states()
    if rows is None:
        return None
    row = next((row for row in rows if row.get("actor_id") == ACTOR_ID), None)
    if row and row.get("instance") not in (None, "", config.INSTANCE):
        log.debug("Usage actor belongs to instance %s, not %s", row.get("instance"), config.INSTANCE)
        return None
    return row


def _reset_label(reset_epoch: int) -> str:
    if not reset_epoch:
        return "unknown"
    tz = ZoneInfo(config.USER_TIMEZONE or "UTC")
    reset = datetime.fromtimestamp(reset_epoch, timezone.utc).astimezone(tz)
    return reset.strftime("%d %b at %H:%M %Z")


def send_telegram_alert(text: str) -> bool:
    token = config.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        log.error("Usage alert unavailable: Telegram credentials are not configured")
        return False
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
        return bool(payload.get("ok"))
    except Exception as exc:
        log.error("Could not send Codex usage alert: %s", exc)
        return False


def check_once(now: datetime | None = None, send_alert=send_telegram_alert) -> str:
    """Check and persist once if the actor's one-hour interval has elapsed."""
    now = (now or _utc_now()).astimezone(timezone.utc)
    row = _actor_row()
    if row is None:
        return "actor-unavailable"

    state = dict(row.get("state") or {})
    last_check = _parse_timestamp(state.get("last_check_at"))
    if last_check is not None and (now - last_check).total_seconds() < CHECK_INTERVAL_SECONDS:
        return "not-due"

    usage = supabase_client.latest_codex_weekly_usage()
    if usage is None:
        log.warning("No Codex weekly-usage snapshot found")
        return "usage-unavailable"

    used_percent, resets_at = usage
    threshold = float(state.get("threshold_percent") or DEFAULT_THRESHOLD_PERCENT)
    alerted_reset = state.get("alerted_reset_at")
    should_alert = used_percent >= threshold and alerted_reset != resets_at
    alert_sent = False
    if should_alert:
        alert_sent = send_alert(
            f"Codex weekly usage reached {used_percent:g}% "
            f"(alert threshold {threshold:g}%). {100 - used_percent:g}% remains; "
            f"the limit resets {_reset_label(resets_at)}."
        )

    next_state = {
        "last_check_at": now.isoformat().replace("+00:00", "Z"),
        "last_seen_percent": used_percent,
        "resets_at": resets_at,
        "threshold_percent": threshold,
        "alerted_reset_at": resets_at if alert_sent else alerted_reset,
        "first_activation_confirmed": True,
    }
    if should_alert and not alert_sent:
        summary = (f"Usage is {used_percent:g}%, but the {threshold:g}% Telegram alert "
                   "failed and will retry.")
    elif alert_sent:
        summary = f"Usage is {used_percent:g}%; sent the {threshold:g}% alert for this weekly window."
    else:
        summary = f"Hourly check: usage is {used_percent:g}%, below the {threshold:g}% alert threshold."

    update = SimpleNamespace(
        status="running", state=next_state, summary=summary, error_reason=None,
    )
    if not supabase_client.save_prompt_actor_update(row, update):
        return "persistence-failed"
    return "alerted" if alert_sent else "checked"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [codex-usage] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("Monitor started: hourly checks, alert at 85%%")
    while True:
        try:
            result = check_once()
            if result != "not-due":
                log.info("Check result: %s", result)
        except Exception:
            log.exception("Unexpected monitor error")
        time.sleep(RETRY_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
