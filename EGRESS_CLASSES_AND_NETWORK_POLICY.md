# Axon egress classes and network policy

Status: Step 3 design contract, 2026-09-07. This document resolves the former
“two egress points” ambiguity and defines enforceable network and
application-layer policy. It does not implement networks, firewalls, proxies,
or credential brokers.

## 1. Resolution of the egress model

An Axon engine has **two trust classes**, not two literal destinations:

1. **Operational services** selected by the operator: the active LLM provider,
   Supabase, Telegram, and optional Groq. Requests pass through credential and
   policy brokers; the engine never receives their raw credentials.
2. **Model-selected public research**: URLs chosen during a model turn. These
   pass through the separate, credential-free read-fetch proxy and its
   instance policy.

Tailscale coordination/relay traffic is infrastructure-plane traffic from the
separate sidecar. Image pulls and time synchronization belong to the host.
Neither is engine egress.

The engine has no direct WAN interface, no public default route, and no direct
public DNS resolver. It can reach only fixed private service peers and ports.
Policy failure, broker failure, or proxy failure is returned explicitly; there
is never a direct-network fallback.

## 2. Per-instance network topology

Each instance receives isolated point-to-point internal networks rather than
one shared service LAN:

```text
                                  +--> LLM credential broker --> LLM egress gate --> WAN
                                  +--> Supabase broker --------> SB egress gate --> WAN
engine (no WAN/default route) ----+--> Telegram broker --------> TG egress gate --> WAN
                                  +--> Groq broker ------------> Groq gate ------> WAN [optional]
                                  +--> read-fetch proxy -------------------------> WAN
                                  <-- Tailscale sidecar ingress [UI ports only]
```

For deployment `<id>`, use distinct `internal: true` engine-facing bridges:

```text
<id>-llm-link
<id>-supabase-link
<id>-telegram-link
<id>-groq-link       # only when voice is enabled
<id>-research-link   # only when browsing is enabled
<id>-ui-link         # only when remote UI is enabled
```

Every link has exactly the engine and its intended peer. No link is reused by
another instance. Brokers/gates receive separate upstream links; the engine is
never attached to those networks. Static private addresses may be assigned by
the orchestrator for firewall stability, but applications use fixed service
names supplied through non-secret configuration.

The Tailscale sidecar exposes only dashboard and Web CLI ingress toward the
engine. It does not offer SOCKS, HTTP CONNECT, subnet routing, exit-node, or IP
forwarding services to the engine.

## 3. Engine allow matrix

The engine network namespace has a default-deny OUTPUT/FORWARD policy. Only
these new TCP connections are accepted:

| Source | Destination peer | Port | Condition | Purpose |
|---|---|---:|---|---|
| Engine | LLM broker | 8443 | Always, selected provider only | Claude or Codex API protocol |
| Engine | Supabase broker | 8444 | Always | Approved database/function operations |
| Engine | Telegram broker | 8445 | Telegram enabled | Poll, reply, media metadata/download |
| Engine | Groq broker | 8446 | Voice provider is Groq | Audio transcription |
| Engine | Read-fetch proxy | 8787 | Browsing enabled | Model-selected HTTPS GET |

Ingress to the engine is limited to:

| Source | Destination | Port | Purpose |
|---|---|---:|---|
| Tailscale sidecar | Engine | 8501/TCP | Streamlit dashboard |
| Tailscale sidecar | Engine | 7690/TCP | Web CLI WebSocket |

Same-container Unix sockets do not traverse network policy. Return traffic is
allowed only through connection tracking (`ESTABLISHED,RELATED`). All other
engine ingress, egress, forwarding, ICMP forwarding, and IPv6 traffic is
dropped and rate-limited denial metadata is logged outside the engine.

The engine does not need public DNS. Internal peer names are supplied by a
private authoritative resolver which answers only the deployment’s internal
zone and returns `REFUSED` for all other names. If static `/etc/hosts` entries
prove sufficient, prefer them and expose no DNS socket to the engine.

## 4. Firewall-rule-level requirements

Docker network declarations are not the complete security boundary. The host
must enforce equivalent rules before/independently of Docker’s permissive
forwarding rules. With iptables-nft, install them in `DOCKER-USER`; a native
nftables deployment must use a base chain ordered before Docker accepts.

For every instance, resolve concrete interface and IP variables at deployment:

