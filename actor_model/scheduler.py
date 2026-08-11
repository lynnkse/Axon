from __future__ import annotations

from datetime import datetime, timezone
from math import exp

from .eligibility import eligibility_reason
from .types import ActorRecord


def nice_to_weight(nice: int) -> float:
    return 1.25 ** (-nice)


def decay_dormant_debt(actor: ActorRecord, now: datetime, half_life_days: float = 7.0) -> float:
    if not actor.dormant_since:
        return actor.service_debt
    then = datetime.fromisoformat(actor.dormant_since.replace("Z", "+00:00"))
    days = max(0.0, (now - then).total_seconds() / 86400)
    return actor.service_debt * exp(-0.693147 * days / half_life_days)


class Scheduler:
    def rank(self, actors: list[ActorRecord], now: datetime | None = None) -> list[ActorRecord]:
        now = now or datetime.now(timezone.utc)
        eligible = [a for a in actors if eligibility_reason(a, now)[0]]
        for actor in eligible:
            actor.service_debt = decay_dormant_debt(actor, now)
            actor.nice_weight = nice_to_weight(actor.nice)
        return sorted(eligible, key=lambda a: (
            a.scheduling_class, a.virtual_deadline, -a.service_debt,
            -a.nice_weight, a.actor_id,
        ))

    def account(self, actors: list[ActorRecord], selected_id: str | None, quantum: float = 1.0) -> None:
        for actor in actors:
            if not eligibility_reason(actor)[0]:
                continue
            if actor.actor_id == selected_id:
                actor.service_debt = max(0.0, actor.service_debt - quantum)
                actor.virtual_deadline += quantum / max(actor.nice_weight, 0.01)
            else:
                actor.service_debt += quantum
