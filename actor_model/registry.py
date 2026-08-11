from __future__ import annotations

from typing import Protocol

from .types import ActorRecord, Event, TransitionContext, TransitionResult


class Actor(Protocol):
    actor_type: str
    def transition(self, record: ActorRecord, events: list[Event], context: TransitionContext) -> TransitionResult: ...


class Registry:
    def __init__(self) -> None:
        self._actors: dict[str, Actor] = {}
    def register(self, actor: Actor) -> None:
        self._actors[actor.actor_type] = actor
    def get(self, actor_type: str) -> Actor:
        return self._actors[actor_type]
