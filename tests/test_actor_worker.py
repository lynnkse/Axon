from datetime import datetime, timezone

from actor_model.registry import Registry
from actor_model.types import ActorRecord, Disposition, TransitionResult
from actor_model.worker import TransitionWorker


class Actor:
    actor_type="test"
    def transition(self, record, events, context):
        return TransitionResult({**record.state,"ran":True},{"summary":"ran"},Disposition.READY_AGAIN)


class Store:
    def __init__(self, acquire=True, commit=True): self.can_acquire=acquire; self.can_commit=commit; self.releases=0
    def acquire(self,*args): return self.can_acquire
    def events_for(self,actor): return []
    def commit(self,*args): return self.can_commit
    def release(self,*args): self.releases+=1; return True


def rec():
    state={x:[] for x in ("observations","beliefs","decisions","commitments","unresolved_questions","proposed_actions","completed_actions")}
    return ActorRecord("a","test","rog",2,state,{},Disposition.READY_AGAIN,dirty=True)


def worker(store):
    registry=Registry(); registry.register(Actor()); return TransitionWorker(store,registry,"owner")


def test_lease_contention_spends_no_transition():
    store=Store(acquire=False); assert worker(store).activate(rec()) is False and store.releases==0


def test_cas_conflict_returns_false_and_releases_lease():
    store=Store(commit=False); assert worker(store).activate(rec()) is False and store.releases==1


def test_ready_again_commits_once_without_recursing():
    store=Store(); assert worker(store).activate(rec()) is True and store.releases==1
