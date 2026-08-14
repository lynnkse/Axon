"""Prompt-embedded actor blocks: bounded rendering, strict parsing, and validation.

Actors advance only inside an existing conversational turn. There is no runtime,
timer, poller, scheduler, worker, or additional model call in this module.
"""
from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

BEGIN_INPUT = "<<<AXON_ACTOR_INPUT>>>"
END_INPUT = "<<<END_AXON_ACTOR_INPUT>>>"
BEGIN_UPDATE = "<<<AXON_ACTOR_UPDATE>>>"
END_UPDATE = "<<<END_AXON_ACTOR_UPDATE>>>"

# Prompt-growth policy: retain the newest eight history entries, truncate any
# individual string to 2,000 characters, lists to 32 items, mappings to 64 keys,
# and nesting to six levels. This bounds old history without fixing actor count.
HISTORY_ENTRIES = 8
MAX_STRING_CHARS = 2_000
MAX_LIST_ITEMS = 32
MAX_DICT_KEYS = 64
MAX_DEPTH = 6
MAX_UPDATE_JSON_CHARS = 50_000
MAX_SUMMARY_CHARS = 1_000
MAX_ERROR_CHARS = 2_000
ACTOR_DORMANCY_SECONDS = 24 * 60 * 60
CODE_HASH_TURN_WINDOW = 16
CODE_STATE_KEYS = ("role", "instructions", "methodology_reference")

_UPDATE_RE = re.compile(
    re.escape(BEGIN_UPDATE) + r"\s*(.*?)\s*" + re.escape(END_UPDATE),
    re.DOTALL,
)
VALID_STATUSES = {"running", "finished", "error"}


class ActorBlockError(ValueError):
    pass


@dataclass(frozen=True)
class ActorUpdate:
    actor_id: str
    status: str
    state: dict[str, Any]
    summary: str
    error_reason: str | None = None


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth >= MAX_DEPTH:
        return "[depth limit]"
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_CHARS else value[:MAX_STRING_CHARS] + "…[truncated]"
    if isinstance(value, list):
        return [_bounded(v, depth + 1) for v in value[-MAX_LIST_ITEMS:]]
    if isinstance(value, dict):
        return {str(k): _bounded(v, depth + 1) for k, v in list(value.items())[:MAX_DICT_KEYS]}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded(str(value), depth + 1)


def active_actor_rows(rows: list[dict], max_slots: int | None = None) -> list[dict]:
    """Select non-terminal actors by Linux nice priority, optionally capped."""
    terminal = {"completed", "finished", "blocked", "error"}
    eligible = sorted(
        (row for row in rows if str(row.get("disposition", "")).lower() not in terminal),
        key=lambda row: (int(row.get("nice", 0) or 0), str(row.get("actor_id", ""))),
    )
    return eligible if max_slots is None else eligible[:max(0, max_slots)]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def prompt_actor_rows(
    rows: list[dict], relevance_check, max_slots: int | None = None,
    now: datetime | None = None, dormancy_seconds: int = ACTOR_DORMANCY_SECONDS,
) -> list[dict]:
    """Gate low-priority actors unless dirty, newly relevant, or dormant.

    Unknown actor types and relevance-check failures fail open: correctness wins
    over token savings. Nice <= 0 actors always run.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = []
    for row in active_actor_rows(rows):
        last_advanced = _parse_timestamp(row.get("last_advanced_at"))
        must_run = (
            int(row.get("nice", 0) or 0) <= 0
            or bool(row.get("dirty"))
            or last_advanced is None
            or (now - last_advanced).total_seconds() >= dormancy_seconds
        )
        if not must_run:
            changed = relevance_check(row)
            must_run = changed is not False
        if must_run:
            selected.append(row)
    return selected if max_slots is None else selected[:max(0, max_slots)]


def split_actor_state(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the slow-changing code segment and dynamic data segment."""
    code = {key: state[key] for key in CODE_STATE_KEYS if key in state}
    data = {key: value for key, value in state.items()
            if key not in CODE_STATE_KEYS and key != "history"}
    return code, data