```text
ENGINE_IP
LLM_BROKER_IP      LLM_PORT=8443
SB_BROKER_IP       SB_PORT=8444
TG_BROKER_IP       TG_PORT=8445
GROQ_BROKER_IP     GROQ_PORT=8446       # optional
FETCH_PROXY_IP     FETCH_PORT=8787       # optional
TAILSCALE_IP
```

Required logical rules, in this order:

```text
accept ct state established,related
drop   ct state invalid

accept src ENGINE_IP dst LLM_BROKER_IP  tcp dport 8443
accept src ENGINE_IP dst SB_BROKER_IP   tcp dport 8444
accept src ENGINE_IP dst TG_BROKER_IP   tcp dport 8445       if enabled
accept src ENGINE_IP dst GROQ_BROKER_IP tcp dport 8446       if enabled
accept src ENGINE_IP dst FETCH_PROXY_IP tcp dport 8787       if enabled

accept src TAILSCALE_IP dst ENGINE_IP tcp dport 8501          if enabled
accept src TAILSCALE_IP dst ENGINE_IP tcp dport 7690          if enabled

drop+rate-limited-log src ENGINE_IP
drop+rate-limited-log dst ENGINE_IP
```

There is no rule for port 7681/ttyd. There is no engine rule for TCP 53, UDP
53, TCP 80, TCP 443, private LANs, host gateways, metadata endpoints, sibling
containers, or Docker DNS forwarding to public resolvers.

If same-bridge filtering is used anywhere, host deployment must enable bridge
netfilter and prove packets traverse the policy. The preferred two-peer links
reduce dependence on filtering lateral peers but do not replace the explicit
engine drop rules.

### Broker-to-gate rules

Each credential broker can reach only its matching upstream gate on one fixed
port. It cannot reach another broker, proxy, engine interface, host service,
private subnet, or raw WAN address. Each upstream gate:

- accepts traffic only from its matching broker;
- selects a fixed upstream service manifest;
- resolves DNS itself through a controlled resolver;
- allows only TCP 443 to resolved/validated public addresses;
- rejects all loopback, private, link-local, carrier-grade NAT, multicast,
  reserved, documentation, benchmark, and metadata ranges for IPv4 and IPv6;
- pins the validated address while preserving TLS hostname/SNI validation;
- blocks redirects to hosts outside its own service manifest;
- cannot connect to instance-private or host networks;
- emits trusted audit records.

The application broker enforces HTTP/API semantics and injects credentials.
The egress gate enforces destination identity. Keeping these controls separate
prevents a compromised broker from opening arbitrary raw internet connections.
They may later share one hardened process only if equivalent kernel-enforced
destination confinement is demonstrated.

## 5. Operational-service policies

Operational destinations are fixed by deployment, never selected by the model.
Every broker rejects absolute URLs, caller-supplied upstream hosts,
`Host`/`:authority` overrides, `CONNECT`, credential headers, proxy headers,
and hop-by-hop headers. Request and response sizes, duration, concurrency, and
rate are bounded per instance.

### 5.1 LLM service

Exactly one provider policy is enabled for the selected engine:

```text
Claude engine -> Claude broker -> versioned Claude upstream-host manifest
Codex engine  -> Codex broker  -> versioned Codex upstream-host manifest
```

The broker supports only the API operations required by the pinned CLI,
injects the provider credential, enforces instance quota/rate limits, and
streams bounded responses. It must not expose a generic HTTP or CONNECT proxy.

`api.anthropic.com:443` and `api.openai.com:443` are expected primary API
hosts, but the interactive Claude/Codex CLIs may use additional authenticated
service hosts. The final manifest is produced by network-capturing each pinned
CLI in staging. Until that manifest is reviewed, the gate remains deny-all;
unknown telemetry, updater, login, package, and web-search endpoints are not
automatically added. This is a validation gate, not permission to default open.

### 5.2 Supabase service

The Supabase gate permits exactly one configured project hostname on TCP 443.
The broker owns the anon/service-role credential and exposes a narrower local
API. Initial core allowlist derived from the current copied tree:

| Upstream path class | Methods | Named resources |
|---|---|---|
| `/rest/v1/<table>` | GET, POST, PATCH | `actor_state`, `alive_state`, `anton_model`, `dreams`, `insights`, `memory`, `messages`, `projects`, `rules`, `summaries` |
| `/rest/v1/rpc/<name>` | POST | `increment_rule_usage` |
| `/functions/v1/<name>` | POST | `embed`, `search` |

