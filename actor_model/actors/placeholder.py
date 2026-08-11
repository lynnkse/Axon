from ..types import ActorRecord, Disposition, Event, TransitionContext, TransitionResult


class PlaceholderActor:
    def __init__(self, actor_type: str): self.actor_type = actor_type
    def transition(self, record: ActorRecord, events: list[Event], context: TransitionContext) -> TransitionResult:
        return TransitionResult(record.state, {"summary":"placeholder; dormant"}, Disposition.DORMANT)
