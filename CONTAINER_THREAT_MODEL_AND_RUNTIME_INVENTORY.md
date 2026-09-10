# Axon container threat model and runtime inventory

Status: Step 1 design baseline, 2026-09-07. This document inventories the
current copied tree and freezes the boundary assumptions that later container
work must satisfy. It does not implement containers or brokers.

## 1. Deployment boundary and security objective

One Axon instance is one isolated deployment with its own engine processes,
instance plugin/configuration, filesystem, private bridge network, userspace
Tailscale identity, persistent state, service brokers, and read-fetch proxy.
The shared engine image is version-tagged; instance material is injected at
startup and is never baked into a separate image.

The cloud LLM inference service is outside the container boundary. Everything
local that drives an instance is inside the boundary: session manager,
Telegram and CLI nodes, actor code, plugin, dashboard, maintenance processes,
and any model-launched subprocesses.

The primary objective is containment of a compromised engine/plugin/model
tool execution. It must not expose host files, raw service credentials, other
instances, unrestricted network egress, the Docker daemon, or host process
control. It may affect its own explicitly writable workspace and local state.

### Trust zones

1. **Trusted host/orchestrator**: container lifecycle, image selection,
   policy injection, volume ownership, backup/rollback, broker credentials.
2. **Credential brokers**: trusted, narrow services holding provider secrets.
   They are higher-value than an engine and must not expose general proxying.
3. **Engine container**: treat as potentially compromised. The instance
   plugin has the same privilege as the engine and is not a policy authority.
4. **Read-fetch proxy**: separately isolated and credential-free. Treat fetched
   content and its parsers as hostile.
5. **Tailscale sidecar**: separately privileged network-identity component;
   its state/auth material is not visible to the engine.
6. **External services/content**: untrusted except for authenticated protocol
   behavior. LLM output is not trusted as authorization.

### Threat actors and protected assets

Threats include prompt-injected web content, malicious Telegram input or
media, unsafe model-generated commands, vulnerable dependencies, a malicious
or buggy instance plugin, a compromised sibling instance, hostile fetched
servers/DNS, and accidental operator misconfiguration.

Protected assets are the host, other instances, raw credentials, Supabase
data/integrity, Telegram identity, LLM accounts and quota, private user files,
session transcripts, audit integrity, and availability of the live instance.

### Explicit non-goals for the first read-only release

- Containing cloud-side inference itself.
- Write/posting integrations such as Instagram.
- Treating a safety-monitor actor as an authorization boundary.
- Granting arbitrary outbound network access for ad-hoc utilities.
- Supporting PDFs through the browsing proxy in v1.

## 2. Current supervised process inventory

`start_axon.sh` currently supervises via host tmux. The container replacement
will use `supervisord`; it must not invoke the host launcher internally.

| Process | Executable/current launch | Required for | Child processes |
|---|---|---|---|
| Manager, Claude variant | `python -u session_manager.py` | Core engine | One persistent `claude --dangerously-skip-permissions` PTY process |
| Manager, Codex variant | `python -u session_manager_codex.py` | Alternative core engine | One persistent `codex [resume ID|-C PROJECT_DIR] --no-alt-screen --dangerously-bypass-approvals-and-sandbox` PTY process |
| Telegram node | `python -u telegram_node.py` | Telegram frontend | `ffmpeg` fallback for voice; `kb_ingest.py` and `study_ingest.py` for requested ingestion |
| CLI node | `python -u cli_node.py` | Local interactive PTY frontend | None |
| Curator | `python -u curator.py` | Periodic rules/insights maintenance | None |
| Codex usage monitor | `python -u codex_usage_monitor.py` | Hourly usage actor and alert | None |
| Dashboard | `streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0` | Web status/UI | Calls `tailscale ip -4` for display |
| Web CLI bridge | `python -u web_cli_bridge.py` | Browser-to-engine PTY access | None |

The container supervisor needs separate programs for the selected manager,
Telegram, CLI if enabled, curator, usage monitor, Streamlit, and WebSocket
bridge. Startup order should make the manager sockets ready before dependent
frontends. Restart policy must avoid two simultaneous managers sharing state.

### Conditionally invoked children

- `permission_hook.py` is invoked by Claude Code hook configuration, not by
  `start_axon.sh`. It reads JSON on stdin and contacts `permission.sock`.
