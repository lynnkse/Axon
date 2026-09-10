# Step 6 per-instance network topology verification

Status: implemented and adversarially exercised under gVisor `runsc`,
2026-09-09.

## Artifacts

- `topology/run_topology_smoke.sh`: reproducible two-instance topology builder,
  adversarial probe suite, and fail-safe cleanup harness.
- `topology/firewall.sh`: source-scoped IPv4/IPv6 `DOCKER-USER` chains for
  WAN-facing research and operational gateway addresses. Private, loopback,
  link-local, CGNAT, documentation, benchmark, multicast, reserved, and ULA
  ranges are rejected before DNS/public-HTTPS allows; all other protocols and
  ports are rejected.
- `topology/Dockerfile.firewall`: short-lived firewall installer image. It is
  the only Step 6 component run with `runc` and host networking, because gVisor
  correctly prevents sandboxed processes from changing host netfilter. It
  receives only `NET_ADMIN` and `NET_RAW`, then exits.
- `topology/Dockerfile.fixture` and `topology/net_fixture.py`: unprivileged,
  credential-free engine, operational-broker, service-gateway, socket, and
  network-probe fixtures. They test topology and are not production API
  brokers.
- The real Step 5 `axon-research-proxy:step5` image is used once per instance;
  it is not replaced by a fixture.

## Built topology

The harness creates two independent instances, A and B. Each has four unique
dual-stack bridges:

```text
engine -- internal private bridge -- research proxy -- research uplink
   |
   +---- internal private bridge -- operational broker
                                      |
                              internal broker bridge
                                      |
                              service-egress gateway -- service uplink
```

In concrete terms, the engine is attached only to its instance-private
`internal` bridge. Its research proxy is dual-homed on that bridge and its own
research uplink. Its operational broker is dual-homed on the private bridge
and an internal broker/gateway bridge. The service-egress gateway is dual-homed
on that broker bridge and a distinct operational uplink. No network or subnet
is shared between A and B, and research and operational uplinks are never
shared with each other.

All eight long-lived test containers use `--runtime=runsc`, read-only roots,
all capabilities dropped, no-new-privileges, fixed IPv4/IPv6 addresses, PID
and memory limits, and bounded tmpfs. No host ports are published.

## Actual proof on this host

The final run completed:

```text
STEP6_TOPOLOGY_SMOKE=PASS
```

Docker inspection proved every engine, proxy, broker, and gateway workload ran
with `runtime=runsc` and `readonly=true`. It also printed the exact attachment
set for every container; engines had one internal bridge, while proxies,
brokers, and gateways had only their two intended links.

Positive-path probes proved:

- instance A engine reached its own real research proxy on TCP 8787; the proxy
  returned the expected `422 {"error":"destination_denied"}` for a loopback
  fetch URL;
- instance A engine reached its own broker fixtures on 8443, 8444, 8445, and
  8446;
- A broker reached only its A service gateway on 9443 over IPv4;
- B broker reached only its B service gateway on 9443 over IPv6.

Direct-IP adversarial probes from A's engine to B's real research proxy,
operational broker, and engine socket endpoint were run for both address
families. All six returned `Network is unreachable`:

```text
172.31.20.20:8787       denied
172.31.20.30:8443       denied
172.31.20.10:9000       denied
fd00:6:20::20:8787      denied
fd00:6:20::30:8443      denied
fd00:6:20::10:9000      denied
```

Additional probes proved that A's engine could not reach its gateway-facing
broker network, public IPv4 HTTPS, or public IPv6 HTTPS; the research proxy
could not reach the operational gateway; and the operational gateway could
not reach the research proxy. Each engine created a mode-0600 Unix manager
socket inside its own mount namespace; neither container had a path to a
sibling instance's socket.

Four host firewall chains were installed—one for each A/B research/service
uplink address. Real TCP/80 attempts from the research proxy and service gate
were rejected, and both chains recorded packets in their final reject rules.
Attempts toward the other private role were recorded against the
`172.16.0.0/12` rejection before the port allows. Both IPv4 and IPv6 chains
were listed live and contained only private/special rejects, DNS allows,
TCP/443 allow, and final reject.

The images exercised by the successful run were:

```text
axon-network-fixture:step6
sha256:9813eb02fc09cf4d3eec7b84732022419528b7e2ce94fe2d4b83a26fc0841ff8

axon-firewall-helper:step6
sha256:7da5d8dbab2eece637e964c016cf9f958178a5634f59aa65bf782c09d90783fa

axon-research-proxy:step5
sha256:de91de184fe342231131d18ad735453b6a236e3a5f2741219f3a4f3f873d41ae
```

After the run, the cleanup trap removed all eight containers, all eight
bridges, and all four IPv4/IPv6 firewall chains. Explicit post-run checks found
no `axon-s6-*` Docker objects and both `iptables` and `ip6tables` reported the
test chain absent.

The complete copied-tree regression suite then passed:

```text
python3 -m unittest discover -s tests -v
Ran 42 tests in 3.347s
OK
```

Python compilation, shell syntax checks, and `git diff --check` also passed.

## Deliberately unverified in Step 6

- Production Claude/Codex, Supabase, Telegram, and Groq application brokers.
  Step 6 builds and proves their isolated network slot and dedicated service
  egress path; credential injection and per-API semantic enforcement remain
  later implementation work. The broker/gateway listeners here are explicitly
  credential-free topology fixtures.
- The service gateway's fixed hostname manifests, controlled DNS resolver,
  TLS address pinning, and provider-specific certificate checks. The kernel
  layer proved public TCP/443-only egress, but hostname identity belongs to the
  production gate implementation.
- A successful real public TCP/443 or DNS transaction through either uplink.
  This step tested denial/isolation and did not contact third-party APIs.
- Persistence of firewall policy across Docker daemon or host reboot. The test
  helper deliberately installs disposable chains; production orchestration
  must install equivalent rules transactionally before starting workloads.
- Host compromise, Docker-daemon compromise, malicious firewall-helper image,
  packet-flood/conntrack exhaustion, and production load/soak behavior.
- Tailscale/UI ingress. This step covers engine egress and cross-instance
  isolation; the separate userspace Tailscale topology remains a later step.
