from __future__ import annotations

from typing import Protocol

import supabase_client

from .types import ActorRecord, Disposition, Event, TransitionResult


class Store(Protocol):
    def list_actors(self) -> list[ActorRecord]: ...
    def events_for(self, actor: ActorRecord) -> list[Event]: ...
    def acquire(self, actor: ActorRecord, owner: str, activation_id: str) -> bool: ...
    def commit(self, actor: ActorRecord, owner: str, activation_id: str, result: TransitionResult) -> bool: ...
    def release(self, actor_id: str, owner: str, activation_id: str) -> bool: ...
    def save_schedule(self, actor: ActorRecord) -> bool: ...


class SupabaseActorStore:
    def list_actors(self) -> list[ActorRecord]:
        return [self._actor(r) for r in supabase_client.fetch_actor_states()]

    def events_for(self, actor: ActorRecord) -> list[Event]:
        return [self._event(r) for r in supabase_client.fetch_actor_events(actor.actor_id, actor.last_event_sequence)]

    def acquire(self, actor: ActorRecord, owner: str, activation_id: str) -> bool:
        return bool(supabase_client.acquire_actor_lease(actor.actor_id, actor.revision, owner, activation_id))

    def commit(self, actor: ActorRecord, owner: str, activation_id: str, result: TransitionResult) -> bool:
        return bool(supabase_client.commit_actor_transition(
            actor.actor_id, actor.revision, owner, activation_id, result.state,
            result.directory_projection, result.disposition.value, result.last_event_sequence,
            [e.as_payload() for e in result.emitted_events]))

    def release(self, actor_id: str, owner: str, activation_id: str) -> bool:
        return supabase_client.release_actor_lease(actor_id, owner, activation_id)

    def save_schedule(self, actor: ActorRecord) -> bool:
        return supabase_client.save_actor_schedule(actor.actor_id, actor.revision,
            actor.virtual_deadline, actor.service_debt, actor.nice_weight)

    @staticmethod
    def _actor(r: dict) -> ActorRecord:
        fields = ActorRecord.__dataclass_fields__
        data = {k: v for k, v in r.items() if k in fields}
        data["disposition"] = Disposition(data["disposition"])
        return ActorRecord(**data)

    @staticmethod
    def _event(r: dict) -> Event:
        from .types import Assignment
        return Event(**{k: v for k, v in r.items() if k in Event.__dataclass_fields__ and k != "assignments"},
                     assignments=tuple(Assignment(**a) for a in r.get("assignments", [])))