- `kb_ingest.py` may invoke `claude --print` and reads uploaded PDF/PPTX/image
  files or a supplied URL.
- `study_ingest.py` invokes the Claude executable and reads PDFs; it contains
  host-specific Claude installation paths that cannot survive unchanged.
- Telegram voice conversion invokes `ffmpeg` with temporary input/output files.

These children execute with the engine container's privilege and network
policy. A bypass flag on the model CLI does not bypass gVisor, filesystem
mounts, or network enforcement outside the container.

### Present but not in the normal supervised stack

The following files must not be included as enabled services merely because
they exist in the repository:

- `proactive_node.py` and its reminder/dream loop.
- `ralph_node.py` and `ralph.sock` service mode.
- `autocad_bot.py`, `autocad_ingest.py`, and `autocad_search_mcp.py`.
- `study_search_mcp.py`, `kb_ingest.py`, and standalone ingestion commands.
- `glm_agent.py` and its Zhipu API access.
- `start_web.sh`, legacy ttyd paths, and standalone dashboard launcher.
- Manim/rendering and other development utilities.

Each requires an explicit capability decision before it is enabled. Optional
utilities inherit no network destination or host mount by default.

## 3. Filesystem inventory and target mapping

### Required read-only inputs

| Current path/assumption | Contents | Container target |
|---|---|---|
| Axon repository/current directory | Python modules, dashboard, scripts | `/opt/axon`, read-only image content |
| `PROJECT_DIR` | Model working tree and instance-visible files | `/workspace`, instance-specific mount; writable only if that instance needs file mutation |
| `PROFILE_PATH` | Profile/system-prompt material | `/etc/axon-instance/profile.md`, read-only |
| `AXON_EXTENSIONS_PATH` | Instance plugin entry point and sibling modules | `/etc/axon-instance/plugin/axon_ext.py`, entire plugin directory read-only |
| Non-secret instance configuration | Identity, timezone, channel, engine choice, limits | `/etc/axon-instance/config.env` or validated equivalent, read-only |
| CA trust and timezone database | TLS and `ZoneInfo` support | Image-owned, read-only |
| Claude/Codex executable and Python dependencies | Runtime programs | Image-owned, read-only |

Do not mount the host home, SSH directory, Git credentials, Docker socket,
host `/tmp`, arbitrary repositories, or the live host Axon checkout.

### Required writable runtime state

| Logical path | Current use | Proposed storage |
|---|---|---|
| `SOCKET_DIR` (currently `/tmp/axon`) | `user_input.sock`, `cli_input.sock`, `display.sock`, `claude_response.sock`, `permission.sock` | `/run/axon`, instance-private tmpfs |
| `RELAY_DIR` (currently `~/.claude-relay`) | Session/thread pointer, manager locks, sentinel, reminders, uploads | `/var/lib/axon`, small persistent instance volume |
| `SESSION_ID_FILE` | Claude resume ID | `/var/lib/axon/session_id` |
| `CODEX_THREAD_ID_FILE` | Codex resume ID | `/var/lib/axon/codex_thread_id` |
| Manager lock files | Singleton protection | `/var/lib/axon/*.lock`; stale PID handling must be container-aware |
| Telegram uploads/books | Downloaded media and ingestion staging | `/var/lib/axon/uploads`, `/var/lib/axon/books`, quotas required |
| Claude session JSONL | Current code scans `~/.claude/projects/<encoded-project>/*.jsonl` | Dedicated `/var/lib/axon/claude` volume with container HOME configured consistently |
| Codex rollout JSONL | Current code scans `~/.codex/sessions/**/*.jsonl` | Dedicated `/var/lib/axon/codex` volume with container HOME configured consistently |
| Logs | Manager, Telegram, CLI, curator, usage, dashboard | `/var/log/axon`, instance volume with rotation/size limits |
| Temporary conversion files | Voice `.oga`/`.mp3`, bounded scratch | `/tmp`, size-bounded tmpfs |
| Dashboard document inputs | HTML/Markdown files beneath the Axon tree | Already available read-only under `/opt/axon` |
| Dashboard instance design input | Currently `~/ailin/DESIGN.md` | Optional read-only instance document mount; remove hardcoded home path |
| Dashboard media inputs | Currently `~/Axon/manim/output`, `~/Axon/manim/scenes`, and `~/manim_videos` | Disabled by default or mounted as explicit read-only optional media volumes |

