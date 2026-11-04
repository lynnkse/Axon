# ChatGPT–Axon gateway

This is a local proof of concept for delegating bounded messages to Axon's
persistent main session. It deliberately does not expose shell commands,
filesystem paths, databases, credentials, model selection, workers, arbitrary
process execution, cancellation, or attachments.

## Boundary

- HTTP binds only to `127.0.0.1` by default and rejects non-loopback binds.
- A bearer token of at least 32 characters is mandatory.
- The only currently authorized target is `axon-main`.
- Messages are limited to 16 KiB and timeouts to 5–900 seconds.
- Tasks are written to SQLite before submission and use idempotency keys.
- Replies must match both `source=chatgpt` and the generated `request_id`.
- Telegram explicitly ignores `chatgpt` and `live_voice` responses.
- The audit table stores lifecycle metadata, not message bodies.
- There is intentionally no cancellation endpoint.

## Local proof of concept

Use a locally generated secret. Do not commit or paste it into configuration:

```bash
export AXON_GATEWAY_TOKEN="$(openssl rand -hex 32)"
python axon_gateway.py
```

In a second terminal, the MCP facade can be launched over stdio:

```bash
export AXON_GATEWAY_TOKEN="<same local token>"
python axon_gateway_mcp.py
```

The MCP facade exposes exactly:

- `delegate_task`
- `get_task_status`
- `get_task_result`

It accepts only a localhost HTTP gateway URL. No listener, tunnel, firewall,
domain, or public endpoint is configured by these files.

## Deferred work

Secure MCP Tunnel configuration is a separate deployment step. Target routing
beyond `axon-main`, file transfer, and safe cancellation require explicit Axon
capabilities and authorization before they can be exposed.
