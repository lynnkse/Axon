#!/usr/bin/env python3
from __future__ import annotations
import json, os, socket, socketserver, threading

PORTS = {8443: "claude", 8444: "supabase", 8445: "telegram", 8446: "groq", 8448: "codex"}
GATE = os.environ.get("AXON_SERVICE_GATE", "172.31.31.20")

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        service = self.server.service
        with socket.create_connection((GATE, 9443), 10) as upstream:
            upstream.sendall(json.dumps({"service": service}).encode() + b"\n")
            response = upstream.makefile("rb").readline(4096)
        self.wfile.write(response)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET6
    def __init__(self, port, service):
        self.service = service
        super().__init__(("::", port), Handler)

servers = [Server(port, service) for port, service in PORTS.items()]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({"event":"broker_ready","ports":PORTS}), flush=True)
threading.Event().wait()
