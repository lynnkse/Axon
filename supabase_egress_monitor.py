#!/usr/bin/env python3
"""Hourly monitor for locally observable Axon -> Supabase response traffic."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import config
import supabase_client

from dashboard.supabase_egress_telemetry import summarize


log = logging.getLogger(__name__)
ACTOR_ID = "supabase-egress-monitor"
CHECK_INTERVAL_SECONDS = 60 * 60
RETRY_INTERVAL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _create_actor_if_missing() -> dict | None:
    rows = supabase_client.fetch_prompt_actor_states()
    if rows is None:
        return None
    row = next((item for item in rows if item.get("actor_id") == ACTOR_ID), None)
    if row:
        return row if row.get("instance") in (None, "", config.INSTANCE) else None
    now = _now().isoformat().replace("+00:00", "Z")
    payload = {
        "actor_id": ACTOR_ID,
        "actor_type": ACTOR_ID,
        "instance": config.INSTANCE,
        "state_schema_version": 1,
        "revision": 0,
        "state": {
            "scope": "ROG Axon dashboard Supabase REST response bodies only",
            "billed_organization_egress_status": "unavailable",
            "billed_organization_egress_blocker": (
                "Supabase organization usage is not exposed by the installed project credentials"
            ),
        },
        "directory_projection": {"summary": "Supabase egress monitor initializing", "status": "running"},
        "disposition": "ready_again",
        "last_advanced_at": now,
        "dirty": False,
    }
    req = urllib.request.Request(
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/actor_state",
        data=json.dumps(payload).encode(), method="POST", headers={
            "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            created = json.loads(response.read().decode())
        return created[0] if isinstance(created, list) and created else None
    except Exception as exc:
        log.error("Could not register monitor actor: %s", exc)
        return None


def check_once(now: datetime | None = None, *, force: bool = False) -> str:
    now = (now or _now()).astimezone(timezone.utc)
    row = _create_actor_if_missing()
    if row is None:
        return "actor-unavailable"
    state = dict(row.get("state") or {})
    last_check = _parse(state.get("last_check_at"))
    if not force and last_check and (now - last_check).total_seconds() < CHECK_INTERVAL_SECONDS:
        return "not-due"
    local = summarize(now)
    today = local["today_utc"]
    hour = local["last_hour"]
    next_state = {
        "last_check_at": now.isoformat().replace("+00:00", "Z"),
        "scope": "ROG Axon dashboard Supabase REST response bodies only",
        "measurement_semantics": (
            "Observed response-body bytes; excludes TLS/headers, other clients, Storage, "
            "Realtime, Functions, Auth, and all non-dashboard traffic"
        ),
        "local_requests_last_hour": hour["requests"],
        "local_response_bytes_last_hour": hour["response_bytes"],
        "local_requests_today_utc": today["requests"],
        "local_response_bytes_today_utc": today["response_bytes"],
        "top_endpoints_today": local["top_endpoints_today"],
        "latest_local_event_at": local["latest_event_at"],
        "log_path": local["log_path"],
        "billed_organization_egress_status": "unavailable",
        "billed_organization_egress_blocker": (
            "Exact organization-wide billing usage requires Supabase organization usage access; "
            "project service-role keys do not provide it"
        ),
        "free_plan_egress_quota_gb": 5,
    }
    summary = (
        f"Measured {today['response_bytes']} dashboard response bytes in "
        f"{today['requests']} requests today UTC; exact billed organization egress unavailable."
    )
    update = SimpleNamespace(status="running", state=next_state, summary=summary, error_reason=None)
    return "checked" if supabase_client.save_prompt_actor_update(row, update) else "persistence-failed"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [supabase-egress] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("Monitor started: hourly local dashboard response-byte summaries")
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
