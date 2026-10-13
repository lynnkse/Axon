"""Low-overhead, credential-free telemetry for Supabase response bodies.

This measures bytes actually returned to Axon components.  It is deliberately
not presented as Supabase's organization-wide billed egress: headers, TLS,
other clients, and non-REST products are outside this local observation point.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


LOG_PATH = Path(os.environ.get(
    "AXON_SUPABASE_EGRESS_LOG",
    str(Path.home() / "Axon/logs/supabase_egress.jsonl"),
))
MAX_LOG_BYTES = 10 * 1024 * 1024


def _safe_endpoint(url: str) -> str:
    """Return only a service/table path; never retain queries or credentials."""
    path = urlsplit(url).path
    marker = "/rest/v1/"
    if marker in path:
        table = path.split(marker, 1)[1].split("/", 1)[0]
        return f"rest/{table}"
    return path.strip("/") or "unknown"


def record_response(*, component: str, url: str, response_bytes: int,
                    status: int, duration_ms: float) -> None:
    """Append one sanitized request observation; telemetry failure is harmless."""
    event = {
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "component": component,
        "endpoint": _safe_endpoint(url),
        "response_bytes": max(0, int(response_bytes)),
        "status": int(status),
        "duration_ms": round(max(0.0, float(duration_ms)), 1),
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size >= MAX_LOG_BYTES:
            rotated = LOG_PATH.with_suffix(".jsonl.1")
            rotated.unlink(missing_ok=True)
            LOG_PATH.replace(rotated)
        line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # Observability must never break the dashboard request it observes.
        return


def summarize(now: datetime | None = None) -> dict:
    """Summarize the rolling hour and local calendar day from retained events."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hour_start = now - timedelta(hours=1)
    day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    totals = {
        "last_hour": {"requests": 0, "response_bytes": 0},
        "today_utc": {"requests": 0, "response_bytes": 0},
    }
    endpoints: Counter[str] = Counter()
    latest = None
    for path in (LOG_PATH.with_suffix(".jsonl.1"), LOG_PATH):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            continue
        for line in lines:
            try:
                event = json.loads(line)
                when = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00"))
                size = max(0, int(event.get("response_bytes", 0)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if when >= day_start:
                totals["today_utc"]["requests"] += 1
                totals["today_utc"]["response_bytes"] += size
                endpoints[str(event.get("endpoint", "unknown"))] += size
            if when >= hour_start:
                totals["last_hour"]["requests"] += 1
                totals["last_hour"]["response_bytes"] += size
            if latest is None or when > latest:
                latest = when
    totals["top_endpoints_today"] = dict(endpoints.most_common(10))
    totals["latest_event_at"] = (
        latest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if latest else None
    )
    totals["log_path"] = str(LOG_PATH)
    return totals
