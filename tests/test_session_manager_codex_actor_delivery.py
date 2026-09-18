import json
import threading
import unittest
from unittest.mock import Mock, patch

import config
from actor_model.prompt_blocks import BEGIN_UPDATE, END_UPDATE
from session_manager_codex import QueueItem, SessionManagerCodexNode


ACTOR_ID = "ailin-project-driver"


def actor_row():
    return {
        "actor_id": ACTOR_ID,
        "actor_type": ACTOR_ID,
        "revision": 7,
        "disposition": "ready_again",
        "state": {"role": "Advance the Ailin roadmap."},
    }


def actor_response(message="Visible reply", requested_input="Authorize the next step."):
    payload = {
        "actor_id": ACTOR_ID,
        "status": "running",
        "state": {
            "current_task": "Expose the roadmap",
            "current_task_status": "waiting_for_human",
            "requested_input": requested_input,
            "roadmap_items": [
                "Supabase permissions",
                "Sandbox",
                "Internet browsing",
                "Instagram connection",
                "Memory retrieval",
                "Q-learning",
                "Principal model v2",
            ],
        },
        "summary": "Waiting for the required authorization.",
        "error_reason": None,
    }
    return f"{message}\n{BEGIN_UPDATE}\n{json.dumps(payload)}\n{END_UPDATE}"


class SessionManagerCodexActorDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.node = SessionManagerCodexNode.__new__(SessionManagerCodexNode)
        self.node._publish_clean_response = Mock()
        self.item = QueueItem(
            text="test",
            source="telegram",
            user_id="anton",
            prompt_actor_rows=[actor_row()],
        )

    def test_valid_update_is_persisted_and_project_status_is_readable(self):
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=True) as save:
            self.node._publish_response(self.item, actor_response())

        save.assert_called_once()
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Visible reply", published)
        self.assertIn("Actors\nAilin project driver — waiting for human", published)
        self.assertIn("  Now: Expose the roadmap", published)
        self.assertIn("  Waiting for the required authorization.", published)
        self.assertIn("Needs from you: Authorize the next step.", published)
        self.assertNotIn(BEGIN_UPDATE, published)

    def test_missing_update_gets_one_repair_attempt(self):
        self.node._run_codex_turn = Mock(return_value=(actor_response("Repaired reply"), None))
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=True):
            self.node._publish_response(self.item, "Reply without actor protocol")

        self.node._run_codex_turn.assert_called_once()
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Repaired reply", published)
        self.assertIn("Ailin project driver — waiting for human", published)
        self.assertIn("Needs from you:", published)

    def test_in_progress_update_is_visible_without_a_fake_blocker(self):
        response = actor_response(requested_input="None")
        payload_start = response.index(BEGIN_UPDATE) + len(BEGIN_UPDATE)
        payload_end = response.index(END_UPDATE)
        payload = json.loads(response[payload_start:payload_end])
        payload["state"]["current_task_status"] = "in_progress"
        payload["summary"] = "Verified the permissions inventory."
        response = f"Visible reply\n{BEGIN_UPDATE}\n{json.dumps(payload)}\n{END_UPDATE}"

        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=True):
            self.node._publish_response(self.item, response)

        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Ailin project driver — in progress", published)
        self.assertIn("  Verified the permissions inventory.", published)
        self.assertNotIn("Needs from you:", published)

    def test_invalid_repair_fails_closed_without_persistence(self):
        self.node._run_codex_turn = Mock(return_value=("Still invalid", None))
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update") as save:
            self.node._publish_response(self.item, "Reply without actor protocol")

        save.assert_not_called()
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("withheld", published.lower())

    def test_persistence_failure_fails_closed(self):
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=False):
            self.node._publish_response(self.item, actor_response())

        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("could not be persisted", published)

    def test_local_tui_does_not_stream_raw_actor_answer_during_turn(self):
        self.node._running = True
        self.node.master_fd = 1
        self.node.state_lock = threading.Lock()
        self.node.current_item = self.item
        self.node._forward_display = Mock()
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.os.read", side_effect=[b"raw actor JSON", b""]):
            self.node._pty_reader_thread()
        self.node._forward_display.assert_not_called()

    def test_local_tui_does_not_redraw_raw_actor_answer_after_turn(self):
        self.node._running = True
        self.node.master_fd = 1
        self.node.current_item = None
        self.node._forward_display = Mock()
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.os.read", side_effect=[b"raw actor JSON", b""]):
            self.node._pty_reader_thread()
        self.node._forward_display.assert_not_called()

    def test_all_actors_get_readable_lines(self):
        from actor_model.prompt_blocks import ActorUpdate

        updates = [
            ActorUpdate(ACTOR_ID, "running", {"current_task_status": "in_progress"},
                        "Designing scoped access.", None),
            ActorUpdate("condor-cluster-actor", "running", {},
                        "Five slots; four unclaimed.", None),
        ]
        summary = self.node._actor_progress_summary(updates)
        self.assertIn("Ailin project driver — in progress", summary)
        self.assertIn("Condor cluster — running", summary)
        self.assertIn("Five slots; four unclaimed.", summary)
        self.assertNotIn("{", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
