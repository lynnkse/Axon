from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .actions import enforce_authorization
from .contracts import normalize_state, validate_event
from .registry import Registry
from .store import Store
from .types import ActorRecord, TransitionContext


class TransitionWorker:
    def __init__(self, store: Store, registry: Registry, owner: str) -> None:
        self.store, self.registry, self.owner = store, registry, owner

    def activate(self, actor: ActorRecord) -> bool:
        activation_id = str(uuid4())
        if not self.store.acquire(actor, self.owner, activation_id):
            return False
        try:
            events = self.store.events_for(actor)
            context = TransitionContext(activation_id, datetime.now(timezone.utc))
            result = self.registry.get(actor.actor_type).transition(actor, events, context)
            result.state = normalize_state(result.state)
            for event in result.emitted_events:
                validate_event(event)
            _, result.proposed_actions = enforce_authorization(result.proposed_actions, [])
            result.state["proposed_actions"].extend(result.proposed_actions)
            if events:
                result.last_event_sequence = max(e.sequence for e in events)
            if len(events) >= 500:
                # Store reads are paged; re-enter scheduling to drain the next page.
                from .types import Disposition
                result.disposition = Disposition.READY_AGAIN
            return self.store.commit(actor, self.owner, activation_id, result)
        finally:
            self.store.release(actor.actor_id, self.owner, activation_id)
