# Step 4 offline engine image verification

Status: implemented and exercised under gVisor `runsc`, 2026-09-08.

## Artifacts

- `Dockerfile`: shared Python 3.11 engine image, UID/GID 10001, pinned engine
  and web dependencies, ffmpeg, supervisord, read-only-image layout, OCI
  labels, entrypoint, and health check.
- `.dockerignore`: excludes `.env`, logs, generated output, Git data, host ttyd,
  and design-only material from the image context.
- `container/entrypoint.sh`: validates identity/schema/mounts, rejects raw
  credential variables, selects exactly one manager, enables only configured
  programs, and execs supervisord as PID 1.
- `container/supervisord.base.conf` and `container/programs/*.conf`: independent
  manager, Telegram, CLI, curator, usage-monitor, dashboard, and Web CLI
  programs with group-aware stop and bounded logs/retries.
- `container/pty_supervisor.py`: supplies the terminal-only `cli_node.py` with
  a stable PTY under supervisord. It always restarts after manager disconnect.
- `container/healthcheck.py`: verifies every enabled supervisor program, all
  five manager Unix sockets, Streamlit's health endpoint, and TCP 7690.
- `container/run_offline_smoke.sh`: reproducible destructive-container smoke
  harness using disposable containers and named volumes, `--runtime=runsc`,
  `--network=none`, read-only root, dropped capabilities, no-new-privileges,
  tmpfs runtime/scratch, and resource ceilings.
- `container/bin/fake-{claude,codex}` and `container/offline_frontend.py`:
  credential-free smoke fixtures for the two components that intrinsically
  require an external service. They are selected only by
  `AXON_OFFLINE_SMOKE=1`.
- `container/smoke/`: non-secret mounted profile/config/plugin fixtures.

## Actual proof on this host

Docker reported `runsc` registered at `/usr/local/bin/runsc` using gVisor's
systrap platform. Both smoke runs printed:

```text
runtime=runsc network=none readonly=true
network=loopback-only
```

The final Claude run reached `RUNNING` for all seven programs (`manager`,
`telegram`, `cli`, `curator`, `usage-monitor`, `dashboard`, and
`web-cli-bridge`). Its manager recovered from forced SIGKILL with PID `10 ->
49`; health returned to `healthy`. Graceful container stop returned exit 0.
After restart, marker data in `/var/lib/axon`, `/workspace`, and
`/var/log/axon` remained and health returned to `healthy`.

The final Codex run produced the same seven `RUNNING` states. Its manager
recovered from PID `10 -> 48`; graceful stop returned exit 0; all three
persistent volumes retained data; final health was `healthy`.

In both runs all five Axon sockets were connectable. Their modes/ownership
were `0700 10001:10001`; the private supervisor socket was
`0600 10001:10001`. The health check also successfully reached
`127.0.0.1:8501/_stcore/health` and the WebSocket TCP listener on 7690.

Static shell/Python checks passed. The copied tree's existing unit suite
passed 14/14. The image built successfully as `axon-engine:step4`; the last
tested image ID was `sha256:41955ebc624715cb01d5e9edd7f904b719edeec11e137d00b7758f9ae6adb3ef`.

The original repository `requirements.txt` did not resolve: `mcp==1.27.0`
requires PyJWT >=2.10.1 while `zhipuai==2.1.5.20250825` requires PyJWT <2.9.
Those dependencies belong to disabled auxiliary processes, so the image uses
the explicit `container/requirements-engine.txt` process-set manifest rather
than silently weakening either constraint.

## Deliberately unverified in Step 4

- Real Claude and Codex CLI packaging, startup, session resume, and provider
  protocol. The offline runs use PTY-compatible fixtures; real CLI versions
  and broker compatibility remain later-step work.
- Real Telegram initialization/polling and broker behavior. The offline
  frontend proves supervisor and Unix-socket attachment/reconnection only.
- Real Supabase, Telegram, Groq, LLM, browsing-proxy, or Tailscale traffic.
  No engine network interface or operational credential was present.
- Broker networks, Step 3 firewall rules, per-service health, and degraded
  upstream behavior.
- A model turn and rollout/session-ID persistence. No cloud inference was
  allowed, so persistence proof is filesystem/volume-level.
- Host quota enforcement, OOM/PID exhaustion, log rotation at its thresholds,
  backup/restore, multi-instance isolation, and production Compose/system
  orchestration.
- Production readiness. The runtime image intentionally contains no real
  Claude/Codex executable yet and fails closed outside smoke mode until the
  selected pinned CLI is installed.

