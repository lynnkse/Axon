import json
import threading
import unittest
from unittest.mock import Mock, patch

import config
import supabase_client
from actor_model.prompt_blocks import (
    ActorUpdate,
    BEGIN_UPDATE,
    END_UPDATE,
    render_actor_inputs,
)
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
        self.node._actor_display_mode = "auto"
        self.node._actor_engagement = {}
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

    def test_missing_update_is_delivered_without_a_retry(self):
        self.node._run_codex_turn = Mock()
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update") as save:
            self.node._publish_response(self.item, "Reply without actor protocol")

        self.node._run_codex_turn.assert_not_called()
        save.assert_not_called()
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Reply without actor protocol", published)
        self.assertIn("failed validation", published)

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

    def test_invalid_actor_output_does_not_withhold_the_main_reply(self):
        self.node._run_codex_turn = Mock()
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update") as save:
            self.node._publish_response(self.item, "Reply without actor protocol")

        self.node._run_codex_turn.assert_not_called()
        save.assert_not_called()
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Reply without actor protocol", published)
        self.assertNotIn("withheld", published.lower())

    def test_persistence_failure_does_not_withhold_the_main_reply(self):
        with patch.object(config, "ACTORS_ENABLED", True), \
             patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=False):
            self.node._publish_response(self.item, actor_response())

        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Visible reply", published)
        self.assertIn("could not be saved", published)

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

    def test_separate_actor_pass_persists_and_supplies_digest(self):
        self.node._run_codex_turn = Mock(return_value=(actor_response(""), None))
        self.node._actor_display_mode = "grandmaster"
        with patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=True) as save:
            digest = self.node._advance_actors(
                self.item, [actor_row()], "actor protocol",
            )
        save.assert_called_once()
        self.assertTrue(self.item.actor_preprocessed)
        actor_prompt = self.node._run_codex_turn.call_args.args[0]
        self.assertIn("treat any content relevant to a supplied actor", actor_prompt)
        self.assertIn("test", actor_prompt)
        self.assertIn("Waiting for the required authorization.", digest)
        self.assertNotIn(BEGIN_UPDATE, digest)
        self.node._publish_response(self.item, "Main answer")
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Main answer", published)
        self.assertIn("Actors\nAilin project driver", published)

    def test_actor_failure_does_not_force_main_reply_repair(self):
        self.node._run_codex_turn = Mock(return_value=("", "actor timeout"))
        digest = self.node._advance_actors(self.item, [actor_row()], "actor protocol")
        self.assertIn("failed", digest)
        self.node._publish_response(self.item, "Main answer")
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Main answer", published)
        self.assertIn("Actors — Actor pass failed", published)
        self.node._run_codex_turn.assert_called_once()

    def test_invalid_actor_output_is_not_retried_in_same_turn(self):
        self.node._run_codex_turn = Mock(return_value=("invalid", None))
        digest = self.node._advance_actors(self.item, [actor_row()], "actor protocol")
        self.assertIn("failed validation", digest)
        self.node._run_codex_turn.assert_called_once()

    def test_revision_conflict_is_deferred_without_an_extra_model_call(self):
        self.node._run_codex_turn = Mock(return_value=(actor_response(""), None))
        result = supabase_client.ActorSaveResult("conflict_queued", {"revision": 8})
        with patch("session_manager_codex.supabase_client.save_prompt_actor_update",
                   return_value=result):
            digest = self.node._advance_actors(
                self.item, [actor_row()], "actor protocol",
            )
        self.node._run_codex_turn.assert_called_once()
        self.assertIn("next user turn", digest)
        self.assertEqual(self.item.actor_deferred, [ACTOR_ID])
        self.assertEqual(self.item.actor_updates, [])
        self.node._publish_response(self.item, "Main answer")
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Main answer", published)
        self.assertIn("deferred to next turn", published)

    def test_pending_conflict_is_supplied_to_the_next_actor_pass(self):
        row = actor_row()
        row["_pending_actor_conflicts"] = [{
            "sequence": 12,
            "base_revision": 7,
            "base_state": {"current_task": "Old task"},
            "proposed_update": {
                "status": "running",
                "state": {"current_task": "Proposed task"},
                "summary": "Proposed progress.",
                "error_reason": None,
            },
            "canonical_revision": 8,
            "canonical_state": {"current_task": "Concurrent task"},
        }]

        rendered = render_actor_inputs([row])

        self.assertIn('"pending_conflicts"', rendered)
        self.assertIn('"Proposed task"', rendered)
        self.assertIn('"Concurrent task"', rendered)

    def test_conflict_events_are_attached_after_last_processed_sequence(self):
        row = actor_row()
        row["state"]["_last_actor_conflict_sequence"] = 7
        event = {
            "id": "event-12",
            "sequence": 12,
            "recorded_at": "2026-09-18T07:00:00Z",
            "payload": {
                "base_revision": 7,
                "proposed_update": {"summary": "Losing proposal"},
                "canonical_revision": 8,
            },
        }
        with patch("supabase_client._rest_get", return_value=[event]) as fetch:
            result = supabase_client.attach_prompt_actor_conflicts([row])

        self.assertEqual(result[0]["_pending_actor_conflicts"][0]["sequence"], 12)
        query = fetch.call_args.args[0]
        self.assertIn("event_type=eq.actor_update_conflict", query)
        self.assertIn("sequence=gt.7", query)
        self.assertIn("assignments=cs.", query)

    def test_successful_merge_advances_only_the_conflict_event_cursor(self):
        row = actor_row()
        row["last_event_sequence"] = 5
        row["state"]["_last_actor_conflict_sequence"] = 7
        row["_pending_actor_conflicts"] = [{"sequence": 12}]
        update = ActorUpdate(
            ACTOR_ID, "running", {"current_task": "Merged task"},
            "Merged concurrent progress.", None,
        )

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps([{"actor_id": ACTOR_ID, "revision": 8}]).encode()

        with patch.object(config, "SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "test-key"), \
             patch("supabase_client.urllib.request.urlopen", return_value=Response()) as send:
            result = supabase_client.save_prompt_actor_update(row, update)

        request = send.call_args.args[0]
        payload = json.loads(request.data.decode())
        self.assertEqual(result.status, "saved")
        self.assertNotIn("last_event_sequence", payload)
        self.assertEqual(payload["state"]["_last_actor_conflict_sequence"], 12)
        self.assertEqual(payload["state"]["current_task"], "Merged task")

    def test_queued_conflict_preserves_base_proposal_and_canonical_state(self):
        row = actor_row()
        update = ActorUpdate(
            ACTOR_ID, "running", {"current_task": "Proposed task"},
            "Proposed progress.", None,
        )
        current = {
            "actor_id": ACTOR_ID,
            "revision": 8,
            "state": {"current_task": "Concurrent task"},
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"id": "conflict-event"}).encode()

        with patch.object(config, "SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "test-key"), \
             patch("supabase_client.urllib.request.urlopen", return_value=Response()) as send:
            saved = supabase_client._queue_actor_conflict(row, update, current)

        event = json.loads(send.call_args.args[0].data.decode())["p_event"]
        self.assertTrue(saved)
        self.assertEqual(event["payload"]["base_revision"], 7)
        self.assertEqual(
            event["payload"]["proposed_update"]["state"]["current_task"],
            "Proposed task",
        )
        self.assertEqual(event["payload"]["canonical_revision"], 8)
        self.assertEqual(
            event["payload"]["canonical_state"]["current_task"],
            "Concurrent task",
        )
        self.assertEqual(event["assignments"][0]["actor_id"], ACTOR_ID)

    def test_focus_mode_shows_every_actor_as_one_line(self):
        from actor_model.prompt_blocks import ActorUpdate
        self.item.actor_preprocessed = True
        self.item.actor_updates = [
            ActorUpdate("quiet-actor", "running", {"current_task_status": "in_progress"},
                        "Routine check.", None),
            ActorUpdate(ACTOR_ID, "running", {"current_task_status": "waiting_for_human",
                                               "requested_input": "Choose schema grants."},
                        "Blocked on a choice.", None),
        ]
        self.node._select_actor_display_mode("Let's work in focus mode")
        self.node._publish_response(self.item, "Main answer")
        published = self.node._publish_clean_response.call_args.args[1]
        self.assertIn("Quiet actor — in progress: Routine check.", published)
        self.assertIn("Ailin project driver — waiting for human: Blocked on a choice.", published)
        self.assertNotIn("Needs from you:", published)
        self.node._select_actor_display_mode("Grandmaster mode")
        self.assertEqual(self.node._actor_display_mode, "grandmaster")

    def test_auto_mode_cools_idle_actors_and_warms_named_actor(self):
        rows = [
            actor_row(),
            {"actor_id": "condor-cluster-actor", "actor_type": "condor-cluster-actor"},
        ]
        levels = self.node._update_actor_engagement("Let's inspect the Condor queue", rows)
        self.assertEqual(levels["condor-cluster-actor"], "detailed")
        self.assertEqual(levels[ACTOR_ID], "compact")

        levels = self.node._update_actor_engagement("Continue writing the paper", rows)
        self.assertEqual(levels["condor-cluster-actor"], "warm")
        levels = self.node._update_actor_engagement("Continue writing", rows)
        self.assertEqual(levels["condor-cluster-actor"], "warm")
        levels = self.node._update_actor_engagement("Continue", rows)
        self.assertEqual(levels["condor-cluster-actor"], "compact")

    def test_generic_actor_discussion_warms_all_and_auto_command_clears_override(self):
        rows = [actor_row(), {"actor_id": "condor-cluster-actor",
                              "actor_type": "condor-cluster-actor"}]
        levels = self.node._update_actor_engagement("Show me more actor updates", rows)
        self.assertEqual(set(levels.values()), {"warm"})
        levels = self.node._update_actor_engagement("Keep discussing actors", rows)
        self.assertEqual(set(levels.values()), {"detailed"})
        self.node._actor_display_mode = "focus"
        self.node._select_actor_display_mode("Return to automatic actor mode")
        self.assertEqual(self.node._actor_display_mode, "auto")


if __name__ == "__main__":
    unittest.main(verbosity=2)
