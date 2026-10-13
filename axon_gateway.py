#!/usr/bin/env python3
"""Local, capability-limited gateway into Axon's persistent main session.

The gateway deliberately exposes only bounded message delegation plus
status/result retrieval. It never accepts commands, filesystem paths, model
selection, credentials, or arbitrary process/worker operations.

Phase 1 is localhost-only HTTP. A caller must present AXON_GATEWAY_TOKEN.
Tasks and audit metadata are persisted in SQLite before delivery to Axon.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import config


LOG = logging.getLogger("axon.gateway")
# Every source below has an explicit non-Telegram branch in telegram_node.py.
# Do not accept an arbitrary source here: that would reintroduce the shared
# relay queue-corruption failure mode.
SOURCE = os.environ.get("AXON_GATEWAY_SOURCE", "chatgpt")
ALLOWED_SOURCES = frozenset({"chatgpt", "slack"})
DEFAULT_DB = Path(os.environ.get("AXON_GATEWAY_DB", "/tmp/axon/gateway.sqlite3"))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_MESSAGE_BYTES = 16 * 1024
MAX_IDEMPOTENCY_KEY_BYTES = 256
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 900
ALLOWED_TARGETS = frozenset({"axon-main"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_payload_hash(target: str, text: str) -> str:
    payload = json.dumps(
        {"target": target, "text": text}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class GatewayTask:
    task_id: str
    conversation_id: str
    target: str
    status: str
    created_at: str
    updated_at: str
    result: Optional[str] = None
    error: Optional[str] = None


class ConflictError(ValueError):
    """An idempotency key was reused with a different request payload."""


class TaskStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    target TEXT NOT NULL,
                    text TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    timeout_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS gateway_tasks_status_created
                    ON gateway_tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS gateway_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    event TEXT NOT NULL,
                    target TEXT,
                    caller_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS gateway_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT,
                    message TEXT,
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES gateway_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS gateway_events_task_event
                    ON gateway_events(task_id, event_id);
                """
            )
            # A process may die after submitting a request. Replaying it could
            # execute the same bounded request twice, so fail closed instead.
            conn.execute(
                """UPDATE gateway_tasks
                   SET status='failed', error='gateway interrupted while task was running',
                       updated_at=?
                   WHERE status='running'""",
                (utc_now(),),
            )
        # The task body may contain private conversation text. Keep the
        # durable store readable and writable only by the Axon account.
        os.chmod(self.db_path, 0o600)

    def record_event(
        self,
        task_id: str,
        *,
        kind: str,
        detail: str = "",
        message: str = "",
    ) -> None:
        """Persist only the session manager's already-public activity stream."""
        kind = str(kind)[:80]
        detail = str(detail)[:1000]
        message = str(message)[:4000]
        if not kind:
            return
        with self._lock, self._connect() as conn:
            # Adjacent duplicate tool/status events add noise without useful
            # information when a runner retries an internal progress update.
            previous = conn.execute(
                """SELECT kind, detail, message FROM gateway_events
                   WHERE task_id=? ORDER BY event_id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if previous is not None and (
                previous["kind"], previous["detail"], previous["message"]
            ) == (kind, detail, message):
                return
            conn.execute(
                """INSERT INTO gateway_events
                   (task_id, kind, detail, message, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (task_id, kind, detail, message, utc_now()),
            )

    def events_after(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event_id, kind, detail, message, occurred_at
                   FROM gateway_events WHERE task_id=? AND event_id>?
                   ORDER BY event_id LIMIT 100""",
                (task_id, after),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"], "kind": row["kind"],
                "detail": row["detail"] or "", "message": row["message"] or "",
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]

    def create_task(
        self,
        *,
        conversation_id: str,
        idempotency_key: str,
        target: str,
        text: str,
        timeout_seconds: int,
        caller_id: str,
    ) -> tuple[GatewayTask, bool]:
        payload_hash = canonical_payload_hash(target, text)
        now = utc_now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM gateway_tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("idempotency key already used for another payload")
                return self._public(existing), False

            task_id = uuid.uuid4().hex
            request_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO gateway_tasks
                   (task_id, conversation_id, idempotency_key, payload_hash,
                    target, text, request_id, timeout_seconds, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    task_id,
                    conversation_id,
                    idempotency_key,
                    payload_hash,
                    target,
                    text,
                    request_id,
                    timeout_seconds,
                    now,
                    now,
                ),
            )
            self._audit(conn, task_id, "created", target, caller_id)
            row = conn.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._public(row), True

    def claim_next(self, caller_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM gateway_tasks WHERE status='queued'
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            conn.execute(
                "UPDATE gateway_tasks SET status='running', updated_at=? WHERE task_id=?",
                (now, row["task_id"]),
            )
            self._audit(conn, row["task_id"], "started", row["target"], caller_id)
            return conn.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (row["task_id"],)
            ).fetchone()

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        caller_id: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT target FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            conn.execute(
                """UPDATE gateway_tasks
                   SET status=?, result=?, error=?, updated_at=?
                   WHERE task_id=?""",
                (status, result, error, utc_now(), task_id),
            )
            self._audit(conn, task_id, status, row["target"], caller_id, error)

    def get(self, task_id: str) -> Optional[GatewayTask]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._public(row) if row is not None else None

    @staticmethod
    def _public(row: sqlite3.Row) -> GatewayTask:
        return GatewayTask(
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            target=row["target"],
            status=row["status"],
            result=row["result"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        task_id: str,
        event: str,
        target: str,
        caller_id: str,
        detail: Optional[str] = None,
    ) -> None:
        conn.execute(
            """INSERT INTO gateway_audit
               (task_id, event, target, caller_id, occurred_at, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, event, target, caller_id, utc_now(), detail),
        )


class AxonSocketClient:
    def __init__(
        self,
        input_socket: str = config.USER_INPUT_SOCK,
        response_socket: str = config.CLAUDE_RESPONSE_SOCK,
        source: str = SOURCE,
    ):
        if source not in ALLOWED_SOURCES:
            raise ValueError("gateway source is not explicitly routed")
        self.input_socket = input_socket
        self.response_socket = response_socket
        self.source = source

    def execute(
        self,
        row: sqlite3.Row,
        on_activity: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> str:
        # Subscribe before submission so a fast response cannot be missed.
        response = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        response.settimeout(float(row["timeout_seconds"]))
        response.connect(self.response_socket)
        try:
            payload = {
                "text": row["text"],
                "source": self.source,
                "user_id": f"{self.source}-gateway",
                "request_id": row["request_id"],
            }
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
                sender.settimeout(5.0)
                sender.connect(self.input_socket)
                sender.sendall((json.dumps(payload) + "\n").encode("utf-8"))

            buffer = b""
            while True:
                chunk = response.recv(65536)
                if not chunk:
                    raise ConnectionError("Axon response socket closed")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    if message.get("type") == "activity":
                        if (
                            message.get("source") == self.source
                            and message.get("request_id") == row["request_id"]
                            and message.get("growing")
                            and on_activity is not None
                        ):
                            on_activity(message)
                        continue
                    if (
                        message.get("source") == self.source
                        and message.get("request_id") == row["request_id"]
                    ):
                        return str(message.get("text", ""))
        finally:
            response.close()


class GatewayService:
    def __init__(self, store: TaskStore, client: AxonSocketClient):
        self.store = store
        self.client = client
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="axon-gateway-worker", daemon=True)

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._worker.join(timeout=2)

    def notify(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            row = self.store.claim_next("gateway-worker")
            if row is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            self.store.record_event(
                row["task_id"], kind="running", detail="Axon started this request"
            )
            try:
                result = self.client.execute(
                    row,
                    on_activity=lambda activity: self.store.record_event(
                        row["task_id"],
                        kind=str(activity.get("kind", "progress")),
                        detail=str(activity.get("detail", "")),
                        message=str(activity.get("message", "")),
                    ),
                )
            except socket.timeout:
                self.store.finish(
                    row["task_id"],
                    status="timed_out",
                    error="Axon did not return a final response before the task timeout",
                    caller_id="gateway-worker",
                )
            except Exception as exc:
                LOG.exception("Gateway task %s failed", row["task_id"])
                self.store.finish(
                    row["task_id"],
                    status="failed",
                    error=f"Axon transport failed: {type(exc).__name__}",
                    caller_id="gateway-worker",
                )
            else:
                self.store.finish(
                    row["task_id"],
                    status="succeeded",
                    result=result,
                    caller_id="gateway-worker",
                )


class GatewayHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *, service: GatewayService, token: str, caller_id: str):
        super().__init__(address, handler)
        self.service = service
        self.token = token
        self.caller_id = caller_id


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        return hmac.compare_digest(value, expected)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if urlparse(self.path).path != "/v1/tasks":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 1 or content_length > MAX_MESSAGE_BYTES + 4096:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"})
            return
        try:
            body = json.loads(self.rfile.read(content_length))
            conversation_id = str(body["conversation_id"]).strip()
            idempotency_key = str(body["idempotency_key"]).strip()
            target = str(body.get("target", "axon-main")).strip()
            text = str(body["text"]).strip()
            timeout_seconds = int(body.get("timeout_seconds", 600))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})
            return

        if not conversation_id or len(conversation_id.encode()) > 256:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid conversation_id"})
            return
        if not idempotency_key or len(idempotency_key.encode()) > MAX_IDEMPOTENCY_KEY_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid idempotency_key"})
            return
        if target not in ALLOWED_TARGETS:
            self._json(HTTPStatus.FORBIDDEN, {"error": "target is not authorized"})
            return
        if not text or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid message size"})
            return
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid timeout"})
            return

        try:
            task, created = self.server.service.store.create_task(
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                target=target,
                text=text,
                timeout_seconds=timeout_seconds,
                caller_id=self.server.caller_id,
            )
        except ConflictError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        if created:
            self.server.service.store.record_event(
                task.task_id, kind="accepted", detail="Queued for Axon"
            )
            self.server.service.notify()
        self._json(HTTPStatus.ACCEPTED if created else HTTPStatus.OK, asdict(task))

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) not in {3, 4} or parts[:2] != ["v1", "tasks"]:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        task = self.server.service.store.get(parts[2])
        if task is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
            return
        body = asdict(task)
        if len(parts) == 4:
            if parts[3] == "events":
                try:
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid event cursor"})
                    return
                if after < 0:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid event cursor"})
                    return
                events = self.server.service.store.events_after(task.task_id, after)
                self._json(HTTPStatus.OK, {
                    "task_id": task.task_id,
                    "events": events,
                    "next_after": events[-1]["event_id"] if events else after,
                    "status": task.status,
                })
                return
            if parts[3] != "result":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            body = {
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result if task.status == "succeeded" else None,
                "error": task.error,
            }
        self._json(HTTPStatus.OK, body)


def build_server(host: str, port: int, token: str, db_path: Path, caller_id: str, source: str = SOURCE) -> tuple[GatewayHTTPServer, GatewayService]:
    store = TaskStore(db_path)
    service = GatewayService(store, AxonSocketClient(source=source))
    server = GatewayHTTPServer(
        (host, port), GatewayHandler, service=service, token=token, caller_id=caller_id
    )
    return server, service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Phase 1 gateway is localhost-only")
    token = os.environ.get("AXON_GATEWAY_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("AXON_GATEWAY_TOKEN must contain at least 32 characters")
    caller_id = os.environ.get("AXON_GATEWAY_CALLER_ID", "chatgpt-local")
    if SOURCE not in ALLOWED_SOURCES:
        raise SystemExit("AXON_GATEWAY_SOURCE is not explicitly routed")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, service = build_server(args.host, args.port, token, args.db, caller_id)
    service.start()
    LOG.info("Axon gateway listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.stop()


if __name__ == "__main__":
    main()
