#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import socket
import socketserver
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from email.message import Message
from typing import Callable, Iterable, Mapping

from sandbox.policy import PolicyError, ResearchPolicy, canonical_host, load_policy


class FetchError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str


def parse_target(url: object, policy: ResearchPolicy) -> Target:
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > policy.limits["request_bytes"]:
        raise FetchError("malformed_request", "url is missing or too large")
    if url != url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise FetchError("destination_denied", "url contains whitespace/control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise FetchError("destination_denied", "url cannot be parsed") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise FetchError("destination_denied", "only absolute HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise FetchError("destination_denied", "embedded credentials are forbidden")
    if parsed.fragment:
        raise FetchError("destination_denied", "URL fragments are forbidden")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise FetchError("destination_denied", "invalid port") from exc
    if port != 443:
        raise FetchError("destination_denied", "only port 443 is allowed")
    try:
        host = canonical_host(parsed.hostname or "")
    except PolicyError as exc:
        raise FetchError("destination_denied", str(exc)) from exc
    if host not in policy.allowed_hosts:
        raise FetchError("destination_denied", "host is not allowlisted")
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    if path.startswith("//") or "\\" in path:
        raise FetchError("destination_denied", "ambiguous request target")
    return Target(url=urllib.parse.urlunsplit(("https", host, parsed.path or "/", parsed.query, "")),
                  host=host, port=port, path=path)


def validate_public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise FetchError("destination_denied", "DNS returned an invalid address") from exc
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.is_link_local or address.is_reserved):
        raise FetchError("destination_denied", "DNS returned a non-public address")
    return address


def resolve_public(host: str, port: int, resolver: Callable = socket.getaddrinfo) -> tuple[str, ...]:
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("upstream_unavailable", "DNS resolution failed") from exc
    addresses: list[str] = []
    for answer in answers:
        sockaddr = answer[4]
        if not sockaddr:
            continue
        canonical = str(validate_public_ip(sockaddr[0]))
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise FetchError("upstream_unavailable", "DNS returned no usable addresses")
    return tuple(addresses)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class HTTPSPinnedTransport:
    def __init__(self, resolver: Callable = socket.getaddrinfo, context: ssl.SSLContext | None = None):
        self.resolver = resolver
        self.context = context or ssl.create_default_context()

    def request(self, target: Target, deadline: float, response_limit: int) -> UpstreamResponse:
        addresses = resolve_public(target.host, target.port, self.resolver)
        last_error: Exception | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("upstream_timeout")
            connection = PinnedHTTPSConnection(target.host, address, target.port, remaining, self.context)
            try:
                connection.request("GET", target.path, headers={
                    "Host": target.host,
                    "Accept": "text/html,text/plain,application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "AxonResearchProxy/1",
                    "Connection": "close",
                })
                response = connection.getresponse()
                headers = {key.lower(): value for key, value in response.getheaders()}
                encoding = headers.get("content-encoding", "identity").lower()
                if encoding not in ("", "identity"):
                    raise FetchError("response_too_large", "compressed responses are denied")
                length = headers.get("content-length")
                if length is not None:
                    try:
                        parsed_length = int(length)
                    except ValueError as exc:
                        raise FetchError("upstream_unavailable", "invalid Content-Length") from exc
                    if parsed_length < 0 or parsed_length > response_limit:
                        raise FetchError("response_too_large")
                body = response.read(response_limit + 1)
                if len(body) > response_limit:
                    raise FetchError("response_too_large")
                return UpstreamResponse(response.status, headers, body, address)
            except FetchError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()
        if time.monotonic() >= deadline:
            raise FetchError("upstream_timeout") from last_error
        raise FetchError("upstream_unavailable", "all validated addresses failed") from last_error


class SlidingWindowLimiter:
    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.clock = clock
        self._events: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        now = self.clock()
        with self._lock:
            self._events = [event for event in self._events if now - event < 60]
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True


