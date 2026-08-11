"""Pure shadow execution and parity reporting; never selects live authority."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


def actor_is_authoritative(runtime_enabled: bool, shadow_mode: bool) -> bool:
    return runtime_enabled and not shadow_mode


def dual_run_legacy_wins(legacy: Callable[[], T], actor: Callable[[], T],
                         compare: Callable[[T, T], None]) -> T:
    """Execute both paths, report parity, and deliberately return legacy."""
    legacy_result = legacy()
    actor_result = actor()
    compare(legacy_result, actor_result)
    return legacy_result


def compare_numeric(label: str, legacy: dict, actor: dict, fields: tuple[str, ...],
                    tolerance: float = 1e-4) -> list[str]:
    discrepancies = []
    for field in fields:
        left, right = legacy.get(field), actor.get(field)
        if left is None and right is None:
            continue
        if left is None or right is None or abs(float(left) - float(right)) > tolerance:
            discrepancies.append(f"{field}: legacy={left!r} actor={right!r}")
    if discrepancies:
        log.error("ACTOR SHADOW MISMATCH [%s] tolerance=%g: %s", label, tolerance, "; ".join(discrepancies))
    else:
        log.info("Actor shadow parity [%s] OK (%d fields, tolerance=%g)", label, len(fields), tolerance)
    return discrepancies


def typed_base(state: dict) -> dict:
    result = dict(state)
    for key in ("observations", "beliefs", "decisions", "commitments", "unresolved_questions",
                "proposed_actions", "completed_actions"):
        result.setdefault(key, [])
    return result
