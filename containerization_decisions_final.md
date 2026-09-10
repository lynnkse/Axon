# Remaining decisions -- settled, proceed to step 1

Anton's answers to the open decisions from your last analysis:

1. **Exact operational egress needed per engine/frontend**: settled, don't
   leave this open. The required outbound destinations are: the Claude/Codex
   API, Supabase (database), the Telegram Bot API, and Groq (voice
   transcription). Nothing else should be reachable by default.

2. **Separate machine identity — real LAN IP vs. bridge + Tailscale
   identity**: bridge network + a userspace Tailscale sidecar is sufficient.
   No real LAN IP is required.

3. **In-container supervisor**: use `supervisord`. Not worth deliberating
   further -- it's the standard, well-documented choice for this job and
   there's no strong reason to pick anything more exotic.

4. **How instance images get built/versioned**: keep one shared/generic
   engine image, with each instance's plugin/config injected at container
   start (same pattern as the existing `AXON_EXTENSIONS_PATH` mechanism) --
   do NOT bake a separate image per instance. Tag images with version
   numbers (e.g. `axon-engine:v3`) so a specific running version is always
   identifiable and a previous tag can be restarted if a new one breaks.

5. **LLM/Supabase credentials — stay in engine or move behind a broker**:
   move behind a broker. The engine should be able to *call* the LLM API and
   Supabase, but should never hold or see the raw credentials directly.

6. **Backup/upgrade/rollback for persistent volumes**: low-stakes, since
   almost all real state (conversation history, memory, insights) already
   lives in Supabase, not on local disk -- the actual persistent volume is
   just small local files (session/thread IDs, logs). Backup via a simple
   periodic copy is sufficient. For rollback, just keep the previous image
   tag available and restart it against the same volume if a new version
   breaks. No need to design anything more elaborate than that.

## What to do now

All the open decisions from your last analysis are now settled (see above).
Proceed to **step 1** of your revised 14-step sequence: freeze the threat
model and runtime inventory (enumerate every required filesystem path,
subprocess, secret, inbound interface, and outbound service for both the
Claude and Codex engines). This remains analysis/documentation output only,
still working inside `/tmp/axon_containerization_work` -- do not modify
`/home/anton/Axon` and do not begin real container/image implementation
yet without a further explicit go-ahead.
