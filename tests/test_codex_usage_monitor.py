from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import codex_usage_monitor as monitor


def actor(state=None):
    return {
        "actor_id": monitor.ACTOR_ID,
        "revision": 3,
        "state": {"role": "static", **(state or {})},
    }


def test_not_due_does_not_read_usage_or_write():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    row = actor({"last_check_at": (now - timedelta(minutes=59)).isoformat()})
    with patch.object(monitor, "_actor_row", return_value=row), \
         patch.object(monitor.supabase_client, "latest_codex_weekly_usage") as usage, \
         patch.object(monitor.supabase_client, "save_prompt_actor_update") as save:
        assert monitor.check_once(now=now) == "not-due"
    usage.assert_not_called()
    save.assert_not_called()


def test_below_threshold_checks_without_alert():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    row = actor({"threshold_percent": 85})
    sent = []
    with patch.object(monitor, "_actor_row", return_value=row), \
         patch.object(monitor.supabase_client, "latest_codex_weekly_usage", return_value=(77.0, 123)), \
         patch.object(monitor.supabase_client, "save_prompt_actor_update", return_value=True) as save:
        assert monitor.check_once(now=now, send_alert=lambda text: sent.append(text) or True) == "checked"
    assert sent == []
    assert save.call_args.args[1].state["last_seen_percent"] == 77.0


def test_threshold_alerts_only_once_per_reset_window():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    sent = []
    with patch.object(monitor, "_actor_row", return_value=actor({"threshold_percent": 85})), \
         patch.object(monitor.supabase_client, "latest_codex_weekly_usage", return_value=(85.0, 123)), \
         patch.object(monitor.supabase_client, "save_prompt_actor_update", return_value=True) as save:
        assert monitor.check_once(now=now, send_alert=lambda text: sent.append(text) or True) == "alerted"
    assert len(sent) == 1
    assert save.call_args.args[1].state["alerted_reset_at"] == 123

    sent.clear()
    previous = actor({"threshold_percent": 85, "alerted_reset_at": 123})
    with patch.object(monitor, "_actor_row", return_value=previous), \
         patch.object(monitor.supabase_client, "latest_codex_weekly_usage", return_value=(91.0, 123)), \
         patch.object(monitor.supabase_client, "save_prompt_actor_update", return_value=True):
        assert monitor.check_once(now=now, send_alert=lambda text: sent.append(text) or True) == "checked"
    assert sent == []
