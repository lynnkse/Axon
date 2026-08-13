# Prompt-embedded actor mechanism

Actors have no runtime, scheduler, timer, poller, or separate LLM call.
Actor rows are global/shared across Axon instances; `actor_id` is an opaque
identity and prompt fetches never filter by deployment instance.

- `AXON_ACTORS=0`: actor blocks are disabled.
- `AXON_ACTORS=1`: on each real user prompt, every non-terminal `actor_state`
  row that fits the current slot cap is embedded in that same prompt and its
  validated output is synchronously revision-checked back into the same row.
- `MAX_ACTOR_SLOTS=4` by default. If eligible actors exceed the cap, lower
  Linux-style `nice` values run first; missing `nice` is neutral (`0`).
- Empty `actor_state` is valid and produces no actor blocks.
- Stored history retains 50 updates; prompt context contains only the newest 8,
  with additional structural/string caps documented in `actor_model/prompt_blocks.py`.

Restart `session_manager.py` after changing `AXON_ACTORS`, because output-format
instructions are added to the system prompt at Claude session startup.
