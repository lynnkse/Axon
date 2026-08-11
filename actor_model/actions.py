from __future__ import annotations


CONSEQUENTIAL_KINDS = {"write_file", "send_message", "external_api_write", "execute_command", "delete"}


def authorized(action: dict, authorization_events: list[dict]) -> bool:
    if action.get("kind") not in CONSEQUENTIAL_KINDS:
        return True
    action_id = action.get("action_id")
    return any(e.get("event_type") == "action_authorized" and
               e.get("payload", {}).get("action_id") == action_id for e in authorization_events)


def enforce_authorization(actions: list[dict], authorization_events: list[dict]) -> tuple[list[dict], list[dict]]:
    allowed, proposed = [], []
    for action in actions:
        (allowed if authorized(action, authorization_events) else proposed).append(action)
    return allowed, proposed
