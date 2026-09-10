from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class PolicyError(ValueError):
    pass


_POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/json"})
_TOP_KEYS = frozenset({"schema_version", "policy_id", "allowed_hosts", "allowed_content_types", "limits"})
_LIMIT_KEYS = frozenset({
    "request_bytes", "response_bytes", "redirects", "timeout_seconds",
    "concurrency", "requests_per_minute",
})
_LIMIT_BOUNDS = {
    "request_bytes": (256, 8192),
    "response_bytes": (1024, 5 * 1024 * 1024),
    "redirects": (0, 3),
    "timeout_seconds": (1, 15),
    "concurrency": (1, 8),
    "requests_per_minute": (1, 120),
}


def canonical_host(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise PolicyError("host must be a non-empty string of at most 253 characters")
    if value != value.strip() or any(ord(char) < 33 for char in value):
        raise PolicyError("host contains whitespace or control characters")
    host = value[:-1] if value.endswith(".") else value
    if not host or host.startswith(".") or host.endswith(".") or ".." in host:
        raise PolicyError("host is not canonical")
    try:
        result = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PolicyError("host is not valid IDNA") from exc
    if len(result) > 253:
        raise PolicyError("canonical host is too long")
    labels = result.split(".")
    if len(labels) < 2:
        raise PolicyError("host must be a fully qualified DNS name")
    for label in labels:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise PolicyError("host contains an invalid DNS label")
    try:
        ipaddress.ip_address(result)
    except ValueError:
        return result
    raise PolicyError("IP literals are forbidden")


def _exact_keys(obj: Mapping[str, Any], expected: frozenset[str], where: str) -> None:
    actual = set(obj)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise PolicyError(f"{where} keys invalid; missing={missing}, unknown={unknown}")


@dataclass(frozen=True)
class ResearchPolicy:
    schema_version: int
    policy_id: str
    allowed_hosts: frozenset[str]
    allowed_content_types: frozenset[str]
    limits: Mapping[str, int]
    policy_hash: str


def validate_policy(document: Any) -> ResearchPolicy:
    if not isinstance(document, dict):
        raise PolicyError("policy must be a JSON object")
    _exact_keys(document, _TOP_KEYS, "policy")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise PolicyError("schema_version must equal 1")
    policy_id = document["policy_id"]
    if not isinstance(policy_id, str) or not _POLICY_ID.fullmatch(policy_id):
        raise PolicyError("policy_id is invalid")

    hosts = document["allowed_hosts"]
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 64:
        raise PolicyError("allowed_hosts must contain 1..64 hosts")
    canonical_hosts = [canonical_host(host) for host in hosts]
    if len(set(canonical_hosts)) != len(canonical_hosts):
        raise PolicyError("allowed_hosts contains duplicates after canonicalization")

    content_types = document["allowed_content_types"]
    if not isinstance(content_types, list) or not 1 <= len(content_types) <= 3:
        raise PolicyError("allowed_content_types must contain 1..3 values")
    if any(not isinstance(item, str) or item not in _CONTENT_TYPES for item in content_types):
        raise PolicyError("allowed_content_types contains an unsupported value")
    if len(set(content_types)) != len(content_types):
        raise PolicyError("allowed_content_types contains duplicates")

    limits = document["limits"]
    if not isinstance(limits, dict):
        raise PolicyError("limits must be an object")
    _exact_keys(limits, _LIMIT_KEYS, "limits")
    checked_limits: dict[str, int] = {}
    for key, (minimum, maximum) in _LIMIT_BOUNDS.items():
        value = limits[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise PolicyError(f"{key} must be an integer in [{minimum}, {maximum}]")
        checked_limits[key] = value

    canonical_document = {
        "schema_version": 1,
        "policy_id": policy_id,
        "allowed_hosts": sorted(canonical_hosts),
        "allowed_content_types": sorted(content_types),
        "limits": {key: checked_limits[key] for key in sorted(checked_limits)},
    }
    encoded = json.dumps(canonical_document, sort_keys=True, separators=(",", ":")).encode()
    return ResearchPolicy(
        schema_version=1,
        policy_id=policy_id,
        allowed_hosts=frozenset(canonical_hosts),
        allowed_content_types=frozenset(content_types),
        limits=MappingProxyType(checked_limits),
        policy_hash=hashlib.sha256(encoded).hexdigest(),
    )


def load_policy(path: str | Path) -> ResearchPolicy:
    policy_path = Path(path)
    try:
        raw = policy_path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read policy: {exc}") from exc
    if len(raw) > 64 * 1024:
        raise PolicyError("policy file exceeds 64 KiB")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError("policy is not valid UTF-8 JSON") from exc
    return validate_policy(document)

