# Follow-up: answers to your open questions + a real scope change

## IMPORTANT: working directory changed

Do NOT touch `/home/anton/Axon` anymore — Anton is actively working in that
real checkout right now. A full copy has been made at
`/tmp/axon_containerization_work` (identical copy, same git history). Do all
further reading/design/prototyping against that copy. When the design is
validated and Anton approves, the changes will be copied back into the real
`/home/anton/Axon` manually — not automatically, not by you.

## Reaffirmed: this is generic Axon framework, not Ailin-specific

Same as stated in the original brief — Ailin is just the first consumer.
Build it as a capability of the shared engine (same pattern as the
instance-plugin / Codex-engine-swap work), not bespoke to one instance.

## Answers to your open questions from the last analysis

1. **Does the model process itself run inside gVisor, or does the host
   process just dispatch disposable sandbox jobs?**
   Anton's answer: the actual LLM inference is obviously cloud-side (Claude
   or GPT API) and stays out of the container -- that's not something we can
   or need to contain. But the entire Axon *instance* -- session manager,
   telegram/cli nodes, actor framework, everything that isn't the LLM API
   call itself -- should run *inside* the container. This is a real scope
   change from the original "just add a sandboxed fetch tool the model can
   call" framing: the whole engine process should be containerized, not just
   a disposable job dispatched from an unsandboxed host process. This
   directly addresses the biggest caveat from your last analysis (that a
   fetch-tool-only sandbox doesn't achieve real containment while the host
   engine itself runs with --dangerously-skip-permissions /
   --dangerously-bypass-approvals-and-sandbox) -- now the engine itself would
   be inside the boundary, not just the fetch path.
   The container should be self-contained except for two egress points:
   (a) outbound connection to the cloud LLM API, (b) the browsing/fetch
   proxy path.

2. **Proxy: host-side or separate least-privileged container?**
   Anton leans toward a separate least-privileged container (not host-side),
   but wants to actually discuss the tradeoffs rather than just picking one.
   Please give a concrete recommendation with reasoning (attack surface,
   latency, operational complexity of each option) so this can be a real
   decision, not a default.

3. **Is PDF support needed in the first release?**
   Anton wasn't sure what this question meant when read back to him --
   please clarify explicitly next time (this was about whether the fetch
   proxy's allowed content-type list should include `application/pdf` for
   pages that link to PDFs). His answer once clarified: no strong opinion,
   not a blocker either way -- skip it for v1 unless it's trivial, don't
   spend design effort on it now.

4. **What exact isolated network topology guarantees no cross-instance
   proxy access?**
   Anton's answer, fairly specific: each containerized Axon instance should
   appear as its own distinct machine on the network -- its own IP (e.g. via
   a bridge/macvlan network, possibly its own Tailscale node), its own
   network interface and filesystem -- but sharing the host kernel. He was
   explicit that a full VM is NOT wanted (kernel-level container isolation,
   e.g. gVisor, is sufficient) -- the goal is "looks and behaves like a
   separate computer" without the overhead of a full virtual machine.

## What to do now

Given point 1 is a real scope change (whole-engine containerization, not
just a sandboxed tool), please:
1. Re-assess whether your original 9-step recommended sequence still holds,
   or whether it needs reordering/expansion given the engine itself is now
   inside the boundary.
2. Give a concrete recommendation for point 2 (proxy placement) with
   reasoning, not just an open question.
3. Continue treating this as analysis/design output for now (working only
   inside /tmp/axon_containerization_work) -- Anton will review before any
   real implementation begins against the real Axon checkout.
