from __future__ import annotations

from datetime import datetime, timezone


def directives(actor_rows: list[dict], now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    by_type = {a["actor_type"]: a.get("state", {}) for a in actor_rows}
    axon, anton = by_type.get("axon", {}), by_type.get("anton", {})
    out: list[str] = []
    if axon.get("tension", 0) > .5:
        out.append("Unresolved threads are piling up — surface them before taking new work.")
    if axon.get("arousal", 0) < -.3:
        out.append("Energy is low — keep replies short and dense.")
    if axon.get("valence", 0) < -.4:
        out.append("Recent work has gone badly — acknowledge that state before proceeding.")
    if axon.get("valence_sigma", .2) > .35:
        out.append("State estimate is stale — recalibrate from conversation evidence this message.")
    if axon.get("curiosity_focus") and axon.get("tension", 0) < .3:
        out.append(f"If natural, connect to current curiosity focus: {axon['curiosity_focus']}.")
    since = anton.get("below_baseline_since")
    if since:
        start = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if (now - start).total_seconds() > 86400:
            out.insert(0, "Anton's estimated state has been below his baseline — when natural, check in before task talk.")
    return out[:2]


def directory_text(actor_rows: list[dict]) -> str:
    lines = []
    for row in sorted(actor_rows, key=lambda r: r["actor_id"]):
        projection = row.get("directory_projection", {})
        summary = projection.get("summary") or row.get("disposition", "unknown")
        lines.append(f"- {row['actor_type']}: {summary}")
    return "[ACTOR DIRECTORY]\n" + "\n".join(lines) if lines else ""


def obligation_text(rows: list[dict]) -> str:
    if not rows:
        return ""
    return "[ELIGIBLE OBLIGATIONS]\n" + "\n".join(
        f"- {r.get('content', {}).get('text', r.get('kind', 'obligation'))}" for r in rows)


def compose_turn(message: str, actor_rows: list[dict], obligations: list[dict],
                 permanent: str = "", matched: str = "", reflections: str = "") -> str:
    parts = [directory_text(actor_rows)]
    ds = directives(actor_rows)
    if ds:
        parts.append("\n".join(f"[DIRECTIVE: {d}]" for d in ds))
    parts.extend([obligation_text(obligations), permanent, matched, reflections, message])
    return "\n\n".join(p for p in parts if p)