Instance-plugin policy may add its own project and tables, currently including
`conversations`, `valence`, `body_state`, and `dreams`, but this is a separate
broker identity/policy—not a wildcard expansion of the core project.

Dashboard read-only resources currently include `food_entries`, `fitness_log`,
`compulsive_behavior_tracking`, `personal_tasks`, `project_experiments`, and
study tables (`study_areas`, `study_attempts`, `study_book_chunks`,
`study_books`, `study_exercises`, `study_topics`). These receive GET only.
Curator receives GET/PATCH only for `rules` and `insights`.

For each resource, policy specifies allowed methods, query keys, maximum rows,
body schema/size, and caller service. The broker constructs the upstream URL;
the engine cannot submit a complete upstream URL. DELETE is denied globally.
RPC/function names are exact, not prefixes. Supabase storage, realtime, auth,
GraphQL, arbitrary RPC, and arbitrary Edge Functions are denied.

Service-role power must not be reproduced as an unrestricted local endpoint.
Where practical, split read-only dashboard identity from manager mutation
identity and enforce row/table policy independently of Supabase RLS.

### 5.3 Telegram service

The gate permits only the reviewed Telegram Bot API/file service hosts on TCP
443 (normally `api.telegram.org`). The broker owns the bot token and maps the
private instance identity to one bot.

Allowed operation classes:

- Long-poll updates for that bot.
- Send/edit/delete the bot’s own messages as required by current handlers.
- Chat actions and callback-query acknowledgements.
- Fetch bot-file metadata and download the returned file path.
- Set command metadata if current startup requires it.

The broker forms token-bearing paths; the engine never sends or receives the
real token. It pins the authorized chat/user IDs, limits media size, forbids
arbitrary Telegram method forwarding, and strips token-bearing URLs from
errors/logs. Webhooks and inbound host ports are disabled in v1.

Telegram media URLs returned to the engine must remain broker-local opaque
handles. The engine cannot use them as arbitrary fetch URLs.

### 5.4 Groq service

Disabled unless `VOICE_PROVIDER=groq`. The gate permits only
`api.groq.com:443`; the broker exposes a transcription-only operation using
the pinned model(s), initially `whisper-large-v3-turbo`.

It accepts bounded audio, injects the Groq credential, limits duration/bytes
and concurrency, and returns text plus non-secret metadata. Chat/completions,
model administration, arbitrary paths, and caller authorization headers are
denied.

## 6. Model-selected public research policy

Research is deliberately separate from all operational brokers. The engine’s
model-visible `sandbox_fetch(url)` tool sends one declarative URL to its own
credential-free proxy over `<id>-research-link`.

An instance-owned, read-only policy contains:

```json
{
  "schema_version": 1,
  "policy_id": "instance-readonly-research",
  "allowed_hosts": ["en.wikipedia.org", "arxiv.org"],
  "allowed_content_types": ["text/html", "text/plain", "application/json"],
  "limits": {
    "request_bytes": 8192,
    "response_bytes": 5242880,
    "redirects": 3,
    "timeout_seconds": 15,
    "concurrency": 2,
    "requests_per_minute": 20
  }
}
```

Framework invariants cannot be relaxed by policy:

- Outbound method is GET and scheme is HTTPS.
- Ports other than 443, IP-literal URLs, embedded credentials, fragments, and
  non-canonical hostnames are rejected.
- Caller headers, cookies, authorization, bodies, and proxy settings are
  ignored/rejected rather than forwarded.
- Allowlist matching is exact after lowercase/IDNA/trailing-dot
  canonicalization; subdomains require separate entries.
- Every DNS result and redirect hop is validated; connections are pinned to a
  validated public address while TLS verifies the original hostname.
- Private/special IPv4 and IPv6 destinations and cross-instance networks are
  blocked both in code and host firewall policy.
- Redirects resolve relative locations and may target only another explicitly
  allowed host.
- Compressed and decoded sizes are bounded; header count/bytes and total wall
  time are bounded.
- V1 accepts HTML, plain text, and JSON. PDF is denied.
- Results carry requested/final URL, retrieval time, status, media type, byte
  counts, policy ID/hash, and an explicit untrusted-content marker.
- Failure is returned as a structured denial/error; no operational broker or
  direct network route is tried.

