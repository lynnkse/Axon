from __future__ import annotations

from datetime import datetime, timezone

from .types import ActorRecord, Disposition


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def eligibility_reason(actor: ActorRecord, now: datetime | None = None,
                       satisfied_dependencies: set[str] | None = None) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    satisfied_dependencies = satisfied_dependencies or set()
    if actor.disposition in {Disposition.COMPLETED, Disposition.DORMANT, Disposition.WAITING_FOR_HUMAN}:
        return False, actor.disposition.value
    if actor.disposition == Disposition.BLOCKED or actor.blocked_reason:
        unblock = _time(actor.unblock_at)
        if not unblock or unblock > now:
            return False, "blocked"
    cooldown = _time(actor.cooldown_until)
    if cooldown and cooldown > now:
        return False, "cooldown"
    if any(dep not in satisfied_dependencies for dep in actor.dependencies):
        return False, "dependencies"
    if actor.quantum_tokens <= 0 or actor.activation_cost_limit <= 0:
        return False, "budget"
    if not actor.dirty and actor.disposition != Disposition.READY_AGAIN:
        return False, "clean"
    return True, "eligible"
