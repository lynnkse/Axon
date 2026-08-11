from __future__ import annotations

from copy import deepcopy

from ..contracts import normalize_state
from ..types import ActorRecord, Disposition, Event, TransitionContext, TransitionResult


def kalman_delta(mu: float, sigma: float, delta: float, obs_sigma: float = .12) -> tuple[float, float]:
    k = sigma ** 2 / (sigma ** 2 + obs_sigma ** 2)
    return max(-1., min(1., mu + k * delta)), max(.05, ((1 - k) ** .5) * sigma)


class AxonActor:
    actor_type = "axon"
    def transition(self, record: ActorRecord, events: list[Event], context: TransitionContext) -> TransitionResult:
        s = normalize_state(deepcopy(record.state))
        for event in events:
            p = event.payload
            if event.event_type == "interaction_received":
                s["tick"] = int(s.get("tick", 0)) + 1
                s["valence_sigma"] = min(.4, s.get("valence_sigma", .2) + .01)
                s["arousal_sigma"] = min(.4, s.get("arousal_sigma", .2) + .01)
                s["valence"] = s.get("valence", 0.) + .02 * (.15 - s.get("valence", 0.))
                s["arousal"] = s.get("arousal", 0.) + .02 * (0. - s.get("arousal", 0.))
                s["tension"] = max(0., s.get("tension", 0.) - .01)
                s["background_affect"] = s.get("background_affect", 0.) * .98
            elif event.event_type == "affect_tag_emitted":
                if p.get("valence_delta") is not None:
                    s["valence"], s["valence_sigma"] = kalman_delta(s.get("valence", 0), s.get("valence_sigma", .2), max(-.15, min(.15, p["valence_delta"])))
                if p.get("arousal_delta") is not None:
                    s["arousal"], s["arousal_sigma"] = kalman_delta(s.get("arousal", 0), s.get("arousal_sigma", .2), max(-.15, min(.15, p["arousal_delta"])))
                if p.get("mood"): s["mood"] = p["mood"].strip().lower()
                if p.get("curiosity_focus"): s["curiosity_focus"] = p["curiosity_focus"].strip()
                if p.get("tension_delta") is not None:
                    s["tension"] = max(0., min(1., s.get("tension", 0) + max(-.2, min(.2, p["tension_delta"]))))
            elif event.event_type == "goal_completed":
                count = max(1, int(p.get("count", 1)))
                s["valence"], s["valence_sigma"] = kalman_delta(s.get("valence", 0), s.get("valence_sigma", .2), min(.15, .06 * count), .15)
            elif event.event_type == "high_confidence_valence_report":
                s["valence"], s["valence_sigma"] = kalman_delta(s.get("valence", 0), s.get("valence_sigma", .2), .3 * float(p["valence"]), .18)
            s["observations"] = (s["observations"] + [{"event_id": event.id, "sequence": event.sequence, "type": event.event_type}])[-100:]
        summary = f"V={s.get('valence',0):+.2f} A={s.get('arousal',0):+.2f} T={s.get('tension',0):.2f} mood={s.get('mood','neutral')}"
        return TransitionResult(s, {"summary": summary}, Disposition.WAITING_FOR_EVENT)
