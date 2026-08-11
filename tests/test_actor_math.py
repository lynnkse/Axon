from datetime import datetime, timedelta, timezone

from actor_model.actors.anton import AntonActor
from actor_model.actors.axon import AxonActor, kalman_delta
from actor_model.types import ActorRecord, Assignment, Disposition, Event, TransitionContext


BASE={"observations":[],"beliefs":[],"decisions":[],"commitments":[],"unresolved_questions":[],"proposed_actions":[],"completed_actions":[]}


def record(kind, state):
    return ActorRecord(f"rog:{kind}",kind,"rog",0,{**BASE,**state},{},Disposition.READY_AGAIN,dirty=True)


def event(kind,payload,seq=1):
    return Event(kind,"rog","test",payload,{},(Assignment("rog:axon"),),sequence=seq,id=str(seq))


def test_deployed_kalman_characterization():
    mu,sigma=kalman_delta(.1,.2,.15,.12)
    assert round(mu,6)==round(.1+(.04/(.04+.0144))*.15,6)
    assert sigma >= .05


def test_axon_homeostasis_and_entropy_exact():
    a=record("axon",{"tick":3,"valence":1.,"valence_sigma":.2,"arousal":-.5,"arousal_sigma":.2,"tension":.5,"background_affect":.4})
    out=AxonActor().transition(a,[event("interaction_received",{})],TransitionContext("x",datetime.now(timezone.utc)))
    assert out.state["tick"]==4
    assert out.state["valence"]==1.+.02*(.15-1.)
    assert out.state["arousal"]==-.5+.02*(0-(-.5))
    assert out.state["tension"]==.49 and out.state["background_affect"]==.392


def test_goal_and_explicit_empathy_are_events():
    a=record("axon",{"valence":0.,"valence_sigma":.2,"arousal":0.,"arousal_sigma":.2})
    out=AxonActor().transition(a,[event("goal_completed",{"count":3}),event("high_confidence_valence_report",{"valence":-1},2)],TransitionContext("x",datetime.now(timezone.utc)))
    assert out.state["valence"] < .15


def test_anton_explicit_only_baseline_and_emission():
    now=datetime.now(timezone.utc)
    base={"valence":.2,"valence_sigma":.3,"energy":0.,"energy_sigma":.3,"baseline_valence":.2,"baseline_energy":0.}
    inferred=event("anton_state_observed",{"valence":-.8,"explicit":False})
    r=AntonActor().transition(record("anton",base),[inferred],TransitionContext("a",now))
    assert r.state["baseline_valence"]==.2 and not r.emitted_events
    explicit=event("anton_state_observed",{"valence":-.8,"explicit":True})
    r=AntonActor().transition(record("anton",base),[explicit],TransitionContext("b",now+timedelta(hours=1)))
    assert r.state["baseline_valence"] < .2
    assert [e.event_type for e in r.emitted_events]==["high_confidence_valence_report"]
