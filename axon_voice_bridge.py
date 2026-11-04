#!/usr/bin/env python3
"""Private mobile voice bridge for GPT-Live and the local Axon gateway.

The browser talks only to this service. OpenAI and Axon credentials stay on
the server, and Axon's Unix sockets remain localhost-only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


LOG = logging.getLogger("axon.voice_bridge")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
DEFAULT_STATIC_DIR = Path(__file__).with_name("voice_web")
OPENAI_LIVE_URL = "https://api.openai.com/v1/live/sessions"
MAX_SDP_BYTES = 256 * 1024
MAX_JSON_BYTES = 24 * 1024
MAX_MESSAGE_BYTES = 16 * 1024
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

LIVE_INSTRUCTIONS = """You are the low-latency voice conversation layer for Axon.
Handle ordinary conversation yourself. Delegate to the client when the user explicitly
asks Axon to act, or asks about Axon's persistent projects, experiments, repositories,
machines, tools, or long-running operational work. Before delegating, briefly tell the
user what bounded request you are sending. Never claim backend work happened until a
client delegation result arrives. Keep delegated requests narrow and preserve the
separation between this conversation and Axon's persistent context."""


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def live_session_payload(sdp: str) -> dict[str, Any]:
    return {
        "session": {
            "model": "gpt-live-1",
            "instructions": LIVE_INSTRUCTIONS,
            "delegation": {"type": "client"},
        },
        "transport": {"type": "webrtc", "sdp": sdp},
    }


def task_packet(transcript: str, offset_ms: int | None = None) -> str:
    clean = transcript.strip()
    if len(clean.encode("utf-8")) > MAX_MESSAGE_BYTES - 1024:
        while len(clean.encode("utf-8")) > MAX_MESSAGE_BYTES - 1024:
            clean = clean[len(clean) // 10 + 1 :]
    offset = f" near audio offset {offset_ms} ms" if offset_ms is not None else ""
    return (
        "A GPT-Live voice conversation explicitly delegated the following bounded "
        f"request{offset}. Use Axon's own persistent context and tools. Infer the latest "
        "user request from the transcript, do not broaden its scope, and return a concise "
        "answer suitable for speech.\n\nVOICE TRANSCRIPT:\n" + clean
    )


def auth_cookie_value(pair_token: str) -> str:
    return hmac.new(
        pair_token.encode("utf-8"), b"axon-voice-browser-v1", hashlib.sha256
    ).hexdigest()


class VoiceBridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        openai_api_key: str,
        gateway_token: str,
        pair_token: str,
        gateway_url: str,
        static_dir: Path,
    ):
        super().__init__(address, handler)
        self.openai_api_key = openai_api_key
        self.gateway_token = gateway_token
        self.pair_token = pair_token
        self.cookie_value = auth_cookie_value(pair_token)
        self.gateway_url = gateway_url.rstrip("/")
        self.static_dir = static_dir.resolve()


