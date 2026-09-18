import json

from model_router import (
    ASTRA, LUNA, SOL, TERRA, BEGIN_ROUTE, END_ROUTE,
    choose_actor_route, choose_main_route, strip_route_update,
)


def test_main_routes_simple_general_complex_and_explicit_astra():
    assert choose_main_route("Are you here?").model == LUNA
    assert choose_main_route("Please organize these notes for tomorrow.").model == TERRA
    assert choose_main_route("Investigate and implement this architecture.").model == SOL
    assert choose_main_route("Use Astra for autonomous GUI computer use.").model == ASTRA


def test_budget_reserve_downgrades_unless_astra_is_explicit():
    assert choose_main_route("Investigate this bug.", 80).model == TERRA
    assert choose_main_route("Use Astra for computer use.", 80).model == ASTRA


def test_actor_router_uses_luna_for_monitors_and_sol_for_escalation():
    monitors = [{"actor_type": "health-monitor", "state": {}}]
    assert choose_actor_route(monitors).model == LUNA
    escalated = [{"actor_type": "project-driver", "state": {"needs_model_escalation": True}}]
    assert choose_actor_route(escalated).model == SOL


def test_route_marker_is_removed_and_parsed_for_next_turn():
    marker = json.dumps({
        "assessment": "downgrade", "recommended_model": LUNA,
        "reason": "next step is deterministic",
    })
    visible, update = strip_route_update(f"Visible answer\n{BEGIN_ROUTE}\n{marker}\n{END_ROUTE}")
    assert visible == "Visible answer"
    assert update["recommended_model"] == LUNA