The fetch proxy receives no LLM, Supabase, Telegram, Groq, Tailscale, instance,
or host credential. Its WAN firewall permits controlled DNS plus TCP 443 to
public addresses and denies all private/special destinations. Because public
host allowlisting is application-layer, the proxy remains a distinct
high-risk parser service under gVisor with read-only root, no host mounts,
bounded resources, and trusted external audit collection.

## 7. Tailscale infrastructure policy

The userspace sidecar alone may contact the coordination, DERP/STUN, DNS, and
peer endpoints required by the pinned Tailscale version. It owns its auth key
and node-state volume. The engine cannot read either.

Tailnet ACLs grant the authorized user/device access only to this instance’s
8501 and 7690 services. They grant no subnet routing or cross-instance access.
The sidecar forwards those two ports to the engine over `<id>-ui-link` and has
no general egress-proxy listener. The engine cannot initiate new connections
through the sidecar.

## 8. Denial, audit, and health behavior

Every layer reports a stable reason class without leaking credentials:

```text
network_denied
service_disabled
operation_denied
destination_denied
rate_limited
request_too_large
response_too_large
upstream_timeout
upstream_unavailable
policy_invalid
```

Trusted audit records include UTC timestamp, instance ID, service class,
policy/manifest hash, operation class, canonical destination, status, bounded
byte counts, latency, and outcome. They redact credentials, Telegram token
segments, cookies, authorization headers, signed parameters, and fetched body
content. Denial logging is rate-limited to prevent disk exhaustion.

Health checks distinguish:

- broker process readiness;
- upstream-gate policy readiness;
- optional upstream reachability;
- engine-to-broker connectivity;
- disabled service state.

Health probes never send LLM prompts, Telegram messages, Supabase mutations,
Groq jobs, or public research requests. Upstream outage marks the capability
degraded and never changes firewall policy.

## 9. Policy ownership and change control

- The shared framework owns schemas, maximum ceilings, blocked address ranges,
  firewall templates, and broker implementations.
- Each instance deployment owns which optional classes are enabled, its exact
  research hosts, Supabase project/resource policy, UI exposure, and tighter
  limits.
- Broker credentials and upstream manifests are installed only by the trusted
  host/orchestrator.
- Instance plugins may request a tool but cannot read, write, or widen policy.
- Policies are validated and hashed before startup. Unknown keys or versions
  fail closed.
- Changes require review, a new configuration revision, restart, and audit
  entry. No model-triggered or live policy mutation exists.

## 10. Verification requirements for implementation

Implementation is not accepted until automated tests prove:

1. Engine can reach each enabled private peer only on its declared port.
2. Engine cannot reach public IPv4/IPv6, public DNS, host gateway/metadata,
   another broker, sibling instance, or disabled service.
3. Tailscale sidecar can initiate only UI connections to the engine; engine
   cannot use it as an exit path.
4. Each broker can reach only its matching upstream gate.
5. Gates reject unlisted hosts, ports, redirects, private addresses, DNS
   changes, header/authority smuggling, and alternate IP families.
6. Supabase broker denies DELETE, arbitrary tables/RPC/functions, excessive
   row limits, and methods not granted to the calling component.
7. Telegram broker never reveals the bot token and rejects arbitrary methods,
   chats, file paths, and oversized media.
8. Groq broker exposes transcription only and is unreachable when disabled.
9. Research proxy passes adversarial redirect, SSRF, DNS-rebinding,
   compression, timeout, size, and malformed-request tests.
10. Killing or misconfiguring any proxy/broker produces an explicit failure
    without direct fallback.
11. Packet capture from the engine shows no raw provider credential and no
    connection outside the private-peer matrix.
12. Effective host firewall rules are generated from validated deployment
    state, atomically installed, and removed on teardown without affecting
    another instance.

## 11. Step 3 acceptance checklist

- [ ] Two trust classes and four operational service classes accepted.
- [ ] Per-instance point-to-point topology accepted.
- [ ] Exact engine peer/port matrix accepted.
- [ ] Default-deny host firewall semantics accepted.
- [ ] Broker/gate separation accepted or explicitly simplified with equivalent
      confinement evidence.
- [ ] Supabase resource/method baseline accepted.
- [ ] Telegram operation baseline accepted.
- [ ] Groq transcription-only policy accepted.
- [ ] Read-fetch policy schema and immutable invariants accepted.
- [ ] Tailscale infrastructure policy accepted.
- [ ] Denial/audit/health semantics accepted.
- [ ] Implementation verification matrix accepted.
