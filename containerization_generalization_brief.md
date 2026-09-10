# Task: analyze generalizing Ailin's sandbox into generic Axon framework infrastructure

## Context

There is an existing Phase 0/1 sandboxed-browsing prototype, currently Ailin-specific,
living on ROG at `~/ailin/sandbox/`:

- `Dockerfile.proxy` — builds a container image for `research_proxy.py`.
- `research_proxy.py` — a host-side (or separate, less-privileged container) HTTP
  proxy that is the ONLY thing a sandboxed agent container is allowed to reach for
  internet access. Enforces: HTTPS-only, GET-only, a fixed domain allowlist
  (currently `en.wikipedia.org`, `arxiv.org`, `export.arxiv.org`, `github.com`,
  `raw.githubusercontent.com`), DNS-resolved-here (not trusted from caller),
  blocks private/loopback/link-local/reserved/metadata IPs (SSRF hardening),
  re-validates every redirect hop, strips caller auth/cookie headers, caps
  response size (5MB)/redirect count (3)/timeout (15s), restricts content-type to
  html/text/json/pdf, and logs every request to an audit log.
- `run_sandbox.sh` — a generic wrapper around `docker run --runtime=runsc` (gVisor)
  that logs every launch + exit code to a host-side audit log. Already
  instance-agnostic in practice — just happens to live under the Ailin repo.

This was built as Ailin-specific infrastructure (for her eventual internet
browsing / "Instagram PR" capability), but Anton has decided (2026-09-05) that
containerized sandbox access should be a GENERIC AXON FRAMEWORK capability, not
bespoke to one instance — following the exact same principle as the
already-built instance-plugin architecture (`instance_plugin.py`,
`AXON_EXTENSIONS_PATH`, the 5-hook `InstancePlugin` contract) and the
Codex-engine swap (`session_manager_codex.py`): build the capability once in
the shared engine, every instance (Ailin, Axon-main, and any future instance)
gets it "for free" via config, not by copying/forking code.

Relevant already-built patterns to reuse conceptually:
- `instance_plugin.py` on aevadim-09/ROG's `~/Axon/` — `InstancePlugin` protocol,
  `TurnContext`, fail-fast dynamic loader via `AXON_EXTENSIONS_PATH`.
- `session_manager_codex.py` — proof that "shared engine file + per-instance
  plugin file + env vars" is the working pattern for adding a whole new
  capability axis without instance-specific branches in the engine.

## What's being asked (ANALYSIS ONLY — do not implement yet)

1. **Where should this code live?** Propose a concrete new location under the
   shared `~/Axon` repo (not `~/ailin`) — e.g. `Axon/sandbox/` — and what, if
   anything, from the current `~/ailin/sandbox/` files should move there
   verbatim vs. needs to change to be instance-agnostic.

2. **What's the generic interface?** The current `research_proxy.py` is a
   fixed, hardcoded `ALLOWED_DOMAINS` set. For this to be genuinely
   per-instance-configurable (e.g. Ailin might eventually need Instagram's
   domains, a future instance might need something else entirely), how should
   the allowlist and any other policy knobs be supplied per-instance? Should
   this be a new field on the `InstancePlugin` protocol (e.g.
   `sandbox_config()` returning an allowlist/policy dict), a separate config
   file per instance, or something else? Keep the same "fail-fast, no silent
   default-open" posture the rest of this architecture uses.

3. **Read vs. write access.** The current proxy is GET-only / read-only
   research fetching. Real Instagram posting (or any future write-capable
   integration) is a fundamentally different, higher-risk trust boundary than
   read-only fetch-and-summarize. Do NOT design the write/posting path in
   detail yet — just flag clearly in your analysis that this is a separate,
   later problem requiring its own scoping (human-approval gate, scoped
   revocable credentials held OUTSIDE the sandbox, etc., per Anton's existing
   stated constraints), so it isn't accidentally conflated with the read-only
   generalization work being asked for right now.

4. **How does an instance/agent actually invoke this?** Today nothing calls
   `research_proxy.py` from a live session. Propose how a running instance
   (via its `InstancePlugin` hooks, or a new actor, or a new tool-call
   surfaced to the model) would trigger a sandboxed fetch and get the result
   back into its context. Keep this proposal-level, not implemented.

5. **Safety monitor tie-in.** There's a previously-discussed but unbuilt
   `ailin-safety-monitor` actor idea (scanning an instance's transcript for
   injection patterns as a coarse tripwire once it has real browsing access).
   Note briefly how/whether this generalizes too (e.g. a generic
   `sandbox-safety-monitor` actor type instead of Ailin-specific), but don't
   design it in full — just note the shape.

## Deliverable

A written analysis/plan (like the consolidated review you did for the
instance-plugin architecture on 2026-09-04) covering the 5 points above,
with a concrete proposed file layout and interface sketch. Explicitly call
out any gaps, risks, or open questions rather than picking an answer and
hiding the uncertainty. Do not write implementation code yet, do not modify
`~/ailin/sandbox/` or create new files outside of your own analysis output.
