#!/usr/bin/env python3
"""Small, credential-free endpoints and probes for topology verification only."""
from __future__ import annotations

import argparse
import os
import selectors
import socket
import sys
import json
import http.client
from pathlib import Path


def listeners(ports: list[int], unix_path: str | None) -> None:
    sel = selectors.DefaultSelector()
    sockets: list[socket.socket] = []
    for port in ports:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", port))
        sock.listen()
        sock.setblocking(False)
        sel.register(sock, selectors.EVENT_READ)
        sockets.append(sock)
    if unix_path:
        path = Path(unix_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen()
        sock.setblocking(False)
        sel.register(sock, selectors.EVENT_READ)
        sockets.append(sock)
    print(f"ready ports={ports} unix={unix_path or '-'}", flush=True)
    while True:
        for key, _ in sel.select():
            conn, _ = key.fileobj.accept()
            with conn:
                conn.sendall(b"axon-step6-ok\n")


def probe(host: str, port: int, expect: str, timeout: float) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            data = sock.recv(64)
        succeeded = data.startswith(b"axon-step6-ok") or port == 8787
        detail = data.decode("utf-8", "replace").strip()
    except OSError as exc:
        succeeded = False
        detail = f"{type(exc).__name__}:{exc}"
    wanted = expect == "allow"
    print(f"expect={expect} target={host}:{port} connected={str(succeeded).lower()} detail={detail}")
    if succeeded != wanted:
        raise SystemExit(1)


def fetch_probe(host: str) -> None:
    body = json.dumps({"url": "https://127.0.0.1/"})
    conn = http.client.HTTPConnection(host, 8787, timeout=2)
    conn.request("POST", "/v1/fetch", body, {
        "Content-Type": "application/json", "Content-Length": str(len(body))})
    response = conn.getresponse()
    payload = response.read().decode()
    print(f"research_proxy={host}:8787 status={response.status} body={payload}")
    if response.status != 422 or "destination_denied" not in payload:
        raise SystemExit(1)


def json_probe(host: str, port: int, expected_service: str) -> None:
    with socket.create_connection((host, port), timeout=15) as conn:
        payload = json.loads(conn.makefile("rb").readline(4096))
    print(json.dumps(payload, sort_keys=True))
    if payload.get("outcome") != "reachable" or payload.get("service") != expected_service:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--ports", required=True)
    serve.add_argument("--unix")
    check = sub.add_parser("probe")
    check.add_argument("host")
    check.add_argument("port", type=int)
    check.add_argument("--expect", choices=("allow", "deny"), required=True)
    check.add_argument("--timeout", type=float, default=1.0)
    fetch = sub.add_parser("fetch-probe")
    fetch.add_argument("host")
    json_check = sub.add_parser("json-probe")
    json_check.add_argument("host")
    json_check.add_argument("port", type=int)
    json_check.add_argument("service")
    args = parser.parse_args()
    if args.command == "serve":
        listeners([int(value) for value in args.ports.split(",")], args.unix)
    elif args.command == "probe":
        probe(args.host, args.port, args.expect, args.timeout)
    elif args.command == "fetch-probe":
        fetch_probe(args.host)
    else:
        json_probe(args.host, args.port, args.service)


if __name__ == "__main__":
    main()
