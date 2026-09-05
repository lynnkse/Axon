import unittest
from unittest.mock import Mock, call, patch

import config
import supabase_client
from instance_plugin import TurnContext
from session_manager import QueueItem, SessionManagerNode


class SessionManagerInstanceHookTests(unittest.TestCase):
    def setUp(self):
        self.node = SessionManagerNode.__new__(SessionManagerNode)
        self.item = QueueItem(
            text="hello",
            source="telegram",
            user_id="anton",
            request_id="request-1",
        )
        self.expected_turn = TurnContext(
            text="hello",
            source="telegram",
            user_id="anton",
            request_id="request-1",
        )

    def test_plugin_path_calls_all_five_hooks_and_not_legacy_functions(self):
        plugin = Mock()
        plugin.system_prompt_context.return_value = "plugin recent"
        plugin.context_for_turn.return_value = "plugin semantic"
        plugin.transform_response.return_value = "plugin clean"
        self.node.instance_plugin = plugin

        with patch.object(config, "INSTANCE", "ailin"), \
             patch.object(supabase_client, "fetch_ailin_recent_conversations") as old_recent, \
             patch.object(supabase_client, "save_ailin_conversation") as old_save, \
             patch.object(supabase_client, "fetch_ailin_semantic_context") as old_semantic, \
             patch.object(supabase_client, "apply_ailin_tick") as old_tick, \
             patch.object(supabase_client, "strip_ailin_tags") as old_strip:
            self.assertEqual(self.node._system_prompt_instance_context(), "plugin recent")
            self.node._on_instance_turn_received(self.item)
            self.assertEqual(self.node._instance_context_for_turn(self.item), "plugin semantic")
            self.assertEqual(
                self.node._transform_instance_response(self.item, "raw response"),
                "plugin clean",
            )
            self.node._on_instance_turn_completed(self.item, "generic clean")

        plugin.system_prompt_context.assert_called_once_with()
        plugin.on_turn_received.assert_called_once_with(self.expected_turn)
        plugin.context_for_turn.assert_called_once_with(self.expected_turn)
        plugin.transform_response.assert_called_once_with(self.expected_turn, "raw response")
        plugin.on_turn_completed.assert_called_once_with(self.expected_turn, "generic clean")
        for old_function in (old_recent, old_save, old_semantic, old_tick, old_strip):
            old_function.assert_not_called()

    def test_legacy_fallback_preserves_all_five_calls(self):
        self.node.instance_plugin = None

        with patch.object(config, "INSTANCE", "ailin"), \
             patch.object(supabase_client, "fetch_ailin_recent_conversations", return_value="old recent") as recent, \
             patch.object(supabase_client, "save_ailin_conversation") as save, \
             patch.object(supabase_client, "fetch_ailin_semantic_context", return_value="old semantic") as semantic, \
             patch.object(supabase_client, "apply_ailin_tick") as tick, \
             patch.object(supabase_client, "strip_ailin_tags", return_value="old clean") as strip:
            self.assertEqual(self.node._system_prompt_instance_context(), "old recent")
            self.node._on_instance_turn_received(self.item)
            self.assertEqual(self.node._instance_context_for_turn(self.item), "old semantic")
            self.assertEqual(
                self.node._transform_instance_response(self.item, "raw response"),
                "old clean",
            )
            self.node._on_instance_turn_completed(self.item, "generic clean")

        recent.assert_called_once_with()
        self.assertEqual(save.call_args_list, [
            call(role="user", content="hello"),
            call(role="ailin", content="generic clean"),
        ])
        semantic.assert_called_once_with("hello")
        tick.assert_called_once_with("raw response", is_real_turn=True)
        strip.assert_called_once_with("raw response")

    def test_legacy_reflection_guards_are_unchanged(self):
        self.node.instance_plugin = None
        item = QueueItem("internal", "reflection", "internal", request_id=None)

        with patch.object(config, "INSTANCE", "ailin"), \
             patch.object(supabase_client, "save_ailin_conversation") as save, \
             patch.object(supabase_client, "apply_ailin_tick") as tick, \
             patch.object(supabase_client, "strip_ailin_tags", return_value="clean"):
            self.node._on_instance_turn_received(item)
            self.assertEqual(self.node._transform_instance_response(item, "raw"), "clean")
            self.node._on_instance_turn_completed(item, "clean")

        save.assert_not_called()
        tick.assert_called_once_with("raw", is_real_turn=False)

    def test_standard_instance_retains_relevant_dream_fallback(self):
        self.node.instance_plugin = None
        with patch.object(config, "INSTANCE", "standard"), \
             patch.object(supabase_client, "fetch_relevant_dreams", return_value="dreams") as dreams:
            self.assertEqual(self.node._instance_context_for_turn(self.item), "dreams")
        dreams.assert_called_once_with("hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
