# Axon instance deployment contract

Status: Step 2 design contract, 2026-09-07. This document defines how one
containerized Axon instance is assembled and operated. It does not implement
images, Compose files, brokers, or container cutover.

It builds on `CONTAINER_THREAT_MODEL_AND_RUNTIME_INVENTORY.md`. The permission
socket mismatch, cross-instance tick transport, and credential-broker ownership
remain unresolved and are carried as explicit cutover blockers in section 14.

## 1. Deployment unit

One logical instance is an independently named deployment containing:

- One generic Axon engine container running under `runsc`.
- One private bridge network used only by that instance.
- One separately isolated read-fetch proxy container when browsing is enabled.
- Credential-broker endpoints for its approved operational services.
- One userspace Tailscale sidecar and state volume when remote UI is enabled.
- Instance-exclusive state, logs, workspace, plugin, profile, and configuration.

The engine container contains all accepted local Axon processes. The cloud LLM
is external. No engine container shares writable volumes, runtime sockets,
network namespaces, container names, or ports with another instance.

Canonical deployment identity is a lowercase DNS-label-like `AXON_INSTANCE_ID`
matching `^[a-z0-9][a-z0-9-]{0,62}$`. It is used in container, volume, network,
broker-policy, log, backup, and Tailscale names. Hostname fallback is forbidden
in the container contract: identity must be explicit.

## 2. Image contract and layering

There is one shared generic engine image, supporting both engine choices:

```text
pinned OS base
  -> Python runtime and pinned Python dependencies
  -> Claude and Codex CLIs at pinned versions
  -> ffmpeg and supervisord
  -> Axon source at a recorded commit
  -> generic entrypoint, supervisor configuration, health probe
```

No instance plugin, profile, policy, workspace, `.env`, credential, Tailscale
state, or runtime data is baked into this image.

Required image identity:

```text
axon-engine:<monotonic-version>       e.g. axon-engine:v3
axon-engine@sha256:<immutable-digest>
```

Deployments pin the digest, not a mutable tag. The tag is an operator-facing
alias. OCI labels record image version, Axon Git commit, build timestamp,
Python version, Claude CLI version, and Codex CLI version. Rebuilding the same
tag with different contents is prohibited.

The root filesystem is read-only. `/opt/axon` is image-owned and not a bind
mount, preventing a running model from modifying framework source or racing
Anton’s host checkout.

## 3. Instance material injection

Instance files are injected by the trusted orchestrator as separate read-only
mounts:

| Container path | Required | Source/content | Mode |
|---|---:|---|---|
| `/etc/axon-instance/config.env` | Yes | Validated non-secret runtime config | file, `0440` |
| `/etc/axon-instance/profile.md` | Yes | Instance profile | file, `0440` |
| `/etc/axon-instance/plugin/` | Engine-dependent | Plugin entry point and sibling modules | directory, read-only |
| `/etc/axon-instance/sandbox-policy.json` | If browsing enabled | Read-fetch policy | file, `0440` |
| `/workspace` | Yes | Instance working tree/data | dedicated instance mount, policy below |

`AXON_EXTENSIONS_PATH` resolves inside the container to
`/etc/axon-instance/plugin/axon_ext.py`. Claude-engine deployments may omit it
only while the legacy fallback still exists; the final generic deployment
contract requires it for every customized instance. Codex already fails when
it is absent.

The plugin directory is mounted as a unit because `instance_plugin.py` uses a
private package namespace for sibling imports. It must not be copied into
`/opt/axon`, added globally to `PYTHONPATH`, or made writable.

The profile and plugin are code/configuration inputs: changing either requires
an explicit deployment restart and a new instance configuration revision in
the audit record. No live mutation or silent reload is allowed.

### Workspace policy

`/workspace` is the only general-purpose location where model-driven file
mutation may be permitted. It is never the host home or Axon checkout.

- Default mode is read-write for instances intended to work on their own files.
- Read-only instances mount it read-only explicitly.
- Host device files, sockets, Git/SSH credentials, and nested mounts are absent.
- The mount is exclusive to one instance.
- A storage quota starts at 10 GiB and is configurable downward or upward only
  through reviewed deployment config.
- `PROJECT_DIR=/workspace` is mandatory.

## 4. Container user and filesystem permissions

The image declares a fixed unprivileged user and group:

```text
uid=10001 (axon)
gid=10001 (axon)
umask=0077
```

Host volume preparation sets ownership to this UID/GID without granting access
to unrelated host users. The engine never runs as root and receives:

