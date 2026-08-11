from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from ..contracts import normalize_state
from ..types import Assignment, ActorRecord, Disposition, Event, TransitionContext, TransitionResult


class AntonActor:
    actor_type = "anton"
    def transition(self, record: ActorRecord, events: list[Event], context: TransitionContext) -> TransitionResult:
        s, emitted = normalize_state(deepcopy(record.state)), []
        for event in events:
            if event.event_type != "anton_state_observed": continue
            p, now = event.payload, context.now
            last = s.get("last_observation_at")
            hours = min(72., max(0., (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()/3600)) if last else 0.
            explicit = bool(p.get("explicit"))
            obs_sigma, drift = (.10 if explicit else .20), min(1., .01 * hours)
            def step(mu, sigma, baseline, z):
                sigma = min(.4, sigma + .005 * hours); mu += drift * (baseline - mu)
                k = sigma**2/(sigma**2+obs_sigma**2); mu = max(-1., min(1., mu+k*(z-mu))); sigma=max(.05,((1-k)**.5)*sigma)
                if explicit: baseline += .03*(z-baseline)
                return mu, sigma, baseline
            if p.get("valence") is not None:
                s["valence"],s["valence_sigma"],s["baseline_valence"] = step(s.get("valence",.2),s.get("valence_sigma",.3),s.get("baseline_valence",.2),float(p["valence"]))
                if explicit:
                    emitted.append(Event("high_confidence_valence_report", record.instance, "actor", {"valence":float(p["valence"])}, {"event_id":event.id,"explicit":True}, (Assignment(f"{record.instance}:axon", confidence=1., reason="explicit Anton report"),), f"{context.activation_id}:empathy:{event.sequence}", source_actor_id=record.actor_id, activation_id=context.activation_id))
            if p.get("energy") is not None:
                s["energy"],s["energy_sigma"],s["baseline_energy"] = step(s.get("energy",0.),s.get("energy_sigma",.3),s.get("baseline_energy",0.),float(p["energy"]))
            s["last_observation_at"] = now.isoformat()
            if s.get("valence",.2) < s.get("baseline_valence",.2)-.25: s.setdefault("below_baseline_since", now.isoformat())
            else: s["below_baseline_since"] = None
            s["observations"] = (s["observations"] + [{"event_id":event.id,"explicit":explicit,"evidence":p.get("evidence")}])[-100:]
        summary=f"V={s.get('valence',.2):+.2f} (base {s.get('baseline_valence',.2):+.2f}, sigma={s.get('valence_sigma',.3):.2f}) E={s.get('energy',0):+.2f}"
        return TransitionResult(s,{"summary":summary},Disposition.WAITING_FOR_EVENT,emitted)
