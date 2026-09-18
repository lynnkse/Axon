"""Deterministic, budget-aware routing for Axon's persistent Codex lanes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"
MODELS = (LUNA, TERRA, SOL, ASTRA)
RESERVE_START_PERCENT = 75.0

BEGIN_ROUTE = "<<<AXON_ROUTE_UPDATE>>>"
END_ROUTE = "<<<END_AXON_ROUTE_UPDATE>>>"
_ROUTE_RE = re.compile(
    re.escape(BEGIN_ROUTE) + r"\s*(.*?)\s*" + re.escape(END_ROUTE), re.DOTALL
)


@dataclass(frozen=True)
class RouteDecision:
    model: str
    reason: str
    budget_limited: bool = False


_SIMPLE = (
    "are you here", "status", "update me", "what time", "summarize", "format",
    "check health", "check the queue", "pulse due",
)
_COMPLEX = (
    "paper", "research", "theorem", "proof", "experiment", "architecture",
    "design", "investigate", "debug", "implement", "isaac", "robot", "slam",
    "analyze", "analysis", "reviewer", "supabase schema", "migration",
)
_ASTRA = (
    "use astra", "switch to astra", "gpt-6 astra", "autonomous gui",
    "computer use", "mouse and keyboard",
)


def _budget(model: str, used_percent: float | None, explicit_astra: bool = False) -> RouteDecision | None:
    if used_percent is None or used_percent < RESERVE_START_PERCENT or explicit_astra:
        return None
    downgrade = {ASTRA: SOL, SOL: TERRA, TERRA: LUNA, LUNA: LUNA}[model]
    if downgrade == model:
        return None
    return RouteDecision(
        downgrade,
        f"weekly usage is {used_percent:.1f}%; preserving the 25% reserve",
        True,
    )


def choose_main_route(
    text: str,
    used_percent: float | None = None,
    deferred_model: str | None = None,
) -> RouteDecision:
    lowered = text.casefold()
    explicit_astra = any(term in lowered for term in _ASTRA)
    if explicit_astra:
        model, reason = ASTRA, "the user explicitly requested Astra or interactive computer work"
    elif deferred_model in MODELS:
        model, reason = deferred_model, "the previous turn requested this lane for the next turn"
    elif any(term in lowered for term in _COMPLEX):
        model, reason = SOL, "research, implementation, or high-ambiguity reasoning"
    elif len(text) <= 500 and any(term in lowered for term in _SIMPLE):
        model, reason = LUNA, "short deterministic or status-oriented request"
    else:
        model, reason = TERRA, "general agentic work"
    limited = _budget(model, used_percent, explicit_astra)
    return limited or RouteDecision(model, reason)


def choose_actor_route(rows: Iterable[dict], used_percent: float | None = None) -> RouteDecision:
    rows = list(rows)
    if any((row.get("state") or {}).get("needs_model_escalation") for row in rows):
        model, reason = SOL, "an actor explicitly requested stronger reasoning"
    else:
        active = []
        for row in rows:
            state = row.get("state") or {}
            actor_type = str(row.get("actor_type") or "")
            waiting = state.get("current_task_status") == "waiting_for_human"
            monitor = any(word in actor_type for word in ("health", "tick", "monitor", "cluster"))
            if not waiting and not monitor:
                active.append(row)
        if active:
            model, reason = TERRA, "at least one project actor has open analytical work"
        else:
            model, reason = LUNA, "all actors are deterministic monitors or waiting"
    limited = _budget(model, used_percent)
    return limited or RouteDecision(model, reason)


def strip_route_update(text: str) -> tuple[str, dict | None]:
    """Remove a next-turn model recommendation from user-visible output."""
    matches = _ROUTE_RE.findall(text)
    update = None
    if matches:
        try:
            candidate = json.loads(matches[-1])
            if (
                isinstance(candidate, dict)
                and candidate.get("assessment") in {"adequate", "escalate", "downgrade"}
                and candidate.get("recommended_model") in MODELS
            ):
                update = candidate
        except json.JSONDecodeError:
            pass
    return _ROUTE_RE.sub("", text).strip(), update


ROUTE_ASSESSMENT_INSTRUCTION = f"""
At the end of each turn, assess the model fit for the next turn. Keep the current
turn single-pass: never retry or switch models inside it. Append exactly one
internal block:
{BEGIN_ROUTE}
{{"assessment":"adequate|escalate|downgrade","recommended_model":"gpt-5.6-luna|gpt-5.6-terra|gpt-5.6-sol|gpt-6-astra","reason":"short reason"}}
{END_ROUTE}
The runtime strips this block before publishing the reply.
""".strip()
