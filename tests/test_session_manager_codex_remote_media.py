import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
from session_manager_codex import SessionManagerCodexNode


class SessionManagerCodexRemoteMediaTests(unittest.TestCase):
    def setUp(self):
        self.node = SessionManagerCodexNode.__new__(SessionManagerCodexNode)
        self.node.current_thread_id = "thread-123"
        self.node.pty_lock = threading.Lock()
        self.node.codex_proc = None

    def test_stage_remote_media_uses_generated_name_and_rewrites_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "photo name;$(touch nope).JPG"
            media.write_bytes(b"image")
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(config, "CODEX_REMOTE_HOST", "anton@host"), \
                 patch("session_manager_codex.secrets.token_hex", return_value="a" * 32), \
                 patch("session_manager_codex.subprocess.run", return_value=completed) as run:
                rewritten, remote_path = self.node._stage_remote_media(
                    f"[Image: {media}]\n\nAnalyze this image.", str(media),
                )

        self.assertEqual(remote_path, "/tmp/axon-relay-media/" + "a" * 32 + ".jpg")
        self.assertIn(f"[Image: {remote_path}]", rewritten)
        self.assertNotIn("photo name", rewritten)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][0], "scp")
        self.assertEqual(run.call_args_list[1].args[0][-2], str(media.resolve()))
        self.assertNotIn("photo name", run.call_args_list[1].args[0][-1])

    def test_stage_remote_media_appends_path_when_prompt_has_no_marker(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as media:
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(config, "CODEX_REMOTE_HOST", "anton@host"), \
                 patch("session_manager_codex.subprocess.run", return_value=completed):
                rewritten, remote_path = self.node._stage_remote_media(
                    "Please inspect the attachment.", media.name,
                )

        self.assertTrue(remote_path.endswith(".pdf"))
        self.assertTrue(rewritten.endswith(f"[Attached file: {remote_path}]"))

    def test_missing_media_fails_before_remote_codex_launch(self):
        self.node._forward_display = Mock()
        with patch("session_manager_codex.subprocess.Popen") as popen:
            response, error = self.node._run_remote_codex_turn(
                "[Image: /missing/photo.jpg]", "/missing/photo.jpg",
            )

        self.assertEqual(response, "")
        self.assertIn("Remote media staging failed", error)
        popen.assert_not_called()

    def test_cleanup_uses_generated_remote_path_as_one_quoted_argument(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(config, "CODEX_REMOTE_HOST", "anton@host"), \
             patch("session_manager_codex.subprocess.run", return_value=completed) as run:
            self.node._cleanup_remote_media("/tmp/axon-relay-media/safe.jpg")

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["ssh", "-o", "BatchMode=yes", "anton@host"])
        self.assertEqual(command[4], "rm -f -- /tmp/axon-relay-media/safe.jpg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
