# Step 7 operational-service egress staging verification

Status: implemented and independently exercised under gVisor `runsc`,
2026-09-09.

## Artifacts

- `operational/manifest.json`: immutable staging mapping for Claude, Codex,
  Supabase, Telegram, and Groq. Each opaque service ID maps to one exact
  hostname and one harmless credential-free health path.
- `operational/gateway.py`: fail-closed service gate. Callers submit only a
  service ID. The gate uses one explicitly configured resolver, validates the
  DNS transaction and every A/AAAA result, rejects non-public answers, pins a
  selected numeric address, and verifies TLS using the manifest hostname for
  SNI/certificate identity.
- `operational/broker.py`: credential-free staging broker with fixed local
  port-to-service mappings. It cannot forward caller-supplied URLs, hosts,
  headers, methods, paths, or credentials.
- `operational/Dockerfile.gateway` and `operational/Dockerfile.broker`:
  unprivileged Python 3.11 images using UIDs 10004 and 10005.
- `operational/run_staging_egress.sh`: disposable internal engine/broker link,
  internal broker/gateway link, separate service uplink, scoped IPv4/IPv6
  firewall installation, positive probes, denial probes, audit capture, and
  automatic cleanup.
- `tests/test_operational_gateway.py`: manifest, fail-closed selection, and
  multi-address DNS validation tests.

## Actual staging proof

The final credential-free staging run completed:

```text
STEP7_STAGING_EGRESS=PASS
```

Docker inspection showed the engine, broker, and gateway all using
`runtime=runsc`, read-only roots, dropped capabilities, no-new-privileges, and
only their intended networks. The engine had no WAN network. The broker had
only the engine-facing and gate-facing internal networks. Only the service
gate had the dedicated service uplink.

Five independent engine-to-broker-to-gateway probes performed controlled DNS,
validated all returned addresses, connected to a pinned public IPv4 address,
verified the real service certificate, sent an unauthenticated HTTPS `HEAD`,
and received a real HTTP response:

```text
Claude    api.anthropic.com                       HTTP 404
Codex     api.openai.com                          HTTP 401
Supabase  configured-project.supabase.co          HTTP 401
Telegram  api.telegram.org                        HTTP 302
Groq      api.groq.com                            HTTP 404
```

These non-2xx statuses are expected for credential-free health requests and
prove DNS/TCP/TLS/HTTP reachability without invoking inference, database
mutation, message sending, or transcription. The Supabase hostname is exact in
the mounted manifest; this report intentionally abbreviates it.

The firewall counters independently recorded 10 controlled UDP DNS packets,
42 accepted TCP/443 packets, and one rejected TCP/80 packet. The gateway used
the host's explicit staging resolver at `192.168.1.1`; arbitrary private/public
DNS destinations remained denied. The service uplink had explicit highest
gateway priority, avoiding dual-homed route dependence on attachment order.

Visible denial checks returned:

```text
unknown service ID                 {"outcome":"service_disabled","code":403}
extra caller-supplied host field   {"outcome":"malformed_request","code":422}
engine -> gateway directly         Network is unreachable
gateway -> public TCP/80           network_denied
```

Safe audit records contained only service ID, outcome, latency, and manifest
hash. They contained no URL, IP, credential, headers, response body, or
Supabase project identifier.

The independently rerun copied-tree suite passed:

```text
python3 -m unittest discover -s tests -v
Ran 48 tests in 3.599s
OK
```

Python compilation, shell syntax, JSON parsing, and `git diff --check` passed.

Tested image IDs:

```text
axon-service-broker:step7
sha256:4aefa4d6132817bf8d6d134224f470a7aae6448ecca9aab020b77f0ea885cb99

axon-service-gateway:step7
sha256:d8fe5918f43ed660c45542a5f4b782eeb8e061cb7981293be5cf87f96df45d05
```

Cleanup removed every `axon-s7-*` container/network and the `AX7SG` IPv4 and
IPv6 chains. Independent inspection reported both firewall chains absent.

## Deliberately unverified in Step 7

- Authenticated or mutating API operations. No real credential was mounted or
  transmitted, and no prompt, database write, Telegram message, or audio was
  sent.
- Provider-specific production broker semantics, credential injection,
  quotas, request/response schemas, streaming, and response-size enforcement.
  This step proves upstream reachability and destination identity only.
- Additional hosts used by the interactive Claude/Codex CLIs. Only the primary
  API hosts were verified; ancillary hosts remain deny-by-default until a
  separately reviewed staging capture justifies them.
- Successful public IPv6 service egress. AAAA answers were queried and
  validated, and IPv6 deny rules remained installed, but this host selected
  working IPv4 paths and has no demonstrated public IPv6 route.
- Resolver DNSSEC/authentication and TCP fallback for truncated DNS responses.
  The bounded client rejects truncation rather than falling back silently.
- Long-running availability, provider rate limits, authenticated certificate
  rotation behavior, firewall persistence across host reboot, and production
  secret-store integration.
