from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Disposition(str, Enum):
    DORMANT = "dormant"
    READY_AGAIN = "ready_again"
    WAITING_FOR_EVENT = "waiting_for_event"
    WAITING_FOR_HUMAN = "waiting_for_human"
    BLOCKED = "blocked"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Assignment:
    actor_id: str
    assignment_type: str = "input"
    confidence: float = 1.0
    reason: str = ""
    assigned_at: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass(frozen=True)
class Event:
    event_type: str
    source_instance: str
    source_kind: str
    payload: dict[str, Any]
    provenance: dict[str, Any]
    assignments: tuple[Assignment, ...] = ()
    idempotency_key: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=lambda: utcnow().isoformat())
    schema_version: int = 1
    source_actor_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    activation_id: str | None = None
    id: str | None = None
    sequence: int = 0

    def as_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["assignments"] = [asdict(a) for a in self.assignments]
        return {k: v for k, v in value.items() if v is not None and k not in {"id", "sequence"}}


@dataclass
class ActorRecord:
    actor_id: str
    actor_type: str
    instance: str
    revision: int
    state: dict[str, Any]
    directory_projection: dict[str, Any]
    disposition: Disposition
    last_event_sequence: int = 0
    dirty: bool = False
    blocked_reason: str | None = None
    unblock_at: str | None = None
    cooldown_until: str | None = None
    dependencies: list[str] = field(default_factory=list)
    scheduling_class: int = 10
    nice: int = 0
    nice_weight: float = 1.0
    virtual_deadline: float = 0.0
    service_debt: float = 0.0
    dormant_since: str | None = None
    quantum_tokens: int = 4000
    activation_cost_limit: float = 1.0


@dataclass
class TransitionContext:
    activation_id: str
    now: datetime
    max_model_turns: int = 2
    max_tool_batches: int = 1
    max_wall_seconds: int = 600


@dataclass
class TransitionResult:
    state: dict[str, Any]
    directory_projection: dict[str, Any]
    disposition: Disposition
    emitted_events: list[Event] = field(default_factory=list)
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)
    last_event_sequence: int = 0