The configured container `HOME` must be stable because both manager variants
derive session paths from `Path.home()`. Alternatively, later code can make
those roots explicit; Step 1 records the current dependency rather than
silently changing it.

### Filesystem hazards requiring migration work

- `config.py` automatically reads `.env` beside the Axon source. The final
  image must not include a secret-bearing `.env`; non-secret config and broker
  endpoints must replace it.
- The dashboard independently reads the same `.env` and currently consumes a
  Supabase key directly.
- `telegram_node.py` passes Supabase and Telegram secrets to ingestion child
  environments. That must be removed when brokers are introduced.
- Claude/Codex local config may contain MCP servers, hooks, credentials, or
  writable paths. It cannot be copied wholesale from a host home directory.
- A `media_path` arrives over the user-input protocol. Consumers must resolve
  and confine it to the instance upload directory before opening it.
- Logs and subprocess output may contain prompts, fetched content, URLs, or
  secrets and require redaction plus retention limits.

## 3a. Runtime package and binary inventory

The shared engine image needs Python 3.11+ (the source uses modern built-in
generic annotations) and the packages in `requirements.txt`: Telegram bot,
OpenAI, Groq, HTTPX, dotenv, MCP, Zhipu, PyMuPDF, and Anthropic libraries.
The supervised web stack additionally imports `streamlit`, `plotly`, and
`websockets`, which are not currently declared in `requirements.txt`; image
construction must pin them rather than inherit them accidentally from a host
environment.

Required non-Python programs are the selected `claude` or `codex` CLI,
`ffmpeg` when voice is enabled, `supervisord`, a POSIX shell and minimal
process utilities. The Claude CLI may bring a Node runtime depending on its
packaging. `tailscale` belongs in the sidecar, not the engine image. Docker
and `runsc` belong on the trusted host and must not exist as usable control
tools inside the engine.

Optional binaries such as Manim, ttyd, and ingestion-specific conversion
tools are excluded until their corresponding capability is approved.

## 4. Secret inventory and broker implications

The settled design says the engine never holds raw credentials. Current code
does not meet that requirement; the following are migration blockers.

| Secret | Current consumer/location | Required target |
|---|---|---|
| Claude API key or Claude Code OAuth state | `claude` CLI environment/home | Claude broker injects provider authorization; engine gets only broker identity |
| OpenAI API key or Codex login state | `codex` CLI environment/`~/.codex` | Codex broker injects authorization; no `auth.json` in engine volume |
| `SUPABASE_ANON_KEY` | `config.py`, persistence clients, dashboard, ingestion/MCP scripts | Instance-scoped Supabase broker; engine sends no `apikey`/Bearer secret |
| `SUPABASE_SERVICE_ROLE_KEY` | `config.py`, `supabase_client.py`, dashboard | Broker owns it and enforces endpoint/method/table policy; service role is never exposed downstream |
| Instance-specific Supabase keys | Current legacy plugin paths such as `AILIN_SUPABASE_*` | Separate broker identity/policy for that instance/project |
| `TELEGRAM_BOT_TOKEN` | `telegram_node.py`, usage monitor, Ralph/ingestion utilities | Telegram broker maps instance identity to token and forms authenticated Bot API request |
| `GROQ_API_KEY` | `telegram_node.py`/Groq SDK | Groq broker injects authorization |
| Tailscale auth key and node state | Future sidecar | Sidecar-only secret/state; never mounted into engine |

Non-secrets such as Supabase project URL, user ID, timezone, policy paths,
socket locations, and broker endpoints may be injected as validated config.
User/chat IDs are sensitive metadata and should not be broadly logged even
though they are not authentication secrets.

### Broker compatibility questions that must be proven in Step 2/3

- Whether Claude Code supports a custom base URL/auth-free downstream mode
  compatible with a credential-injecting broker.
- Whether Codex CLI supports the corresponding base URL arrangement without
  requiring local raw login state.
- How `python-telegram-bot` is configured to use a broker when the token is
  normally part of the request URL. A non-secret placeholder plus a broker
  mapping may work but must be tested.
- Whether the Groq SDK supports the chosen broker/base URL path.
- How Supabase REST/Functions calls are rewritten so engine code no longer
  constructs secret headers. The broker must enforce project, tables,
  functions, HTTP methods, and preferably row-level constraints; simply
  injecting a service-role key into arbitrary requests is not sufficient.

