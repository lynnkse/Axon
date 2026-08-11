#!/usr/bin/env python3
"""Idempotently seed actor_state from legacy affect tables. Dry-run by default."""
from __future__ import annotations

import argparse
import json

import config
import supabase_client
from actor_model.contracts import normalize_state


def rows() -> list[dict]:
    alive, anton = supabase_client.fetch_alive_state() or {}, supabase_client.fetch_anton_model() or {}
    common = {"observations": [], "beliefs": [], "decisions": [], "commitments": [],
              "unresolved_questions": [], "proposed_actions": [], "completed_actions": []}
    axon = normalize_state({**common, "tick": alive.get("tick", 0), "valence": alive.get("valence", 0.),
        "valence_sigma": alive.get("valence_sigma", .2), "arousal": alive.get("arousal", 0.),
        "arousal_sigma": alive.get("arousal_sigma", .2), "mood": alive.get("mood_label", "neutral"),
        "personality_note": alive.get("personality_note"), "curiosity_focus": alive.get("curiosity_focus"),
        "background_affect": alive.get("background_affect", 0.), "tension": alive.get("tension", 0.)})
    anton_state = normalize_state({**common, "valence": anton.get("valence", .2), "valence_sigma": anton.get("valence_sigma", .3),
        "energy": anton.get("energy", 0.), "energy_sigma": anton.get("energy_sigma", .3),
        "baseline_valence": anton.get("baseline_valence", .2), "baseline_energy": anton.get("baseline_energy", 0.),
        "below_baseline_since": anton.get("below_baseline_since"), "last_observation_at": anton.get("last_observation_at"),
        "legacy_last_reflection_at": anton.get("last_reflection_at")})
    result = [
        {"actor_id":f"{config.INSTANCE}:axon","actor_type":"axon","state":axon,"directory_projection":{"summary":"backfilled Axon state"}},
        {"actor_id":f"{config.INSTANCE}:anton","actor_type":"anton","state":anton_state,"directory_projection":{"summary":"backfilled Anton model"}},
        {"actor_id":f"{config.INSTANCE}:reflection","actor_type":"reflection","state":normalize_state({**common,"last_advanced_at":anton.get("last_reflection_at")}),"directory_projection":{"summary":"waiting for idle window"}},
    ]
    for name in ("anplos-improvement","axon-improvement","commitments"):
        result.append({"actor_id":f"{config.INSTANCE}:{name}","actor_type":name,"state":normalize_state(common),"directory_projection":{"summary":"placeholder; dormant"}})
    for row in result:
        row.update(instance=config.INSTANCE, disposition="waiting_for_event" if row["actor_type"] in {"axon","anton","reflection"} else "dormant", dirty=False)
    return result


if __name__ == "__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--apply",action="store_true"); args=parser.parse_args()
    seed=rows(); print(json.dumps(seed,indent=2))
    if args.apply:
        for row in seed:
            if not supabase_client.upsert_actor_seed(row): raise SystemExit(f"failed: {row['actor_id']}")
