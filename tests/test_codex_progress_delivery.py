"""No network/model calls: exercise streaming with deterministic child processes."""
import asyncio
import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import codex_chat_cli
import config
import session_manager_codex as manager
import telegram_node as telegram


class StreamingTests(unittest.TestCase):
    def node(self, source="telegram"):
        node = manager.SessionManagerCodexNode.__new__(manager.SessionManagerCodexNode)
        node._remote_mode = False
        node.pty_lock = threading.Lock()
        node.state_lock = threading.Lock()
        node.response_subs_lock = threading.Lock()
        node.current_item = manager.QueueItem("test", source, "tester", request_id="current")
        node.response_subscribers = []
        node._remember_thread = Mock()
        node._publish_activity = Mock()
        node._turn_timeout = lambda: 5
        return node

    def run_child(self, node, code, lane="main"):
        node._exec_codex_command = lambda *_: [sys.executable, "-u", "-c", code]
        return node._run_exec_codex_turn("test prompt", lane, "test-model")

    def test_progress_is_delivered_while_process_is_running_and_final_is_not_streamed_twice(self):
        node = self.node()
        observations = []
        def activity(**payload):
            if payload.get("message"):
                observations.append((payload["message"], node._active_exec_proc.poll()))
        node._publish_activity.side_effect = activity
        code = '''
import json, sys, time
sys.stdin.read()
def emit(x): print(json.dumps(x), flush=True)
emit({"type":"thread.started", "thread_id":"test-thread"})
emit({"type":"item.completed", "item":{"type":"agent_message","text":"Checking the source."}})
emit({"type":"item.started", "item":{"type":"command_execution", "command":"PRIVATE_ARGUMENT"}})
time.sleep(0.3)
emit({"type":"item.completed", "item":{"type":"command_execution", "aggregated_output":"PRIVATE_RESULT"}})
emit({"type":"item.completed", "item":{"type":"agent_message","text":"Finished."}})
emit({"type":"turn.completed"})
'''
        self.assertEqual(self.run_child(node, code), ("Finished.", None))
        self.assertEqual(observations, [("Checking the source.", None)])
        node._remember_thread.assert_called_once_with("main", "test-model", "test-thread")
        payloads = repr(node._publish_activity.call_args_list)
        self.assertNotIn("PRIVATE_ARGUMENT", payloads)
        self.assertNotIn("PRIVATE_RESULT", payloads)
        self.assertNotIn("Finished.", payloads)
        self.assertIsNone(node._active_exec_proc)

    def test_actor_protocol_and_reasoning_are_never_public_progress(self):
        for lane, text in (("actor", "private actor reasoning"), ("main", "<<<AXON_ROUTE_UPDATE>>>secret")):
            with self.subTest(lane=lane):
                node = self.node()
                events = [
                    {"type":"item.completed", "item":{"type":"reasoning", "text":"PRIVATE_REASONING"}},
                    {"type":"item.completed", "item":{"type":"agent_message", "text":text}},
                    {"type":"item.started", "item":{"type":"mcp_tool_call", "arguments":{"secret":"private"}}},
                    {"type":"item.completed", "item":{"type":"agent_message", "text":"final"}},
                ]
                code = "import sys,json; sys.stdin.read(); events=" + repr(events) + "; [print(json.dumps(x),flush=True) for x in events]"
                self.assertEqual(self.run_child(node, code, lane), ("final", None))
                exposed = [call.kwargs.get("message") for call in node._publish_activity.call_args_list]
                self.assertFalse(any(exposed))
                self.assertNotIn("PRIVATE_REASONING", repr(node._publish_activity.call_args_list))

    def test_full_stderr_pipe_does_not_block_stream(self):
        node = self.node()
        code = '''
import sys, json
sys.stdin.read()
sys.stderr.write(("diagnostic\\n" * 20000)); sys.stderr.flush()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
'''
        self.assertEqual(self.run_child(node, code), ("done", None))

    def test_deadline_reaps_child_and_clears_active_process(self):
        node = self.node()
        node._turn_timeout = lambda: 0.15
        children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with patch.object(manager.subprocess, "Popen", side_effect=launch):
            reply, error = self.run_child(node, "import sys,time; sys.stdin.read(); time.sleep(20)")
        self.assertEqual(reply, "")
        self.assertIn("timeout", error)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(node._active_exec_proc)

    def test_heartbeat_is_not_meaningful_activity_and_is_correlated(self):
        node = self.node()
        del node._publish_activity
        subscriber = Mock()
        node.response_subscribers = [subscriber]
        node._activity_phase = "actor"
        node._publish_activity()
        payload = json.loads(subscriber.sendall.call_args.args[0])
        self.assertFalse(payload["growing"])
        self.assertEqual(payload["kind"], "heartbeat")
        self.assertEqual((payload["source"], payload["request_id"], payload["phase"]),
                         ("telegram", "current", "actor"))

    def test_phase_budget_respects_remaining_request_deadline(self):
        node = self.node()
        del node._turn_timeout
        with patch.object(manager.time, "monotonic", return_value=100):
            node.current_item.deadline = 107
            self.assertEqual(node._turn_timeout(), 7)
            node.current_item.deadline = 99
            self.assertEqual(node._turn_timeout(), 0)
            node.current_item.deadline = 100 + 10 * manager._RESPONSE_TIMEOUT
            self.assertEqual(node._turn_timeout(), manager._RESPONSE_TIMEOUT)

    def test_dead_native_process_is_detected_even_without_rollout_data(self):
        node = self.node()
        node._running = True
        node.codex_proc = Mock()
        node.codex_proc.poll.return_value = 1
        response, error = node._wait_for_rollout_response(Path("/nonexistent-axon-test-rollout"), 0)
        self.assertEqual(response, "")
        self.assertIn("exited", error)