Broker authentication must be based on the private instance network and a
non-exportable workload identity, not a reusable bearer credential readable
by the engine. Brokers must reject arbitrary upstream hosts and header
smuggling.

## 5. Inbound interface inventory

### Instance-internal Unix sockets

| Socket | Producer/consumer | Sensitivity |
|---|---|---|
| `user_input.sock` | Telegram/proactive/Ralph clients -> manager | Can induce a model turn and tool execution; authenticate by filesystem boundary and validate NDJSON/size |
| `cli_input.sock` | CLI/WebSocket bridge -> manager | Raw PTY keyboard control; equivalent to interactive code execution |
| `display.sock` | Manager -> CLI/WebSocket viewers | Leaks complete PTY content, prompts, tool output, and possibly secrets |
| `claude_response.sock` | Manager -> Telegram/Ralph subscribers | Conversation data and internal source metadata |
| `permission.sock` | Claude hook <-> manager; Telegram/CLI decisions | Consequential-action approval channel; strict peer/path controls required |

`permission_hook.py` currently defaults to `/tmp/cognitive-hq/permission.sock`,
while `config.py` defaults the manager to `/tmp/axon/permission.sock`. The
existing deployment may rely on external hook configuration or a stale path;
the container contract must select one path and test the actual hook end to
end.

The current Ailin pulse opens another instance's host Unix socket directly.
That cannot survive per-instance network/filesystem isolation. The generic
tick-dispatch work needs an authenticated cross-instance control route or a
trusted host/orchestrator relay; never share all instance socket directories.

### Externally reachable interfaces

| Interface | Current behavior | Container policy |
|---|---|---|
| Telegram | Outbound long polling and outbound replies; no listening port | Through Telegram broker only |
| Streamlit dashboard | TCP 8501 on `0.0.0.0` | Expose only through the instance Tailscale identity/ACL; add authentication or rely on a proven single-user ACL |
| Web CLI bridge | WebSocket TCP 7690 on `0.0.0.0` | Same restricted Tailscale path; this is full interactive control, not a low-risk viewer |
| Legacy ttyd | TCP 7681, writable in some launchers | Disabled in the new deployment unless separately approved; do not expose alongside Web CLI bridge |
| Container admin/supervisor | Not currently networked | No public supervisor socket; host/orchestrator-only lifecycle control |
| Read-fetch proxy | Future private HTTP endpoint | Private instance network only; never host-published |

The WebSocket bridge currently has `max_size=None` and no application-level
authentication. Network ACLs alone may be acceptable for staging, but message
limits and explicit authentication are recommended before production.

## 6. Outbound service inventory

The engine receives no unrestricted default egress. Four application service
classes are approved, plus infrastructure networking owned by sidecars.

### Approved engine destinations

1. **Claude or Codex API**, according to the selected engine. Exact provider
   hosts and CLI ancillary calls must be captured in staging; unknown update,
   telemetry, package, or arbitrary tool destinations are denied.
2. **Supabase**, limited to the configured project and approved REST/RPC/Edge
   Function operations. Current core code uses REST tables and embedding/search
   functions. Supabase storage/realtime are not implicitly authorized.
3. **Telegram Bot API**, including Telegram-hosted file download URLs returned
   for voice, photo, and document messages.
4. **Groq API**, only when voice transcription is enabled.

All four go through dedicated or policy-separated credential brokers/service
gateways. A generic CONNECT tunnel to arbitrary destinations is forbidden.

### Infrastructure-plane egress

The userspace Tailscale sidecar necessarily needs Tailscale coordination,
DERP/STUN, DNS, and peer connectivity. This is not a fifth engine permission:
it belongs to the separately isolated sidecar. The engine reaches only its
local Tailscale ingress/sidecar interface.

Container image pulls and host time synchronization belong to the host, not
the running engine. Runtime DNS should use a controlled resolver and must not
become a bypass around gateway enforcement.

### Denied by default

- Direct public DNS/network access from the engine.
- Package registries, GitHub, arbitrary MCP endpoints, localhost host services,
  private LANs, cloud metadata, and sibling container networks.
- URLs supplied to ingestion scripts unless routed through the read-fetch
  proxy under policy.