- all Linux capabilities dropped;
- `no-new-privileges`;
- no privileged devices;
- no Docker/containerd socket;
- no host PID, IPC, user, or network namespace;
- a read-only root filesystem;
- seccomp/default runtime restrictions in addition to gVisor.

Writable paths are limited to:

```text
/workspace                 instance workspace volume
/var/lib/axon              persistent runtime state
/var/log/axon              bounded persistent logs
/run/axon                  tmpfs for sockets/PIDs
/tmp                       bounded tmpfs for temporary files
```

`/run/axon` is created `0700`; all Unix sockets are owner-only. `/tmp` is
mounted with `nodev,nosuid,noexec` unless CLI compatibility testing proves
that a specific tool needs executable temporary files. Any exception must be
path-specific rather than relaxing all of `/tmp`.

## 5. Stable environment and paths

The container has a stable non-host home:

```text
HOME=/var/lib/axon/home
SOCKET_DIR=/run/axon
RELAY_DIR=/var/lib/axon/relay
PROJECT_DIR=/workspace
PROFILE_PATH=/etc/axon-instance/profile.md
AXON_EXTENSIONS_PATH=/etc/axon-instance/plugin/axon_ext.py  # when required
```

Engine-specific local state therefore resolves to:

```text
/var/lib/axon/home/.claude/projects/.../*.jsonl
/var/lib/axon/home/.codex/sessions/**/*.jsonl
/var/lib/axon/relay/session_id
/var/lib/axon/relay/codex_thread_id
/var/lib/axon/relay/*.lock
/var/lib/axon/relay/uploads/
/var/lib/axon/relay/books/
```

The minimal Claude/Codex configuration needed for hooks and broker endpoints
must be generated separately from host configuration and mounted read-only or
created by the trusted entrypoint. It contains no raw authentication state.
Host `~/.claude` and `~/.codex` directories are never mounted.

## 6. Persistent and ephemeral volumes

Every volume name includes the instance ID:

| Volume | Container path | Persistence | Backup |
|---|---|---|---|
| `<id>-workspace` | `/workspace` | Instance-defined | Separate workspace policy |
| `<id>-state` | `/var/lib/axon` | Required across upgrades | Periodic copy and pre-upgrade snapshot |
| `<id>-logs` | `/var/log/axon` | Operational retention | Periodic copy/rotation |
| `<id>-run` | `/run/axon` | tmpfs; recreated | Never |
| `<id>-tmp` | `/tmp` | tmpfs; recreated | Never |
| `<id>-tailscale` | Sidecar-only state path | Required | Periodic copy |

Although Supabase holds durable conversational state, Claude/Codex JSONL
rollouts can exceed tens of MiB and are required for seamless resume. They are
part of `<id>-state`; calling that volume “small” is not a safe capacity
assumption. Apply a 5 GiB starting quota, alert at 70%, and prune only through
an explicit session-retention procedure. Never delete the active rollout.

`<id>-logs` starts with a 2 GiB quota. Application log targets rotate at
20 MiB with five retained files per process. Container stdout/stderr collection
has a separate host-side rotation policy. Uploads share the state quota and
must also have per-file and aggregate limits before production cutover.

## 7. Supervisord process contract

PID 1 is an init-compatible entrypoint that validates mounts/configuration and
then `exec`s `supervisord` in the foreground. Supervisord has no TCP control
socket. A Unix control socket, if retained, is under `/run/axon`, mode `0600`.

Accepted process set:

| Program | Condition | Priority/start dependency |
|---|---|---|
| `manager-claude` or `manager-codex` | Exactly one selected by `AXON_ENGINE` | First |
| `telegram` | Enabled for the instance | Starts after manager socket wait |
| `cli` | Enabled for operator console | Starts after manager socket wait |
| `curator` | Enabled by accepted baseline | Starts after broker readiness |
| `usage-monitor` | Enabled by accepted baseline | Starts after broker readiness |
| `dashboard` | Enabled for remote UI | Starts after broker readiness |
| `web-cli-bridge` | Enabled with dashboard CLI | Starts after manager socket wait |

`proactive_node.py`, Ralph, ingestion services, auxiliary MCP services, ttyd,
AutoCAD, GLM, Manim, and development utilities are absent from supervisor
configuration by default. Telegram may invoke approved ingestion children only
after those capabilities receive a separate manifest entry; current default is
disabled.

All long-running programs use `autorestart=unexpected`, finite retry counts,
`stopasgroup=true`, and `killasgroup=true`. Manager shutdown receives SIGTERM
and 30 seconds before SIGKILL; other services receive 15 seconds. Crash-loop
backoff and an unhealthy deployment state are required—supervisor must not
restart a fatally misconfigured plugin forever without surfacing it.

