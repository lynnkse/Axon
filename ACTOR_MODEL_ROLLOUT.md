# Actor-model rollout (ROG only)

Do not run these steps on aevadim-09.

1. Apply `supabase/migrations/20260811_actor_model.sql` in Supabase.
2. Preview conversion: `python3 scripts/backfill_actor_state.py`.
3. Seed once reviewed: `python3 scripts/backfill_actor_state.py --apply`.
4. Run parity checks: `python3 tests/run_actor_tests.py`.
5. Set `AXON_ACTORS=1 AXON_ACTOR_SHADOW=1`. Both folds run and actor state commits,
   but legacy state, prompts, directives, and tag side effects remain authoritative.
   Review `ACTOR SHADOW MISMATCH` logs and actor revisions before cutover.
6. Set `AXON_ACTOR_SHADOW=0` only after parity is accepted. This makes
   `actor_state` and the actor composer authoritative.
7. Keep `alive_state` and `anton_model` unchanged for the rollback window.
8. Roll back by setting `AXON_ACTORS=0` and restarting. Do not replay
   `anton_state_log`; its observations are already folded into the seed.

`ralph_node.py` remains untouched. Generic `ready_again`, leases, durable
checkpoints, and scheduler re-entry replace its execution semantics.