- Zhipu, AutoCAD-specific services, and other optional integrations until an
  instance explicitly enables a reviewed capability.

## 7. Engine-specific runtime requirements

### Claude engine

- Persistent `claude` PTY subprocess and terminal ioctls.
- Reads/writes Claude project JSONL under the configured container home.
- Reads a resume pointer from `RELAY_DIR/session_id` and may delete it when a
  session exceeds 20 MiB.
- Requires its profile and appended system prompt at spawn.
- Permission hook executable/configuration must be deliberately installed;
  no host Claude settings directory should be mounted wholesale.
- Provider authentication must work through the Claude credential broker.
- The engine's `--dangerously-skip-permissions` posture makes the outer
  container/network boundary mandatory.

### Codex engine

- Persistent Codex TUI PTY subprocess in the current copied implementation.
- Reads/writes rollouts under `~/.codex/sessions/**/*.jsonl` and discovers the
  thread ID from rollout contents.
- Persists `RELAY_DIR/codex_thread_id` and a separate manager lock.
- Requires a configured instance plugin; no identity-specific fallback.
- Needs a deliberately minimal Codex config. Do not mount the host `.codex`
  tree because it may contain auth, MCP definitions, skills, and unrelated
  session data.
- Provider authentication must work through the Codex credential broker.
- `--dangerously-bypass-approvals-and-sandbox` is acceptable only because the
  process remains inside the external gVisor and egress boundary.

### Shared behavior

- Both managers use `PROJECT_DIR` as the subprocess working directory.
- Both expose the same five Unix-socket interfaces.
- Both load the instance plugin dynamically from an absolute filesystem path.
- Both process prompt-embedded actor state through Supabase.
- Both must receive graceful termination long enough to flush state and remove
  sockets/locks before supervisord escalates.

## 8. Instance identity, persistence, backup, and rollback

- Use one tagged shared image such as `axon-engine:v3`; record the immutable
  image digest in deployment/audit metadata.
- Inject plugin, profile, and non-secret configuration read-only at startup.
- Give every instance unique volume, bridge network, Tailscale state, broker
  identity, ports, and instance ID.
- Periodically copy the small `/var/lib/axon` and `/var/log/axon` volumes.
- Upgrade by stopping one instance, backing up its volume, and starting the
  new image tag against the same volume.
- Roll back by restarting the previous immutable tag against that volume.
- Never run old and new managers concurrently against the same sockets or
  session/thread pointer.

## 9. Frozen security invariants for subsequent steps

1. No raw Claude, Codex, Supabase, Telegram, Groq, or Tailscale credential is
   readable inside the engine container.
2. No unrestricted engine egress; only approved brokers and the per-instance
   read-fetch proxy are reachable.
3. One read-fetch proxy and one private bridge network per instance.
4. No Docker socket, host home, SSH material, or broad host bind mount.
5. Plugin/config/profile are injected read-only; plugin code cannot change
   policy.
6. Mutable state is confined to bounded instance volumes/tmpfs.
7. Dashboard and Web CLI are reachable only through that instance's Tailscale
   identity and ACL; Web CLI is treated as full code execution.
8. Cross-instance traffic is denied except for explicitly authenticated
   control messages such as the future generic tick dispatch.
9. Browsed content is untrusted, provenance-labelled, size-bounded, and never
   interpreted as authorization.
10. Broker/proxy/policy failure is explicit and fail-closed; there is no direct
    fallback route.
11. Write-capable external integrations remain out of scope.
12. Optional repository utilities receive no runtime privilege unless listed
    in an instance's reviewed capability manifest.

## 10. Exit criteria for Step 1

Step 1 is frozen when the following are accepted:

- The normal supervised process set is correct for each intended instance.
- Optional utilities are either excluded or explicitly enabled.
- The five socket interfaces, dashboard/WebSocket ports, and Telegram polling
  behavior are complete.
- The writable volume mapping covers all Claude/Codex session and upload paths.
- The four application egress classes and Tailscale infrastructure egress are
  accepted.
- Each current raw credential consumer has a broker migration owner.
- The permission-hook path mismatch and cross-instance tick route are assigned
  for resolution before container cutover.

Items still requiring empirical validation are provider hostnames/base-URL
support, broker compatibility, actual plugin filesystem contents, and the
minimal Claude/Codex configuration files. These are recorded unknowns, not
permission to default open.
