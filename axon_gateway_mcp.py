#!/usr/bin/env python3
"""Local stdio MCP facade for the capability-limited Axon gateway.

This process exposes exactly three tools. It does not connect to Axon's Unix
sockets directly and cannot invoke shell, filesystem, database, worker, or
network-management operations. All authorization and durable task handling
remain in the localhost gateway.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP


DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

mcp = FastMCP(
    "axon-gateway",
    instructions=(
        "Delegate bounded messages to authorized Axon actors and retrieve "
        "their durable status/results. This server has no command-execution "
        "or cancellation capability."
    ),
)


class GatewayError(RuntimeError):
    pass


def _configuration() -> tuple[str, str]:
    base_url = os.environ.get("AXON_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise GatewayError("AXON_GATEWAY_URL must be a localhost HTTP endpoint")
    token = os.environ.get("AXON_GATEWAY_TOKEN", "")
    if len(token) < 32:
        raise GatewayError("AXON_GATEWAY_TOKEN must contain at least 32 characters")
    return base_url, token


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url, token = _configuration()
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=encoded,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "gateway request failed")
        except (json.JSONDecodeError, AttributeError):
            detail = "gateway request failed"
        raise GatewayError(f"Axon gateway rejected request ({exc.code}): {detail}") from None
    except URLError as exc:
        raise GatewayError("local Axon gateway is unavailable") from exc


@mcp.tool(description="Submit one bounded message to an authorized Axon actor.")
def delegate_task(
    conversation_id: str,
    text: str,
    idempotency_key: str,
    target: str = "axon-main",
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    return _request(
        "POST",
        "/v1/tasks",
        {
            "conversation_id": conversation_id,
            "text": text,
            "idempotency_key": idempotency_key,
            "target": target,
            "timeout_seconds": timeout_seconds,
        },
    )


@mcp.tool(description="Get durable execution status for an Axon gateway task.")
def get_task_status(task_id: str) -> dict[str, Any]:
    task = _request("GET", f"/v1/tasks/{task_id}")
    return {
        "task_id": task["task_id"],
        "target": task["target"],
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "error": task.get("error"),
    }


@mcp.tool(description="Retrieve the final result for an Axon gateway task.")
def get_task_result(task_id: str) -> dict[str, Any]:
    return _request("GET", f"/v1/tasks/{task_id}/result")


if __name__ == "__main__":
    mcp.run(transport="stdio")