class ResearchFetcher:
    def __init__(self, policy: ResearchPolicy, transport=None, clock=time.monotonic):
        self.policy = policy
        self.transport = transport or HTTPSPinnedTransport()
        self.clock = clock
        self._slots = threading.BoundedSemaphore(policy.limits["concurrency"])
        self._rate = SlidingWindowLimiter(policy.limits["requests_per_minute"], clock)

    def fetch(self, url: object) -> dict:
        if not self._rate.acquire():
            raise FetchError("rate_limited")
        if not self._slots.acquire(blocking=False):
            raise FetchError("rate_limited", "concurrency limit reached")
        started = self.clock()
        try:
            deadline = started + self.policy.limits["timeout_seconds"]
            target = parse_target(url, self.policy)
            requested = target.url
            for redirect_count in range(self.policy.limits["redirects"] + 1):
                response = self.transport.request(target, deadline, self.policy.limits["response_bytes"])
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("upstream_unavailable", "redirect has no Location")
                    if redirect_count >= self.policy.limits["redirects"]:
                        raise FetchError("destination_denied", "redirect limit exceeded")
                    target = parse_target(urllib.parse.urljoin(target.url, location), self.policy)
                    continue
                if not 200 <= response.status < 300:
                    raise FetchError("upstream_unavailable", f"upstream status {response.status}")
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in self.policy.allowed_content_types:
                    raise FetchError("destination_denied", "response media type is denied")
                if response.body.lstrip().startswith(b"%PDF-"):
                    raise FetchError("destination_denied", "PDF content is denied in policy v1")
                charset = "utf-8"
                message = Message()
                message["content-type"] = response.headers.get("content-type", media_type)
                candidate = message.get_content_charset()
                if candidate:
                    charset = candidate
                try:
                    text = response.body.decode(charset, errors="replace")
                except LookupError as exc:
                    raise FetchError("upstream_unavailable", "unknown response charset") from exc
                return {
                    "requested_url": requested,
                    "final_url": target.url,
                    "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": response.status,
                    "media_type": media_type,
                    "response_bytes": len(response.body),
                    "redirects": redirect_count,
                    "policy_id": self.policy.policy_id,
                    "policy_hash": self.policy.policy_hash,
                    "untrusted_content": True,
                    "content": text,
                }
            raise FetchError("destination_denied", "redirect limit exceeded")
        finally:
            self._slots.release()


class SafeAudit:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("research_proxy.audit")

    def record(self, *, policy: ResearchPolicy, url: object, outcome: str,
               status: int | None, response_bytes: int, latency_ms: int) -> None:
        host = "invalid"
        if isinstance(url, str):
            try:
                host = canonical_host(urllib.parse.urlsplit(url).hostname or "")
            except (PolicyError, ValueError):
                pass
        url_hash = hashlib.sha256(str(url).encode("utf-8", errors="replace")).hexdigest()[:16]
        self.logger.info(json.dumps({
            "event": "research_fetch", "policy_id": policy.policy_id,
            "policy_hash": policy.policy_hash, "destination_host": host,
            "url_hash": url_hash, "outcome": outcome, "status": status,
            "response_bytes": response_bytes, "latency_ms": latency_ms,
        }, sort_keys=True, separators=(",", ":")))


MAX_REQUEST_LINE = 2048
MAX_HEADER_LINE = 4096
MAX_HEADER_BYTES = 16384
MAX_HEADER_COUNT = 32


