from __future__ import annotations

import re
from uuid import uuid4

from .types import Assignment, Event


def response_events(text: str, instance: str, source: str, interaction_key: str) -> list[Event]:
    events: list[Event] = []
    def make(kind, payload, actors):
        events.append(Event(kind,instance,"model_output",payload,{"source":source,"interaction_key":interaction_key},
            tuple(Assignment(f"{instance}:{a}",confidence=1.,reason="typed response output") for a in actors),
            f"{interaction_key}:{kind}:{len(events)}"))
    affect={}
    for tag,key in (("VALENCE","valence_delta"),("AROUSAL","arousal_delta"),("TENSION","tension_delta")):
        m=re.search(rf'\[{tag}:\s*([+-]?\d*\.?\d+)\]',text,re.I)
        if m: affect[key]=float(m.group(1))
    for tag,key in (("MOOD","mood"),("CURIOSITY","curiosity_focus")):
        m=re.search(rf'\[{tag}:\s*([^\]]+)\]',text,re.I)
        if m: affect[key]=m.group(1).strip()
    if affect: make("affect_tag_emitted",affect,["axon"])
    done=len(re.findall(r'\[DONE:',text,re.I))
    if done: make("goal_completed",{"count":done},["axon","commitments"])
    m=re.search(r'\[ANTON_STATE:\s*([^\]]+)\]',text,re.I)
    if m:
        body=m.group(1); payload={}
        for key in ("valence","energy"):
            value=re.search(rf'{key}=([+-]?\d*\.?\d+)',body,re.I)
            if value: payload[key]=max(-1.,min(1.,float(value.group(1))))
        for key,pattern in (("mode",r'mode=([\w-]+)'),("evidence",r'evidence="([^"]*)"')):
            value=re.search(pattern,body,re.I)
            if value: payload[key]=value.group(1)
        explicit=re.search(r'explicit=(true|false)',body,re.I)
        payload["explicit"]=bool(explicit and explicit.group(1).lower()=="true")
        make("anton_state_observed",payload,["anton"])
    for match in re.findall(r'\[(?:DREAM|INSIGHT):\s*(.+?)(?:\|[^\]]*)?\]',text,re.I|re.S):
        make("insight_emitted",{"content":match.strip()},["reflection","anton"])
    if source == "reflection":
        make("reflection_completed", {"output_event_count": len(events)}, ["reflection"])
    return events


def interaction_event(text: str, instance: str, source: str, user_id: str, key: str | None=None) -> Event:
    key=key or str(uuid4())
    return Event("interaction_received",instance,"interaction",{"text":text,"source":source,"user_id":user_id},
                 {"channel":source},(Assignment(f"{instance}:axon"),),key)
