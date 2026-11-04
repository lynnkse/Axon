import json
import unittest
from unittest.mock import MagicMock

from session_manager import QueueItem, SessionManagerNode


class GatewayPromptIsolationTests(unittest.TestCase):
    def node(self):
        node = SessionManagerNode.__new__(SessionManagerNode)
        node.current_item = QueueItem(
            "question", "slack", "slack-user", request_id="request-123"
        )
        node.state_lock = MagicMock()
        node.state_lock.__enter__ = MagicMock()
        node.state_lock.__exit__ = MagicMock(return_value=False)
        node.response_subscribers = []
        node.response_subs_lock = MagicMock()
        node.response_subs_lock.__enter__ = MagicMock()
        node.response_subs_lock.__exit__ = MagicMock(return_value=False)
        return node

    def test_current_prompt_context_is_correlated_to_slack_request(self):
        node = self.node()
        self.assertEqual(node._current_response_context(), {
            "source": "slack", "user_id": "slack-user", "request_id": "request-123",
        })


if __name__ == "__main__":
    unittest.main()