class ProxyHandler(socketserver.StreamRequestHandler):
    server: "ResearchProxyServer"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
                  411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
                  422: "Unprocessable Content", 429: "Too Many Requests", 502: "Bad Gateway"}.get(status, "Error")
        self.wfile.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )

    def handle(self) -> None:
        self.request.settimeout(self.server.policy.limits["timeout_seconds"])
        started = time.monotonic()
        url: object = None
        outcome = "malformed_request"
        status_code: int | None = None
        response_bytes = 0
        try:
            request_line = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if not request_line or len(request_line) > MAX_REQUEST_LINE or not request_line.endswith(b"\r\n"):
                raise FetchError("malformed_request", "invalid request line")
            try:
                method, path, version = request_line[:-2].decode("ascii").split(" ")
            except (UnicodeDecodeError, ValueError) as exc:
                raise FetchError("malformed_request", "invalid request line") from exc
            if method != "POST":
                self._reply(405, {"error": "operation_denied"})
                return
            if path != "/v1/fetch":
                self._reply(404, {"error": "operation_denied"})
                return
            if version != "HTTP/1.1":
                raise FetchError("malformed_request", "HTTP/1.1 required")

            headers: dict[str, str] = {}
            total = 0
            for _ in range(MAX_HEADER_COUNT + 1):
                line = self.rfile.readline(MAX_HEADER_LINE + 1)
                total += len(line)
                if len(line) > MAX_HEADER_LINE or total > MAX_HEADER_BYTES:
                    raise FetchError("request_too_large", "headers exceed limit")
                if line == b"\r\n":
                    break
                if not line or not line.endswith(b"\r\n") or line[:1] in b" \t" or b":" not in line:
                    raise FetchError("malformed_request", "invalid header")
                raw_name, raw_value = line[:-2].split(b":", 1)
                try:
                    name = raw_name.decode("ascii").lower()
                    value = raw_value.decode("ascii").strip()
                except UnicodeDecodeError as exc:
                    raise FetchError("malformed_request", "non-ASCII header") from exc
                if not name or not all(char.isalnum() or char == "-" for char in name) or name in headers:
                    raise FetchError("malformed_request", "invalid/duplicate header")
                headers[name] = value
            else:
                raise FetchError("request_too_large", "too many headers")
            if "transfer-encoding" in headers:
                raise FetchError("malformed_request", "Transfer-Encoding is forbidden")
            if "content-length" not in headers:
                self._reply(411, {"error": "malformed_request"})
                return
            try:
                length = int(headers["content-length"])
            except ValueError as exc:
                raise FetchError("malformed_request", "invalid Content-Length") from exc
            if length < 0:
                raise FetchError("malformed_request", "negative Content-Length")
            if length > self.server.policy.limits["request_bytes"]:
                raise FetchError("request_too_large")
            if headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                self._reply(415, {"error": "malformed_request"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise FetchError("malformed_request", "truncated body")
            try:
                document = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FetchError("malformed_request", "invalid JSON") from exc
            if not isinstance(document, dict) or set(document) != {"url"}:
                raise FetchError("malformed_request", "body must contain only url")
            url = document["url"]
            result = self.server.fetcher.fetch(url)
            outcome = "ok"
            status_code = result["status"]
            response_bytes = result["response_bytes"]
            self._reply(200, result)
        except (FetchError, socket.timeout) as exc:
            if isinstance(exc, socket.timeout):
                exc = FetchError("upstream_timeout")
            outcome = exc.reason
            http_status = 429 if exc.reason == "rate_limited" else 413 if exc.reason in {
                "request_too_large", "response_too_large"} else 422 if exc.reason in {
                "destination_denied", "operation_denied"} else 400 if exc.reason == "malformed_request" else 502
            self._reply(http_status, {"error": exc.reason})
        except Exception:
            outcome = "internal_error"
            logging.getLogger("research_proxy").exception("request failed")
            self._reply(502, {"error": "upstream_unavailable"})
        finally:
            self.server.audit.record(
                policy=self.server.policy, url=url, outcome=outcome, status=status_code,
                response_bytes=response_bytes,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )


class ResearchProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, policy: ResearchPolicy, fetcher: ResearchFetcher | None = None,
                 audit: SafeAudit | None = None):
        self.policy = policy
        self.fetcher = fetcher or ResearchFetcher(policy)
        self.audit = audit or SafeAudit()
        super().__init__(address, ProxyHandler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        policy = load_policy(os.environ.get("AXON_RESEARCH_POLICY", "/etc/axon-instance/sandbox-policy.json"))
    except PolicyError as exc:
        raise SystemExit(f"policy_invalid: {exc}")
    host = os.environ.get("AXON_RESEARCH_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("AXON_RESEARCH_LISTEN_PORT", "8787"))
    with ResearchProxyServer((host, port), policy) as server:
        logging.info(json.dumps({"event": "proxy_ready", "policy_id": policy.policy_id,
                                 "policy_hash": policy.policy_hash, "port": port}, sort_keys=True))
        server.serve_forever()


if __name__ == "__main__":
    main()