def render_actor_inputs(
    rows: list[dict], sent_code_hashes: dict[str, int] | None = None,
    current_turn: int = 0, code_hash_turn_window: int = CODE_HASH_TURN_WINDOW,
) -> str:
    blocks = []
    seen: set[str] = set()
    for row in active_actor_rows(rows):
        actor_id = row.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ActorBlockError(f"invalid actor input actor_id: {actor_id!r}")
        if actor_id in seen:
            raise ActorBlockError(f"duplicate actor input actor_id: {actor_id}")
        if not isinstance(row.get("state") or {}, dict):
            raise ActorBlockError(f"actor input {actor_id} state must be an object")
        seen.add(actor_id)
        state = dict(row.get("state") or {})
        history = state.get("history", [])
        code, data = split_actor_state(state)
        payload = {
            "actor_id": actor_id,
            "actor_type": row.get("actor_type"),
            "revision": row.get("revision", 0),
            "status": "running",
            "state": _bounded(data),
            "recent_history": _bounded(list(history)[-HISTORY_ENTRIES:]),
        }
        if code:
            encoded_code = json.dumps(code, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()
            code_hash = hashlib.sha256(encoded_code).hexdigest()[:16]
            last_sent = sent_code_hashes.get(code_hash) if sent_code_hashes is not None else None
            if last_sent is not None and current_turn - last_sent <= code_hash_turn_window:
                payload["code_ref"] = {
                    "role_hash": code_hash,
                    "note": "unchanged; already shown above",
                }
            else:
                payload["code"] = _bounded(code)
                payload["role_hash"] = code_hash
                if sent_code_hashes is not None:
                    sent_code_hashes[code_hash] = current_turn
        blocks.append(f"{BEGIN_INPUT}\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n{END_INPUT}")
    if not blocks:
        return ""
    return (
        "[ACTIVE ACTORS — update every block during this same response]\n"
        + "\n".join(blocks)
    )


def output_instructions() -> str:
    return f"""
PROMPT-EMBEDDED ACTORS: When the current user turn contains {BEGIN_INPUT} blocks,
advance every supplied actor as part of this same response. After the normal reply,
emit exactly one update per input actor using this exact delimiter and JSON shape:
{BEGIN_UPDATE}
{{"actor_id":"exact input actor_id","status":"running|finished|error","state":{{}},"summary":"short current summary","error_reason":null}}
{END_UPDATE}
The input state is dynamic data only. Static code/role fields are preserved by the
runtime; do not echo them into state. Use running when more work remains, finished
when no more work is needed, and error
only for a broken state (include a non-empty error_reason). Preserve useful state;
do not emit markdown fences around the JSON. Never create an actor not present in
the input and never omit an input actor.
""".strip()


def parse_actor_updates(text: str, expected_actor_ids: set[str]) -> list[ActorUpdate]:
    begin_count, end_count = text.count(BEGIN_UPDATE), text.count(END_UPDATE)
    if begin_count != end_count:
        raise ActorBlockError(
            f"unbalanced actor delimiters: begin={begin_count}, end={end_count}")
    raw_blocks = _UPDATE_RE.findall(text)
    if len(raw_blocks) != begin_count:
        raise ActorBlockError(
            f"actor delimiter extraction mismatch: markers={begin_count}, blocks={len(raw_blocks)}")

    updates: list[ActorUpdate] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_blocks, 1):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ActorBlockError(f"actor block {index} contains invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ActorBlockError(f"actor block {index} must be a JSON object")
        if len(raw) > MAX_UPDATE_JSON_CHARS:
            raise ActorBlockError(
                f"actor block {index} exceeds {MAX_UPDATE_JSON_CHARS} characters")
        actor_id = data.get("actor_id")
        status = data.get("status")
        state = data.get("state")
        summary = data.get("summary")
        error_reason = data.get("error_reason")
        if not isinstance(actor_id, str) or actor_id not in expected_actor_ids:
            raise ActorBlockError(f"actor block {index} has unknown actor_id {actor_id!r}")
        if actor_id in seen:
            raise ActorBlockError(f"duplicate actor update for {actor_id}")
        if status not in VALID_STATUSES:
            raise ActorBlockError(f"actor {actor_id} has invalid status {status!r}")
        if not isinstance(state, dict):
            raise ActorBlockError(f"actor {actor_id} state must be an object")
        if not isinstance(summary, str) or not summary.strip():
            raise ActorBlockError(f"actor {actor_id} summary must be non-empty")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ActorBlockError(f"actor {actor_id} summary exceeds {MAX_SUMMARY_CHARS} characters")
        if status == "error" and (not isinstance(error_reason, str) or not error_reason.strip()):
            raise ActorBlockError(f"actor {actor_id} error status requires error_reason")
        if error_reason is not None and not isinstance(error_reason, str):
            raise ActorBlockError(f"actor {actor_id} error_reason must be a string or null")
        if isinstance(error_reason, str) and len(error_reason) > MAX_ERROR_CHARS:
            raise ActorBlockError(f"actor {actor_id} error_reason exceeds {MAX_ERROR_CHARS} characters")
        seen.add(actor_id)
        updates.append(ActorUpdate(actor_id, status, state, summary.strip(), error_reason))

    missing = expected_actor_ids - seen
    if missing:
        raise ActorBlockError(f"missing actor updates: {', '.join(sorted(missing))}")
    if not expected_actor_ids and updates:
        raise ActorBlockError("received actor updates when no actors were supplied")
    return updates


def strip_actor_blocks(text: str) -> str:
    """Remove valid or malformed actor protocol material from the user reply."""
    text = _UPDATE_RE.sub("", text)
    if BEGIN_UPDATE in text:
        text = text.split(BEGIN_UPDATE, 1)[0]
    elif END_UPDATE in text:
        before_end = text.rsplit(END_UPDATE, 1)[0]
        json_start = before_end.rfind("\n{")
        text = before_end[:json_start] if json_start >= 0 else before_end
    return text.replace(END_UPDATE, "").strip()