class VoiceBridgeHandler(BaseHTTPRequestHandler):
    server: VoiceBridgeServer

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _cookie_authorized(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        for item in cookies.split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name == "axon_voice_session":
                return hmac.compare_digest(value, self.server.cookie_value)
        return False

    def _require_auth(self) -> bool:
        if self._cookie_authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "pair this browser first"})
        return False

    def _require_same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            self._json(HTTPStatus.FORBIDDEN, {"error": "same-origin request required"})
            return False
        parsed = urlparse(origin)
        if parsed.scheme != "https" or parsed.netloc != host:
            self._json(HTTPStatus.FORBIDDEN, {"error": "origin rejected"})
            return False
        return True

    def _read_json(self, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 1 or length > maximum:
            raise ValueError("invalid request size")
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def _upstream_json(
        self, url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
        token: str,
    ) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            raw = exc.read(2048)
            try:
                detail = json.loads(raw).get("error", "upstream request failed")
                if isinstance(detail, dict):
                    detail = detail.get("message", "upstream request failed")
            except (json.JSONDecodeError, AttributeError):
                detail = "upstream request failed"
            raise UpstreamError(exc.code, str(detail)) from exc
        except (URLError, TimeoutError) as exc:
            raise UpstreamError(HTTPStatus.BAD_GATEWAY, "upstream unavailable") from exc

    def _serve_static(self, relative: str) -> None:
        name = "index.html" if relative in {"", "/"} else unquote(relative.lstrip("/"))
        candidate = (self.server.static_dir / name).resolve()
        if self.server.static_dir not in candidate.parents and candidate != self.server.static_dir:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if path.startswith("/pair/"):
            supplied = unquote(path.removeprefix("/pair/"))
            if not hmac.compare_digest(supplied, self.server.pair_token):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid pairing link"})
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "axon_voice_session=" + self.server.cookie_value
                + "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000",
            )
            self._security_headers()
            self.end_headers()
            return
        if not self._require_auth():
            return
        if path.startswith("/api/tasks/"):
            task_id = path.removeprefix("/api/tasks/")
            if not TASK_ID_RE.fullmatch(task_id):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid task id"})
                return
            try:
                status, result = self._upstream_json(
                    f"{self.server.gateway_url}/v1/tasks/{task_id}/result",
                    token=self.server.gateway_token,
                )
            except UpstreamError as exc:
                self._json(exc.status, {"error": str(exc)})
                return
            self._json(status, result)
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth() or not self._require_same_origin():
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json(MAX_SDP_BYTES + 4096 if path == "/api/live/session" else MAX_JSON_BYTES)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/live/session":
            sdp = body.get("sdp")
            if not isinstance(sdp, str) or not sdp.strip() or len(sdp.encode()) > MAX_SDP_BYTES:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid SDP"})
                return
            if not self.server.openai_api_key:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "OPENAI_API_KEY is not configured"})
                return
            try:
                status, result = self._upstream_json(
                    OPENAI_LIVE_URL,
                    method="POST",
                    body=live_session_payload(sdp),
                    token=self.server.openai_api_key,
                )
            except UpstreamError as exc:
                self._json(exc.status, {"error": str(exc)})
                return
            self._json(status, result)
            return

        if path == "/api/delegate":
            delegation_id = str(body.get("delegation_id", "")).strip()
            conversation_id = str(body.get("conversation_id", "")).strip()
            transcript = str(body.get("transcript", "")).strip()
            offset = body.get("offset_ms")
            if not delegation_id or len(delegation_id) > 256:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid delegation id"})
                return
            if not conversation_id or len(conversation_id) > 256:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid conversation id"})
                return
            if not transcript or len(transcript.encode()) > MAX_MESSAGE_BYTES:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid transcript"})
                return
            try:
                offset_ms = int(offset) if offset is not None else None
            except (TypeError, ValueError):
                offset_ms = None
            request_body = {
                "conversation_id": conversation_id,
                "idempotency_key": "live:" + hashlib.sha256(
                    f"{conversation_id}:{delegation_id}".encode()
                ).hexdigest(),
                "target": "axon-main",
                "text": task_packet(transcript, offset_ms),
                "timeout_seconds": 600,
            }
            try:
                status, result = self._upstream_json(
                    f"{self.server.gateway_url}/v1/tasks",
                    method="POST",
                    body=request_body,
                    token=self.server.gateway_token,
                )
            except UpstreamError as exc:
                self._json(exc.status, {"error": str(exc)})
                return
            self._json(status, result)
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def build_server(
    host: str,
    port: int,
    *,
    openai_api_key: str,
    gateway_token: str,
    pair_token: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    static_dir: Path = DEFAULT_STATIC_DIR,
) -> VoiceBridgeServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("voice bridge must remain localhost-only")
    if len(gateway_token) < 32 or len(pair_token) < 32:
        raise ValueError("gateway and pairing tokens must contain at least 32 characters")
    parsed = urlparse(gateway_url)
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Axon gateway URL must be localhost")
    if not static_dir.is_dir():
        raise ValueError("voice web directory is missing")
    return VoiceBridgeServer(
        (host, port),
        VoiceBridgeHandler,
        openai_api_key=openai_api_key,
        gateway_token=gateway_token,
        pair_token=pair_token,
        gateway_url=gateway_url,
        static_dir=static_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    gateway_token = os.environ.get("AXON_GATEWAY_TOKEN", "")
    pair_token = os.environ.get("AXON_VOICE_PAIR_TOKEN", "")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        server = build_server(
            args.host,
            args.port,
            openai_api_key=openai_api_key,
            gateway_token=gateway_token,
            pair_token=pair_token,
            gateway_url=os.environ.get("AXON_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not openai_api_key:
        LOG.warning("OPENAI_API_KEY is absent; UI will load but Live session creation is disabled")
    LOG.info("Axon voice bridge listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
