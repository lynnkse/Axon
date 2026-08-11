from datetime import datetime, timedelta, timezone

from actor_model.actions import enforce_authorization
from actor_model.eligibility import eligibility_reason
from actor_model.obligations import eligible, escalation_level
from actor_model.scheduler import Scheduler, decay_dormant_debt
from actor_model.types import ActorRecord, Disposition


def actor(name="a", **kw):
    args=dict(actor_id=name,actor_type="axon",instance="rog",revision=0,state={},directory_projection={},disposition=Disposition.READY_AGAIN,dirty=True)
    args.update(kw); return ActorRecord(**args)


def test_blocked_is_ineligible(): assert eligibility_reason(actor(blocked_reason="human"))[0] is False
def test_budget_is_hard_gate(): assert eligibility_reason(actor(quantum_tokens=0))[1]=="budget"


def test_scheduler_debt_and_nice():
    a,b=actor("a",service_debt=0),actor("b",service_debt=2)
    s=Scheduler(); assert s.rank([a,b])[0].actor_id=="b"
    s.account([a,b],"b"); assert a.service_debt==1 and b.service_debt==1


def test_dormant_debt_decays():
    now=datetime.now(timezone.utc); a=actor(service_debt=10,dormant_since=(now-timedelta(days=7)).isoformat())
    assert 4.9 < decay_dormant_debt(a,now) < 5.1


def test_consequential_action_denied_without_separate_authorization():
    action={"kind":"send_message","action_id":"x"}
    assert enforce_authorization([action],[]) == ([],[action])
    assert enforce_authorization([action],[{"event_type":"action_authorized","payload":{"action_id":"x"}}]) == ([action],[])


def test_obligation_spacing_and_escalation():
    now=datetime.now(timezone.utc)
    row={"status":"open","due_at":(now-timedelta(hours=50)).isoformat(),"last_presented_at":None,"escalation_policy":{"overdue_hours":[24,48]}}
    assert eligible(row,now) and escalation_level(row,now)==2
