# Step 1 acceptance + proceed to Step 2

Anton reviewed the Step 1 exit criteria:

1. CONFIRMED -- the supervised process set (manager, telegram, cli, curator,
   dashboard, usage-monitor) is correct for a real instance.
2. CONFIRMED -- optional/dormant utilities (proactive_node.py, Ralph,
   ingestion/MCP scripts) stay excluded by default.
3. NOT YET RESOLVED, needs more clarification later -- the two real bugs
   (permission_hook.py vs manager socket-path mismatch; Ailin cross-instance
   tick delivery cannot survive network isolation as currently built) still
   need an owner and fix plan. Do not silently resolve these yourself --
   just carry them forward as open items step 2 must not assume are fixed.
4. NOT YET RESOLVED, needs more clarification later -- credential broker
   ownership (who actually builds the Claude/Codex/Supabase/Telegram/Groq
   brokers) is not yet assigned.

None of the still-open items (3, 4) block continued analysis/design work --
they only block a real container cutover later. Proceed now to **Step 2**
of your revised sequence: define the instance deployment contract --
image layering, plugin/profile installation, persistent volumes, UID/GID,
resource limits, socket locations, logs, health signals, upgrades, and
rollback. Continue working only inside /tmp/axon_containerization_work;
do not touch /home/anton/Axon. This remains design/documentation output,
not implementation.
