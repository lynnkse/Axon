#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import socketserver
import ssl
import struct
import secrets
import time
from pathlib import Path

MAX_REQUEST = 2048
KNOWN = frozenset(("claude", "codex", "supabase", "telegram", "groq"))


class Denied(Exception):
    pass


def load_manifest(path: str) -> tuple[str, dict[str, dict[str, str]]]:
    raw = json.loads(Path(path).read_text())
    if set(raw) != {"schema_version", "policy_id", "services"} or raw["schema_version"] != 1:
        raise Denied("policy_invalid")
    if not isinstance(raw["policy_id"], str) or not raw["policy_id"]:
        raise Denied("policy_invalid")
    services = raw["services"]
    if not isinstance(services, dict) or set(services) != KNOWN:
        raise Denied("policy_invalid")
    for service, target in services.items():
        if set(target) != {"host", "path"} or not isinstance(target["host"], str) or not isinstance(target["path"], str):
            raise Denied("policy_invalid")
        host = target["host"].lower().rstrip(".")
        if host != target["host"] or "." not in host or target["path"].startswith("//"):
            raise Denied("policy_invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise Denied("policy_invalid")
        if not target["path"].startswith("/"):
            raise Denied("policy_invalid")
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest(), services


def _dns_name(host: str) -> bytes:
    return b"".join(bytes((len(label),)) + label.encode("ascii") for label in host.split(".")) + b"\0"


def _skip_name(packet: bytes, offset: int) -> int:
    for _ in range(128):
        if offset >= len(packet): raise Denied("upstream_unavailable")
        size = packet[offset]
        if size & 0xC0 == 0xC0:
            if offset + 2 > len(packet): raise Denied("upstream_unavailable")
            return offset + 2
        offset += 1
        if size == 0: return offset
        if size > 63 or offset + size > len(packet): raise Denied("upstream_unavailable")
        offset += size
    raise Denied("upstream_unavailable")


def dns_query(host: str, qtype: int, resolver: str) -> tuple[str, ...]:
    txid = secrets.randbits(16)
    question = _dns_name(host) + struct.pack("!HH", qtype, 1)
    request = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0) + question
    family = socket.AF_INET6 if ":" in resolver else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(request, (resolver, 53))
        packet, peer = sock.recvfrom(4096)
    if peer[0] != resolver or len(packet) < 12:
        raise Denied("upstream_unavailable")
    rid, flags, qd, an, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if rid != txid or flags & 0x8000 == 0 or flags & 0x0200 or flags & 0x000F or qd != 1 or an > 64:
        raise Denied("upstream_unavailable")
    offset = _skip_name(packet, 12)
    if offset + 4 > len(packet): raise Denied("upstream_unavailable")
    offset += 4
    found: list[str] = []
    for _ in range(an):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet): raise Denied("upstream_unavailable")
        kind, klass, _, size = struct.unpack("!HHIH", packet[offset:offset + 10]); offset += 10
        if offset + size > len(packet): raise Denied("upstream_unavailable")
        data = packet[offset:offset + size]; offset += size
        if klass == 1 and kind == qtype and size == (4 if qtype == 1 else 16):
            found.append(socket.inet_ntop(socket.AF_INET if qtype == 1 else socket.AF_INET6, data))
    return tuple(found)


def public_addresses(host: str) -> tuple[str, ...]:
    resolver = os.environ.get("AXON_CONTROLLED_DNS", "192.168.1.1")
    answers = dns_query(host, 1, resolver) + dns_query(host, 28, resolver)
    found: list[str] = []
    for value in answers:
        ip = ipaddress.ip_address(value)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_loopback:
            raise Denied("destination_denied")
        canonical = str(ip)
        if canonical not in found:
            found.append(canonical)
    if not found:
        raise Denied("upstream_unavailable")
    return tuple(found)


class Pinned(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, 443, timeout=8, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def probe(service: str, services: dict[str, dict[str, str]]) -> dict:
    if service not in services:
        raise Denied("service_disabled")
    target = services[service]
    last: Exception | None = None
    for address in public_addresses(target["host"]):
        conn = Pinned(target["host"], address)
        try:
            conn.request("HEAD", target["path"], headers={"Host": target["host"], "User-Agent": "AxonServiceGate/1", "Connection": "close"})
            response = conn.getresponse()
            response.read(1024)
            return {"outcome": "reachable", "service": service, "status": response.status,
                    "address_family": "ipv6" if ":" in address else "ipv4"}
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last = exc
        finally:
            conn.close()
    raise Denied("upstream_unavailable") from last


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        line = self.rfile.readline(MAX_REQUEST + 1)
        outcome = "malformed_request"
        service = "invalid"
        status = 400
        try:
            if not line or len(line) > MAX_REQUEST:
                raise Denied("request_too_large")
            raw = json.loads(line)
            if not isinstance(raw, dict) or set(raw) != {"service"} or not isinstance(raw["service"], str):
                raise Denied("malformed_request")
            service = raw["service"] if raw["service"] in KNOWN else "unknown"
            result = probe(raw["service"], self.server.services)
            outcome, status = "reachable", 200
            payload = result
        except Denied as exc:
            outcome = str(exc)
            status = 403 if outcome in ("service_disabled", "destination_denied") else 422
            payload = {"outcome": outcome}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"outcome": outcome}
        except Exception:
            outcome, status, payload = "upstream_unavailable", 502, {"outcome": "upstream_unavailable"}
        payload["code"] = status
        self.wfile.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        print(json.dumps({"event": "service_gate", "service": service, "outcome": outcome,
                          "latency_ms": int((time.monotonic() - started) * 1000),
                          "manifest_hash": self.server.manifest_hash}, separators=(",", ":")), flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET6
    def __init__(self, address, manifest_hash, services):
        self.manifest_hash, self.services = manifest_hash, services
        super().__init__(address, Handler)


def main() -> None:
    try:
        manifest_hash, services = load_manifest(os.environ.get("AXON_SERVICE_MANIFEST", "/etc/axon-service/manifest.json"))
    except Exception as exc:
        raise SystemExit(f"policy_invalid: {exc}")
    server = Server(("::", 9443), manifest_hash, services)
    print(json.dumps({"event": "service_gate_ready", "manifest_hash": manifest_hash}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
