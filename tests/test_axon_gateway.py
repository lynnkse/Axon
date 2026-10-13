import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from axon_gateway import AxonSocketClient, ConflictError, TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "gateway.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, key="key-1", text="check status"):
        return self.store.create_task(
            conversation_id="conversation-1",
            idempotency_key=key,
            target="axon-main",
            text=text,
            timeout_seconds=30,
            caller_id="test",
        )

    def test_idempotent_create_returns_same_task(self):
        first, created = self.create()
        second, created_again = self.create()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.task_id, second.task_id)

    def test_idempotency_key_rejects_different_payload(self):
        self.create()
        with self.assertRaises(ConflictError):
            self.create(text="different")

    def test_task_lifecycle_is_persisted(self):
        task, _ = self.create()
        claimed = self.store.claim_next("worker")
        self.assertEqual(task.task_id, claimed["task_id"])
        self.assertEqual("running", self.store.get(task.task_id).status)
        self.store.finish(task.task_id, status="succeeded", result="done", caller_id="worker")
        finished = self.store.get(task.task_id)
        self.assertEqual("succeeded", finished.status)
        self.assertEqual("done", finished.result)

    def test_database_is_private(self):
        mode = os.stat(Path(self.tmp.name) / "gateway.sqlite3").st_mode & 0o777
        self.assertEqual(0o600, mode)

    def test_event_stream_is_ordered_and_deduplicated(self):
        task, _ = self.create()
        self.store.record_event(task.task_id, kind="phase", detail="Preparing")
        self.store.record_event(task.task_id, kind="phase", detail="Preparing")
        self.store.record_event(task.task_id, kind="progress", message="Reading logs")
        events = self.store.events_after(task.task_id)
        self.assertEqual(["phase", "progress"], [event["kind"] for event in events])
        self.assertEqual([], self.store.events_after(task.task_id, events[-1]["event_id"]))


class SocketClientTests(unittest.TestCase):
    def test_exact_source_and_request_id_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = str(Path(directory) / "input.sock")
            response_path = str(Path(directory) / "response.sock")
            ready = threading.Event()

            def fake_manager():
                input_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                response_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                input_server.bind(input_path)
                response_server.bind(response_path)
                input_server.listen(1)
                response_server.listen(1)
                ready.set()
                response_conn, _ = response_server.accept()
                input_conn, _ = input_server.accept()
                request = json.loads(input_conn.recv(65536).splitlines()[0])
                self.assertEqual("slack", request["source"])
                self.assertEqual("slack-gateway", request["user_id"])
                messages = [
                    {"text": "wrong source", "source": "telegram", "request_id": request["request_id"]},
                    {"text": "wrong request", "source": "slack", "request_id": "other"},
                    {"type": "activity", "growing": True, "kind": "progress", "detail": "Reading", "source": "slack", "request_id": request["request_id"]},
                    {"text": "correct", "source": "slack", "request_id": request["request_id"]},
                ]
                response_conn.sendall(b"".join((json.dumps(msg) + "\n").encode() for msg in messages))
                input_conn.close()
                response_conn.close()
                input_server.close()
                response_server.close()

            thread = threading.Thread(target=fake_manager)
            thread.start()
            self.assertTrue(ready.wait(2))
            client = AxonSocketClient(input_path, response_path, source="slack")
            row = {
                "text": "hello",
                "request_id": "request-1",
                "timeout_seconds": 5,
            }
            activities = []
            self.assertEqual("correct", client.execute(row, activities.append))
            self.assertEqual("Reading", activities[0]["detail"])
            thread.join(2)

    def test_unrouted_source_is_rejected(self):
        with self.assertRaises(ValueError):
            AxonSocketClient(source="unrouted")


if __name__ == "__main__":
    unittest.main()
