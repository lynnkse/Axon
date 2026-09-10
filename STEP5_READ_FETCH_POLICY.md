# Step 5 read-fetch research policy verification

Status: implemented and adversarially exercised under gVisor `runsc`,
2026-09-09.

## Artifacts

- `sandbox/policy.schema.json`: strict JSON Schema for versioned policy ID,
  exact destination hosts, approved textual MIME types, and bounded request,
  response, redirect, timeout, concurrency, and rate limits. Unknown fields
  are forbidden.
- `sandbox/policy.py`: dependency-free fail-closed loader and validator. It
  mirrors the schema's constraints, canonicalizes IDNA hostnames, rejects IP
  literals/non-FQDNs and canonical duplicates, and computes a canonical policy
  hash for provenance and audit records.
- `sandbox/research_proxy.py`: credential-free HTTPS read-fetch service with a
  bounded HTTP request parser, exact-host policy enforcement, validation of
  every DNS answer, IP-pinned TLS connections with the original hostname used
  for SNI/certificate verification, manual redirect processing, response
  limits, MIME/PDF rejection, concurrency/rate limits, structured provenance,
  and path/query/body-free audit logging.
- `sandbox/Dockerfile.proxy`: minimal Python 3.11 image running as UID/GID
  10002 with no third-party runtime dependency.
- `sandbox/example.policy.json`: non-secret, restrictive example policy.
- `tests/test_research_proxy.py`: adversarial unit and loopback integration
  suite covering schema/validator behavior, URL ambiguity, IP classes,
  multi-answer DNS, redirects, rebinding, size bounds, MIME/PDF handling,
  malformed HTTP, rate/concurrency enforcement, provenance, and audit safety.

## Security properties exercised

Redirects are never auto-followed: every `Location` is parsed, policy-checked,
resolved, and pinned as a new hop before connecting. Resolution fails if any
returned address is non-public. The connection is made directly to one of the
already validated address tuples rather than resolving the hostname again,
closing the DNS-check/reconnect gap while preserving TLS hostname validation.

Inbound parsing has fixed request-line, header-line, total-header, header-count,
and body ceilings; it rejects duplicate headers, transfer encoding, absent or
invalid content length, wrong content type, malformed JSON, and extra JSON
fields. Upstream responses reject compression, invalid/oversized declared
lengths, streaming overflow, disallowed MIME types, explicit PDF MIME, and PDF
magic bytes disguised as an allowed type.

Audit events contain policy identifiers, hostname, a truncated SHA-256 URL
correlator, byte counts, status/category, and latency. They never contain the
raw URL path/query, request body, response body, or upstream headers.

## Actual proof on this host

The complete copied-tree suite ran with:

```text
python3 -m unittest discover -s tests -v
Ran 42 tests in 2.712s
OK
```

That total comprises 28 new Step 5 tests and the 14 pre-existing tests. The new
tests use controlled fake resolvers/transports for deterministic attack
simulation and real loopback TCP for malformed/incremental HTTP parsing. They
proved rejection of userinfo, non-HTTPS schemes, non-443 ports, fragments,
backslashes, ambiguous paths, control whitespace, IP literals, private and
special-use IP ranges, mixed public/private DNS answers, a redirect to a
private address, DNS answer changes after validation, oversized requests and
responses, compressed responses, invalid PDF content, and malformed requests.
They also prove that pinned connections use the selected numeric address but
the original policy-approved hostname for TLS.

Python compilation, JSON parsing of the schema, and `git diff --check` passed.
The image built successfully as `axon-research-proxy:step5`; the tested image
ID was
`sha256:de91de184fe342231131d18ad735453b6a236e3a5f2741219f3a4f3f873d41ae`.

A real container started successfully with `--runtime=runsc`, `--network=none`,
a read-only root, all capabilities dropped, no-new-privileges, PID/memory
ceilings, and a bounded `/tmp` tmpfs. Docker reported:

```text
running runtime=runsc network=none readonly=true caps=["ALL"]
```

Inside the sandbox the process ran as UID 10002 and `/proc/net/dev` exposed
only `lo`. A real local HTTP request to the service for
`https://127.0.0.1/` returned:

```text
422 {"error":"destination_denied"}
```

Mounting the JSON Schema itself in place of a policy made the container exit
1 before listening, with an explicit missing/unknown-key validation error,
proving invalid configuration fails closed at startup.

## Deliberately unverified in Step 5

- Real public HTTPS/DNS traffic and certificate-chain behavior. The container
  smoke test intentionally used `--network=none`; attack paths were exercised
  with deterministic injected resolvers/transports and local sockets.
- Enforcement by the Step 3 firewall/nftables topology, including confinement
  of engine callers to this service and restriction of this service's public
  egress. This image is ready to be placed in that topology but does not create
  host firewall state itself.
- A production recursive resolver's anti-rebinding/caching behavior. Security
  does not depend on it for a single hop because every returned address is
  checked and the chosen address is pinned, but resolver availability and
  latency remain deployment concerns.
- End-to-end wall-clock enforcement while a platform DNS lookup itself is
  stalled. Socket connect/TLS/read operations use the policy deadline; a
  production deployment still needs bounded resolver behavior at the network
  layer.
- Load/soak behavior at the configured maxima, kernel-level resource exhaustion,
  audit-log shipping/retention, and multi-instance deployment isolation.
- Browser rendering, JavaScript execution, file downloads, PDF extraction, and
  authenticated sources. They are intentionally outside this read-only textual
  fetch policy.
