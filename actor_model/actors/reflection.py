from __future__ import annotations

from copy import deepcopy
from typing import Callable

from ..contracts import normalize_state
from ..types import Assignment, ActorRecord, Disposition, Event, TransitionContext, TransitionResult


class ReflectionActor:
    actor_type = "reflection"
    def __init__(self, model_runner: Callable[[str, int], str] | None = None) -> None:
        self.model_runner = model_runner

    def transition(self, record: ActorRecord, events: list[Event], context: TransitionContext) -> TransitionResult:
        state = normalize_state(deepcopy(record.state))
        requested = [e for e in events if e.event_type == "reflection_requested"]
        completed = [e for e in events if e.event_type == "reflection_completed"]
        if completed:
            state["last_completed_at"] = context.now.isoformat()
            state["pending_request_event_id"] = None
            state["completed_activations"] = int(state.get("completed_activations", 0)) + len(completed)
            return TransitionResult(state, {"summary":"idle reflection complete"}, Disposition.WAITING_FOR_EVENT)
        if not requested:
            return TransitionResult(state, {"summary":"waiting for idle reflection window"}, Disposition.WAITING_FOR_EVENT)
        output = self.model_runner(self._prompt(record, requested), context.max_wall_seconds) if self.model_runner else ""
        state["last_requested_at"] = context.now.isoformat()
        state["last_completed_at"] = context.now.isoformat()
        state["last_output"] = output[:4000]
        emitted = []
        if output:
            emitted.append(Event("insight_emitted", record.instance, "actor", {"content":output[:4000]},
                {"request_event_id":requested[-1].id},
                (Assignment(f"{record.instance}:anton", confidence=.7, reason="reflection analysis"),),
                f"{context.activation_id}:reflection-output", source_actor_id=record.actor_id,
                activation_id=context.activation_id))
        return TransitionResult(state, {"summary":"idle reflection complete"}, Disposition.WAITING_FOR_EVENT, emitted)

    @staticmethod
    def _prompt(record: ActorRecord, requested: list[Event]) -> str:
        return ("One bounded, private reflection. Do not address Anton. Do not use tools, write files, "
                "send messages, or perform external actions. From the typed state and idle trigger below, "
                "state at most one evidence-grounded insight about serving Anton's well-being or progress; "
                "empty output is valid.\nSTATE: " + repr(record.state)[:12000] +
                "\nTRIGGER: " + repr(requested[-1].payload))
