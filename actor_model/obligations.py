from __future__ import annotations

from datetime import datetime, timezone


def eligible(row: dict, now: datetime | None = None, interaction: dict | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if row.get("status") != "open":
        return False
    due = row.get("due_at")
    if due and datetime.fromisoformat(due.replace("Z", "+00:00")) > now:
        return False
    last = row.get("last_presented_at")
    if last:
        spacing = float(row.get("min_spacing_seconds", 86400))
        if (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < spacing:
            return False
    qualifier = row.get("qualifying_interaction") or {}
    if qualifier.get("sources") and (interaction or {}).get("source") not in qualifier["sources"]:
        return False
    return True


def escalation_level(row: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    policy = row.get("escalation_policy") or {}
    due = row.get("due_at")
    if not due:
        return int(row.get("escalation_level", 0))
    overdue_h = max(0, (now - datetime.fromisoformat(due.replace("Z", "+00:00"))).total_seconds() / 3600)
    thresholds = sorted(float(x) for x in policy.get("overdue_hours", []))
    return sum(overdue_h >= t for t in thresholds)