Supervisord priority alone does not establish readiness. Small wait wrappers
may wait for required sockets/brokers with a finite deadline and then exit
nonzero. They must not hide permanent failure in infinite retry loops.

### CLI PTY requirement

Current `cli_node.py` assumes a real terminal and host tmux supplies it. Plain
supervisord does not allocate a usable operator PTY. The accepted `cli` process
therefore requires either a narrow PTY wrapper plus a documented attach path,
or conversion to an on-demand `docker exec -it ... cli_node.py` client while
the Web CLI bridge remains supervised. This is a compatibility validation
item for the offline image; it does not authorize dropping CLI capability.

## 8. Entrypoint validation

Before any service starts, entrypoint validation must fail closed on:

- missing/invalid `AXON_INSTANCE_ID` or `AXON_ENGINE`;
- an unrecognized image/config schema version;
- a missing profile, required plugin, workspace, state, or log mount;
- writable plugin/profile/policy inputs;
- owner/mode mismatch on state, logs, run directory, or secrets-free config;
- any raw credential variable present in the engine environment;
- missing selected CLI, Python package, ffmpeg when voice is enabled, or
  supervisor configuration;
- browser enabled without a valid policy and private proxy endpoint;
- remote UI enabled without a Tailscale ingress contract;
- broker endpoint absent for an enabled operational service;
- direct default egress or unexpected DNS path, once network enforcement is
  implemented;
- both managers enabled or neither manager enabled.

The entrypoint logs only non-secret resolved configuration, image digest,
Axon commit, instance ID, enabled capability list, and hashes of mounted plugin,
profile, and policy. It never prints full environment contents.

## 9. Resource limits

Initial per-engine-container limits, pending staging measurements:

```text
CPU limit:                 4 cores
CPU reservation:           0.5 core
memory limit:              6 GiB
memory reservation:        1 GiB
swap:                      disabled (memory+swap limit = memory limit)
PIDs:                      512
open files:                4096 soft / 8192 hard
/run/axon tmpfs:           32 MiB
/tmp tmpfs:                512 MiB
workspace quota:           10 GiB
state quota:               5 GiB
log quota:                 2 GiB
```

These are enforceable starting ceilings, not estimates of expected use. An
instance may receive reviewed overrides, recorded in deployment metadata. It
may not silently inherit unlimited host defaults. OOM, PID exhaustion, disk
quota, and restart behavior must be exercised in staging.

The future read proxy, brokers, and Tailscale sidecar get separate limits so a
fault in one does not consume the engine allocation.

## 10. Socket contract

All same-instance Axon sockets remain Unix-domain sockets under `/run/axon`:

```text
/run/axon/user_input.sock
/run/axon/cli_input.sock
/run/axon/display.sock
/run/axon/claude_response.sock
/run/axon/permission.sock
```

They are never bind-mounted to the host or another instance. The directory is
owner-only. Producers use finite message-size limits, connect/read timeouts,
and validate NDJSON fields. Raw CLI input and display/response streams remain
high-sensitivity channels.

The permission hook's current `/tmp/cognitive-hq/permission.sock` default does
not match this contract. Its exact owner/fix is unresolved; cutover is blocked
until the manager, hook, and tests use one configured path.

The current cross-instance tick cannot use another deployment's Unix socket.
Its authenticated transport and owner remain unresolved. No shared `/run`
mount or cross-instance bridge membership may be introduced as a shortcut.

## 11. Logs and audit records

Application processes write stdout/stderr under supervisor and may write
structured files under `/var/log/axon`. Remove the host launcher’s `tee`,
tmux, `tail -f`, and broad `pkill` patterns from the container lifecycle.

Every record includes timestamp, instance ID, image digest/version, service,
and severity where supported. Redact bearer values, Telegram token URL
segments, authorization headers, cookies, signed URLs, and sensitive query
parameters. Do not log complete environments or broker credentials.

Separate trusted host/proxy/broker audit logs are not writable by the engine.
The engine may read neither other instances’ logs nor broker audit records.
Clock comes from the host kernel; timezone formatting uses the configured
instance timezone while audit timestamps remain UTC.

Health and lifecycle events record startup validation, supervisor state,
readiness transitions, image/config hashes, OOM/resource failures, graceful
shutdown, upgrade, and rollback.

## 12. Health signals

Health is split into three states:

### Liveness

The engine container is live when supervisord is responsive and the selected
manager plus every required enabled program is in `RUNNING`. The selected
Claude/Codex child process must also exist. Repeated supervisor crash loops are
unhealthy rather than perpetually “starting.”

