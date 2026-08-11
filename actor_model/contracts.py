from __future__ import annotations

from .types import Event

EVENT_TYPES = {
    "interaction_received", "affect_tag_emitted", "goal_completed",
    "anton_state_observed", "high_confidence_valence_report",
    "insight_emitted", "reflection_requested", "reflection_completed",
    "action_authorized", "action_completed", "obligation_acknowledged",
}
STATE_SECTIONS = (
    "observations", "beliefs", "decisions", "commitments",
    "unresolved_questions", "proposed_actions", "completed_actions",
)


def validate_event(event: Event) -> None:
    if event.event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event.event_type}")
    if not event.source_instance or not event.source_kind or not event.idempotency_key:
        raise ValueError("event source and idempotency key are required")
    for assignment in event.assignments:
        if not 0.0 <= assignment.confidence <= 1.0:
            raise ValueError("assignment confidence must be in [0, 1]")


def normalize_state(state: dict) -> dict:
    result = dict(state)
    for section in STATE_SECTIONS:
        result.setdefault(section, [])
        if not isinstance(result[section], list):
            raise ValueError(f"state.{section} must be a list")
    return result
