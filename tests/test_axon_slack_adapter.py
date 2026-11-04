import os
import unittest
from unittest.mock import patch

from axon_slack_adapter import Settings, accepted_event, format_progress


class SlackAdapterTests(unittest.TestCase):
    def event(self, **updates):
        event = {
            "type": "message", "channel": "GPRIVATE", "channel_type": "group",
            "user": "UANTON", "text": "hello",
        }
        event.update(updates)
        return event

    def accepted(self, event, settings, **kwargs):
        return accepted_event(event, settings, event_team_id="TLYNNKSE", **kwargs)

    def settings(self, **updates):
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_AXON_TEAM_ID": "TLYNNKSE", "SLACK_AXON_CHANNEL_ID": "GPRIVATE",
            "AXON_GATEWAY_TOKEN": "x" * 32,
        }
        env.update(updates)
        with patch.dict(os.environ, env, clear=True):
            return Settings.from_env()

    def test_only_configured_human_channel_message_is_accepted(self):
        settings = self.settings(SLACK_AXON_ALLOWED_USER_IDS="UANTON")
        self.assertTrue(self.accepted(self.event(), settings))
        self.assertFalse(self.accepted(self.event(channel="GOTHER"), settings))
        self.assertFalse(self.accepted(self.event(bot_id="B1", text="reply"), settings))
        self.assertFalse(self.accepted(self.event(user="UOTHER"), settings))

    def test_wrong_workspace_and_non_private_channel_are_rejected(self):
        settings = self.settings(SLACK_AXON_ALLOWED_USER_IDS="UANTON")
        self.assertFalse(accepted_event(
            self.event(), settings, event_team_id="TOTHER"
        ))
        self.assertFalse(self.accepted(self.event(channel_type="channel"), settings))
        self.assertFalse(accepted_event(self.event(), settings))

    def test_explicitly_authorized_chatgpt_bot_or_app_is_accepted(self):
        settings = self.settings(
            SLACK_AXON_ALLOWED_USER_IDS="",
            SLACK_AXON_ALLOWED_BOT_IDS="BCHATGPT",
            SLACK_AXON_ALLOWED_APP_IDS="ACHATGPT",
        )
        bot_event = {
            "type": "message", "subtype": "bot_message", "channel": "GPRIVATE", "channel_type": "group",
            "bot_id": "BCHATGPT", "text": "ask Axon",
        }
        app_event = {
            "type": "message", "subtype": "bot_message", "channel": "GPRIVATE", "channel_type": "group",
            "bot_id": "BOTHER", "app_id": "ACHATGPT", "text": "ask Axon",
        }
        self.assertTrue(self.accepted(bot_event, settings, own_bot_id="BAXON"))
        self.assertTrue(self.accepted(app_event, settings, own_bot_id="BAXON"))
        self.assertFalse(self.accepted(bot_event, settings, own_bot_id="BCHATGPT"))

    def test_unlisted_bot_and_message_mutations_are_rejected(self):
        settings = self.settings(SLACK_AXON_ALLOWED_USER_IDS="UANTON")
        self.assertFalse(self.accepted({
            "type": "message", "subtype": "bot_message", "channel": "GPRIVATE", "channel_type": "group",
            "bot_id": "BOTHER", "text": "reply",
        }, settings))
        self.assertFalse(self.accepted({
            "type": "message", "subtype": "message_changed", "channel": "GPRIVATE", "channel_type": "group",
            "user": "UANTON", "text": "edited",
        }, settings))

    def test_sender_allowlist_is_required(self):
        with self.assertRaises(ValueError):
            self.settings(
                SLACK_AXON_ALLOWED_USER_IDS="",
                SLACK_AXON_ALLOWED_BOT_IDS="",
                SLACK_AXON_ALLOWED_APP_IDS="",
            )

    def test_missing_tokens_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_progress_format_keeps_public_prose_and_tool_status(self):
        self.assertEqual("[progress]\nReading logs", format_progress({"message": "Reading logs"}))
        self.assertEqual("[activity] Running a command", format_progress({"kind": "progress", "detail": "Running a command"}))


if __name__ == "__main__":
    unittest.main()