### Readiness

The instance is ready only when:

- entrypoint validation succeeded;
- exactly one manager owns its lock;
- all five required Unix sockets exist with correct owner/mode;
- local probes can connect to manager input/response/display sockets without
  causing a model turn;
- enabled brokers and the read proxy report policy-matched readiness;
- Telegram polling has initialized when Telegram is enabled;
- dashboard `/ _stcore / health` equivalent and WebSocket TCP listener are up
  when UI is enabled;
- no required service is in backoff/fatal state.

The eventual literal Streamlit path must be tested and recorded without the
spaces shown descriptively above (`/_stcore/health` in current Streamlit).

### Functional/degraded status

Provider/Supabase/Groq outages produce explicit degraded signals and normal
request failures; they do not open direct egress. Groq failure degrades voice
only. Dashboard failure does not declare the manager dead, but the deployment
is not fully ready when dashboard is configured as required.

Health probes never send a billable LLM turn, Telegram message, database
mutation, or public fetch. A separate operator-triggered synthetic check may
test those integrations in staging.

## 13. Upgrade, backup, and rollback

### Routine backup

- Periodically copy `<id>-state`, `<id>-logs`, and sidecar state to an
  instance-namespaced backup location.
- Use a volume snapshot or briefly quiesce the manager for consistent active
  JSONL/pointer capture; a naive copy may race writes.
- Workspace backup follows its own instance policy.
- Retain at least the latest pre-upgrade snapshot and a small time-based set.

### Upgrade procedure

1. Resolve the new image tag to a digest and verify its provenance.
2. Run entrypoint/config/plugin validation without starting live services.
3. Exercise the image in staging with disposable volumes and broker identities.
4. Mark the live instance unavailable and stop its old deployment gracefully.
5. Confirm the old manager/LLM child exited and no socket owner remains.
6. Snapshot state/log/Tailscale volumes and record the old image digest.
7. Start the new digest against the same instance volumes and immutable config.
8. Wait up to a defined deployment timeout (initially 120 seconds) for
   readiness; run a separately approved functional smoke test.
9. If ready, record successful cutover and retain the prior image/snapshot.
10. If not ready, stop the new deployment before rollback.

Schema/state changes made by an image must be backward-compatible for at least
one prior image version. A migration that prevents prior-image startup needs a
separate backup/restore plan and is not eligible for automatic rollback.

### Rollback procedure

1. Ensure the failed/new deployment is fully stopped.
2. Restore the pre-upgrade volume snapshot only if the new version made an
   incompatible or corrupting local change; otherwise retain current state.
3. Start the recorded previous image digest with the previous config revision.
4. Wait for readiness and run the approved non-mutating checks.
5. Record reason, image/config transition, state restore decision, and result.

Never run two versions against one state volume, Supabase identity, Telegram
bot, or Tailscale identity concurrently.

## 14. Carried open items and cutover blockers

The following are deliberately not solved by this Step 2 contract:

1. **Permission socket mismatch**: `permission_hook.py` and manager defaults
   disagree. An owner and tested fix are required.
2. **Cross-instance tick transport**: isolated deployments cannot open each
   other's Unix sockets. An authenticated relay/transport and owner are
   required; shared socket mounts are forbidden.
3. **Credential brokers**: architectural placement is settled, but ownership
   and implementations for Claude, Codex, Supabase, Telegram, and Groq remain
   unassigned.
4. **CLI PTY attachment**: supervisord cannot reproduce tmux's interactive PTY
   implicitly; the retained CLI capability needs a tested attachment design.
5. **Provider compatibility facts**: exact broker base-URL support and minimal
   credential-free CLI configuration require empirical validation.

These items do not block continued design work. Items 1–3 block real cutover;
items 4–5 block declaring the relevant enabled deployment fully compatible.

## 15. Step 2 acceptance checklist

- [ ] Shared image layering and immutable version identity accepted.
- [ ] Instance injection paths and read-only modes accepted.
- [ ] UID/GID and writable-path contract accepted.
- [ ] Persistent/tmpfs volume mapping and quotas accepted.
- [ ] Supervisord process set, conditions, and shutdown behavior accepted.
- [ ] Baseline resource ceilings accepted or adjusted.
- [ ] Socket paths and isolation rules accepted.
- [ ] Log retention/redaction contract accepted.
- [ ] Liveness/readiness/degraded semantics accepted.
- [ ] Upgrade, backup, rollback, and compatibility requirements accepted.
- [ ] Carried unresolved items acknowledged without assuming resolution.