class TelegramActivityTests(unittest.TestCase):
    def test_transport_heartbeat_does_not_refresh_task_activity(self):
        tracker = telegram.ActivityTracker(started_at=0, last_activity_time=10)
        with patch.object(telegram.time, "monotonic", return_value=100):
            tracker.observe({"growing":False, "phase":"actor"})
        self.assertEqual(tracker.last_activity_time, 10)
        self.assertEqual(tracker.last_heartbeat_time, 100)
        self.assertEqual(telegram._activity_state(tracker, 100), ("quiet", 90))
        self.assertEqual(telegram._activity_state(tracker, 180), ("stalled", 170))

    def test_wait_rejects_foreign_source_and_late_request(self):
        class Subscriber:
            messages = iter([
                {"source":"chatgpt", "request_id":"current", "text":"wrong source"},
                {"source":"telegram", "request_id":"old", "text":"late reply"},
                {"type":"activity", "source":"telegram", "request_id":"old", "growing":True},
                {"source":"telegram", "request_id":"current", "text":"correct"},
            ])
            async def get(self):
                return next(self.messages)
        tracker = telegram.ActivityTracker()
        self.assertEqual(asyncio.run(telegram._wait_for_response(Subscriber(), "telegram", tracker, "current")), "correct")
        self.assertIsNone(tracker.last_activity_time)

    def test_request_finished_index_drops_only_exact_request(self):
        with patch.object(threading.Thread, "start"):
            subscriber = telegram.ResponseSubscriber(Mock())
        subscriber.finish_request("telegram", "old")
        self.assertTrue(subscriber._request_finished({"source":"telegram", "request_id":"old"}))
        self.assertFalse(subscriber._request_finished({"source":"telegram", "request_id":"current"}))
        self.assertFalse(subscriber._request_finished({"source":"cli", "request_id":"old"}))

    def test_request_timeout_records_finished_request(self):
        subscriber = Mock()
        with patch.object(telegram, "_RESPONSE_TIMEOUT", 0):
            response = asyncio.run(telegram._wait_for_response(subscriber, "telegram", request_id="current"))
        self.assertIn("stopped sending status updates", response)
        subscriber.finish_request.assert_called_once_with("telegram", "current")

    def test_completion_and_send_failure_stop_both_background_tasks(self):
        async def scenario(failure):
            stopped = set()
            async def background(name):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.add(name)
            async def waiting(*_, **__):
                await asyncio.sleep(0)
                return "Finished."
            async def typing(*_):
                await background("typing")
            async def status(*_):
                await background("status")
            update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(
                reply_chat_action=AsyncMock(), reply_text=AsyncMock()))
            subscriber = Mock()
            with patch.object(telegram, "_message_lock", asyncio.Lock()), \
                 patch.object(telegram, "_send_to_session_manager", side_effect=OSError("offline") if failure else None,
                              return_value="current"), \
                 patch.object(telegram, "_wait_for_response", side_effect=waiting), \
                 patch.object(telegram, "_typing_keepalive", side_effect=typing), \
                 patch.object(telegram, "_status_notifier", side_effect=status):
                await telegram._handle_and_reply(update, None, subscriber, "question")
            if not failure:
                self.assertEqual(stopped, {"typing", "status"})
            # Failure can cancel the task before it first starts; no pending
            # keepalive/status tasks are left in either case.
            self.assertFalse([task for task in asyncio.all_tasks() if task is not asyncio.current_task()])
            response = update.effective_message.reply_text.call_args.args[0]
            self.assertIn("unavailable" if failure else "Finished.", response)
            subscriber.finish_request.assert_called_once_with("telegram", None if failure else "current")
        for failure in (False, True):
            with self.subTest(failure=failure):
                asyncio.run(scenario(failure))

    def test_notifier_does_not_repeat_quiet_status_or_claim_work(self):
        async def scenario():
            stop = asyncio.Event()
            tracker = telegram.ActivityTracker(started_at=time.monotonic() - 200,
                last_activity_time=time.monotonic() - 100, last_heartbeat_time=time.monotonic(), phase="actor")
            update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
            task = asyncio.create_task(telegram._status_notifier(update, stop, tracker, initial_delay=0.001, interval=0))
            await asyncio.sleep(2.1)
            stop.set()
            await task
            update.effective_message.reply_text.assert_awaited_once()
            message = update.effective_message.reply_text.call_args.args[0]
            self.assertIn("progress is unconfirmed", message)
            self.assertIn("Last phase", message)
            self.assertNotIn("still working", message.lower())
        asyncio.run(scenario())

    def test_shared_timeout_includes_both_manager_phases_and_delivery_grace(self):
        expected = config.CODEX_RESPONSE_TIMEOUT_SECONDS * (2 if config.ACTORS_ENABLED else 1) + 120
        self.assertEqual(config.CODEX_REQUEST_TIMEOUT_SECONDS, expected)
        self.assertGreater(telegram._RESPONSE_TIMEOUT, expected)


