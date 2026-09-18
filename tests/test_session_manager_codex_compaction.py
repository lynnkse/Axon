import threading
import unittest
from unittest.mock import Mock, patch

import config
from session_manager_codex import SessionManagerCodexNode


class SessionManagerCodexCompactionTests(unittest.TestCase):
    def setUp(self):
        self.node = SessionManagerCodexNode.__new__(SessionManagerCodexNode)
        self.node.current_thread_id = None
        self.node.pty_lock = threading.Lock()
        self.node.master_fd = None
        self.node.codex_proc = None
        self.node._rollouts_before_spawn = set()
        self.node._spawn_time = 0.0
        self.node._remote_mode = False

    def _spawn_command(self, thread_id=None):
        self.node.current_thread_id = thread_id
        proc = Mock(pid=1234)
        with patch("session_manager_codex.pty.openpty", return_value=(10, 11)), \
             patch.object(self.node, "_set_pty_size"), \
             patch("session_manager_codex.glob.glob", return_value=[]), \
             patch("session_manager_codex.subprocess.Popen", return_value=proc) as popen, \
             patch("session_manager_codex.os.close"):
            self.node._spawn_codex()
        return popen.call_args.args[0]

    def test_fresh_thread_uses_bounded_total_context(self):
        with patch.object(config, "CODEX_AUTO_COMPACT_TOKEN_LIMIT", 100000):
            cmd = self._spawn_command()

        self.assertEqual(cmd[:5], [
            config.CODEX_PATH,
            "-c", "model_auto_compact_token_limit=100000",
            "-c", 'model_auto_compact_token_limit_scope="total"',
        ])
        self.assertIn("-C", cmd)
        self.assertIn(config.PROJECT_DIR, cmd)
        self.assertNotIn("resume", cmd)

    def test_resumed_thread_uses_same_context_bound(self):
        with patch.object(config, "CODEX_AUTO_COMPACT_TOKEN_LIMIT", 120000):
            cmd = self._spawn_command("thread-123")

        self.assertEqual(cmd[:5], [
            config.CODEX_PATH,
            "-c", "model_auto_compact_token_limit=120000",
            "-c", 'model_auto_compact_token_limit_scope="total"',
        ])
        self.assertIn("resume", cmd)
        self.assertIn("thread-123", cmd)
        self.assertNotIn("-C", cmd)

    def test_remote_fresh_thread_uses_ssh_exec_and_remote_project(self):
        self.node._remote_mode = True
        with patch.object(config, "CODEX_REMOTE_HOST", "anton@100.64.0.1"), \
             patch.object(config, "CODEX_REMOTE_PATH", "/opt/codex"), \
             patch.object(config, "CODEX_REMOTE_PROJECT_DIR", "/srv/Axon"):
            cmd = self.node._remote_codex_command()

        self.assertEqual(cmd[:5], ["ssh", "-o", "BatchMode=yes", "anton@100.64.0.1", cmd[4]])
        self.assertIn("/opt/codex exec", cmd[4])
        self.assertIn("-C /srv/Axon", cmd[4])
        self.assertNotIn("resume", cmd[4])

    def test_remote_resumes_saved_thread(self):
        self.node.current_thread_id = "thread-123"
        with patch.object(config, "CODEX_REMOTE_HOST", "anton@host"), \
             patch.object(config, "CODEX_REMOTE_PATH", "/opt/codex"):
            cmd = self.node._remote_codex_command()

        self.assertIn("exec resume", cmd[4])
        self.assertIn("thread-123", cmd[4])
        self.assertNotIn(" -C ", cmd[4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
