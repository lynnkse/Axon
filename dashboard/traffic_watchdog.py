#!/usr/bin/env python3
"""Summarize dashboard traffic logs and flag repeated unchanged network traffic."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_at(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    events = []
    try:
        for line in args.log.open(encoding="utf-8"):
            try:
                event = json.loads(line)
                event_at = parse_at(event.get("at"))
                if event_at and event_at >= since:
                    events.append(event)
            except ValueError:
                continue
    except FileNotFoundError:
        pass

    runs = [e for e in events if e.get("event") == "dashboard_run_end"]
    network = [e for e in events if e.get("event") == "supabase_network_request"]
    queries = [e for e in events if e.get("event") == "dashboard_query"]
    assets = [e for e in events if e.get("event") == "dashboard_inline_asset"]
    prior_by_session = {}
    unchanged = 0
    for run in runs:
        previous = prior_by_session.get(run.get("session"))
        if previous and previous == run.get("fingerprint"):
            unchanged += 1
        prior_by_session[run.get("session")] = run.get("fingerprint")

    run_count = len(runs)
    network_count = len(network)
    response_bytes = sum(int(e.get("response_bytes") or 0) for e in network)
    inline_bytes = sum(int(e.get("estimated_payload_bytes") or 0) for e in assets)
    unchanged_ratio = unchanged / run_count if run_count else 0.0
    request_per_run = network_count / run_count if run_count else 0.0
    if run_count >= 4 and unchanged_ratio >= 0.6 and request_per_run >= 1:
        level = "review"
        reason = "Repeated unchanged dashboard runs still made Supabase requests."
    elif run_count >= 8 and unchanged_ratio >= 0.7:
        level = "watch"
        reason = "Repeated unchanged dashboard runs detected; cache reuse kept network requests low."
    else:
        level = "ok"
        reason = "No repeated network-traffic pattern above the review threshold."

    summary = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "window_hours": 24,
        "dashboard_runs": run_count,
        "browser_sessions": len({e.get("session") for e in runs if e.get("session")}),
        "supabase_requests": network_count,
        "supabase_response_bytes": response_bytes,
        "supabase_response_megabytes": round(response_bytes / 1_000_000, 3),
        "dashboard_query_renders": len(queries),
        "estimated_inline_payload_bytes": inline_bytes,
        "estimated_inline_payload_megabytes": round(inline_bytes / 1_000_000, 3),
        "unchanged_runs": unchanged,
        "unchanged_run_ratio": round(unchanged_ratio, 3),
        "network_requests_per_run": round(request_per_run, 3),
        "status": level,
        "reason": reason,
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.status.parent, delete=False) as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, args.status)


if __name__ == "__main__":
    main()
