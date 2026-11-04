# Axon Voice bridge

This is a deliberately thin mobile web/PWA voice surface. The browser creates
a GPT-Live WebRTC session through the localhost bridge. When GPT-Live emits a
client delegation, the bridge submits a bounded transcript packet to the
existing Axon gateway and returns the correlated result with
`session.commentary.append`.

## Security boundary

- Both Python services bind only to `127.0.0.1`.
- Tailscale Serve is the only intended HTTPS ingress; no public funnel is used.
- OpenAI and Axon bearer credentials never reach the browser.
- A one-time pairing URL installs a derived `Secure`, `HttpOnly`,
  `SameSite=Strict` browser cookie. The raw pairing token is not stored there.
- Only `axon-main` delegation and task-result retrieval are exposed.
- There is no shell, filesystem, database, cancellation, attachment, worker,
  model-selection, or arbitrary command endpoint.

## Required runtime variables

```bash
export OPENAI_API_KEY="..."
export AXON_GATEWAY_TOKEN="$(openssl rand -hex 32)"
export AXON_VOICE_PAIR_TOKEN="$(openssl rand -hex 32)"
```

Start the local services with the same gateway token:

```bash
python axon_gateway.py
python axon_voice_bridge.py
```

Publish only the voice bridge privately over the Tailnet:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8766
```

Open `https://<tailscale-dns-name>/pair/<AXON_VOICE_PAIR_TOKEN>` once on the
phone. The normal home-screen URL contains no credential after pairing.
