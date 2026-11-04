import base64
import json
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import config
import fitness_transport as transport
import supabase_client


class FitnessTransportTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_publishable_test"),
            patch.object(config, "SUPABASE_URL", "https://example.supabase.co"),
            patch.object(config, "INSTANCE", "aevadim09"),
            patch.object(config, "get", side_effect=lambda key, default="": default),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def result(self, rows, status=200, returncode=0):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps({"rows": rows, "status": status}), stderr="")

    def test_protected_get_uses_rog_and_keeps_request_out_of_shell(self):
        path = "food_entries?date=eq.2026-10-03"
        with patch.object(transport.subprocess, "run", return_value=self.result([{"id": 1}])) as run:
            self.assertEqual(supabase_client._rest_get(path), [{"id": 1}])
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2], "ROG")
        self.assertNotIn(path, args[0][-1])
        self.assertNotIn("sb_publishable_test", str(run.call_args))
        self.assertEqual(json.loads(kwargs["input"])["path"], path)

    def test_authoritative_empty_read_is_valid(self):
        with patch.object(transport.subprocess, "run", return_value=self.result([])):
            self.assertEqual(supabase_client._rest_get("food_entries?date=eq.1900-01-01"), [])

    def test_connection_failure_never_means_empty(self):
        result = SimpleNamespace(returncode=255, stdout="", stderr="connection refused")
        with patch.object(transport.subprocess, "run", return_value=result):
            with self.assertRaises(transport.FitnessTransportError):
                supabase_client._rest_get("food_entries")

    def test_timeout_never_means_empty(self):
        with patch.object(transport.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 25)):
            with self.assertRaises(transport.FitnessTransportError):
                supabase_client._rest_get("fitness_log")

    def test_malformed_response_never_means_empty(self):
        for stdout in ("not json", "[]", '{"status":200,"rows":{}}'):
            with self.subTest(stdout=stdout):
                with patch.object(transport.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                    with self.assertRaises(transport.FitnessTransportError):
                        supabase_client._rest_get("frequent_foods")

    def test_insert_requires_returned_row(self):
        with patch.object(transport.subprocess, "run", return_value=self.result([{"id": 1}], 201)):
            self.assertTrue(supabase_client._rest_insert("food_entries", {"food_item": "test"}))
        with patch.object(transport.subprocess, "run", return_value=self.result([], 201)):
            self.assertFalse(supabase_client._rest_insert("food_entries", {"food_item": "test"}))

    def test_patch_requires_matched_row(self):
        with patch.object(transport.subprocess, "run", return_value=self.result([])):
            self.assertFalse(supabase_client._rest_patch("fitness_log", "date=eq.1900-01-01", {"total_calories": 0}))

    def test_nonprotected_behavior_unchanged(self):
        with patch.object(transport.subprocess, "run") as run:
            self.assertIsNone(transport.via_writer("GET", "memory?select=id"))
            run.assert_not_called()

    def test_private_key_keeps_local_transport(self):
        with patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_existing"):
            self.assertIsNone(transport.via_writer("GET", "food_entries"))

    def test_private_local_read_failure_is_explicit(self):
        with patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_existing"):
            with patch.object(supabase_client.urllib.request, "urlopen", side_effect=OSError("offline")):
                with self.assertRaises(transport.FitnessTransportError):
                    supabase_client._rest_get("food_entries")

    def test_unknown_public_key_host_requires_explicit_route(self):
        with patch.object(config, "INSTANCE", "other"):
            with self.assertRaises(transport.FitnessTransportError):
                transport.via_writer("GET", "food_entries")

    def test_custom_writer_settings(self):
        values = {"SUPABASE_FITNESS_WRITER_SSH": "writer-host", "SUPABASE_FITNESS_WRITER_REPO": "/srv/axon", "SUPABASE_FITNESS_WRITER_PYTHON": "/srv/python"}
        with patch.object(config, "get", side_effect=lambda key, default="": values.get(key, default)):
            with patch.object(transport.subprocess, "run", return_value=self.result([])) as run:
                transport.via_writer("GET", "food_entries")
        self.assertEqual(run.call_args.args[0][-2], "writer-host")
        self.assertIn("/srv/axon", run.call_args.args[0][-1])

    def test_key_classification(self):
        encode = lambda role: "x." + base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=") + ".x"
        self.assertTrue(transport.is_server_key(encode("service_role")))
        for key in ("", "sb_publishable_a", encode("anon"), "a.!invalid.b"):
            self.assertFalse(transport.is_server_key(key))

    def test_remote_script_checks_project_before_request(self):
        request = {"method": "GET", "path": "food_entries", "project_url": "https://wrong.supabase.co"}
        with patch.object(sys, "argv", ["writer", "/unused"]), patch.object(sys, "stdin", io.StringIO(json.dumps(request))):
            with patch.object(supabase_client.urllib.request, "urlopen") as urlopen:
                with self.assertRaisesRegex(SystemExit, "project mismatch"):
                    exec(transport.WRITER_SCRIPT, {})
                urlopen.assert_not_called()

    def test_remote_script_rejects_public_credential(self):
        request = {"method": "GET", "path": "food_entries", "project_url": config.SUPABASE_URL}
        with patch.object(sys, "argv", ["writer", "/unused"]), patch.object(sys, "stdin", io.StringIO(json.dumps(request))):
            with self.assertRaisesRegex(SystemExit, "server credential"):
                exec(transport.WRITER_SCRIPT, {})

    def test_remote_script_returns_row_and_never_credential(self):
        request = {"method": "POST", "path": "food_entries", "project_url": config.SUPABASE_URL,
                   "payload": {"food_item": "test"}, "timeout": 10}
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 201
        response.__enter__.return_value.read.return_value = b'[{"id":123}]'
        output = io.StringIO()
        with patch.object(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_existing"):
            with patch.object(sys, "argv", ["writer", "/unused"]), patch.object(sys, "stdin", io.StringIO(json.dumps(request))):
                with patch.object(supabase_client.urllib.request, "urlopen", return_value=response) as urlopen, redirect_stdout(output):
                    exec(transport.WRITER_SCRIPT, {})
        self.assertEqual(json.loads(output.getvalue()), {"status": 201, "rows": [{"id": 123}]})
        self.assertNotIn("sb_secret", output.getvalue())
        self.assertEqual(urlopen.call_args.args[0].get_header("Prefer"), "return=representation")


if __name__ == "__main__":
    unittest.main()