class RoutedChatTests(unittest.TestCase):
    def test_progress_then_exactly_one_final_and_no_other_source(self):
        stream = codex_chat_cli.ChatStream()
        stream.register("current")
        with patch.object(codex_chat_cli, "write_status") as status, patch.object(codex_chat_cli, "write_reply") as reply:
            stream.handle({"type":"activity", "source":"cli", "request_id":"current", "phase":"main",
                           "growing":True, "message":"Checking now."})
            status.assert_called_once_with("Your request: Checking now.")
            reply.assert_not_called()
            for source, request in (("chatgpt", "current"), ("live_voice", "current"), ("cli", "old")):
                stream.handle({"source":source, "request_id":request, "text":"wrong"})
            reply.assert_not_called()
            stream.handle({"source":"cli", "request_id":"current", "text":"done"})
            stream.handle({"source":"cli", "request_id":"current", "text":"duplicate"})
            stream.handle({"type":"activity", "source":"cli", "request_id":"current", "growing":True, "message":"late"})
            reply.assert_called_once_with("done", "cli")
            status.assert_called_once()

    def test_telegram_events_are_labelled_and_foreign_gateway_activity_is_ignored(self):
        stream = codex_chat_cli.ChatStream()
        with patch.object(codex_chat_cli, "write_status") as status, patch.object(codex_chat_cli, "write_reply") as reply:
            for source in ("chatgpt", "live_voice", "reflection", "ralph", "unknown"):
                stream.handle({"type":"activity", "source":source, "request_id":"other", "growing":True})
            status.assert_not_called()
            stream.handle({"type":"activity", "source":"telegram", "request_id":"tg", "phase":"actor",
                           "kind":"phase", "growing":True, "detail":"Checking"})
            status.assert_called_once_with("Telegram · Project actors: Checking")
            stream.handle({"source":"telegram", "request_id":"tg", "text":"answer"})
            reply.assert_called_once_with("answer", "telegram")


if __name__ == "__main__":
    unittest.main()
