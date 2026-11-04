"""Low-overhead, aggregate-only dashboard traffic telemetry."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


class TrafficTelemetry:
    def __init__(self, logs_dir: Path):
        self.logs_dir = Path(logs_dir)
        self.events_path = self.logs_dir / "traffic_events.jsonl"
        self._runs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _stamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    def _write(self, event: dict[str, Any]) -> None:
        """Append metadata only; never include query values, rows, or credentials."""
        event["at"] = self._stamp()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with _LOCK:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def start_run(self, session_id: str, section: str) -> str:
        run_id = uuid.uuid4().hex
        self._runs[run_id] = {"session": session_id, "section": section, "queries": []}
        self._write({"event": "dashboard_run_start", "run": run_id, "session": session_id, "section": section})
        return run_id

    def record_network(self, table: str, params: str, response_bytes: int, duration_ms: int, ok: bool) -> None:
        self._write({
            "event": "supabase_network_request", "table": table,
            "query": self._digest(params), "response_bytes": int(response_bytes),
            "duration_ms": int(duration_ms), "ok": bool(ok),
        })

    def record_query(self, run_id: str, table: str, params: str, rows: Any) -> None:
        fingerprint = self._digest(rows)
        run = self._runs.get(run_id)
        if run is not None:
            run["queries"].append((table, fingerprint))
        self._write({
            "event": "dashboard_query", "run": run_id, "table": table,
            "query": self._digest(params), "rows": len(rows) if isinstance(rows, list) else 0,
            "result": fingerprint,
        })

    def record_inline_asset(self, run_id: str, kind: str, name: str, payload_bytes: int) -> None:
        self._write({
            "event": "dashboard_inline_asset", "run": run_id, "kind": kind,
            "name": Path(name).name, "estimated_payload_bytes": int(payload_bytes),
        })

    def finish_run(self, run_id: str) -> None:
        run = self._runs.pop(run_id, None)
        if run is None:
            return
        fingerprint = self._digest(run["queries"])
        self._write({
            "event": "dashboard_run_end", "run": run_id, "session": run["session"],
            "section": run["section"], "fingerprint": fingerprint,
            "queries": len(run["queries"]),
        })


def read_status(logs_dir: Path) -> dict[str, Any]:
    path = Path(logs_dir) / "traffic_status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
