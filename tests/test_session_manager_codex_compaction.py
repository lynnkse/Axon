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


if __name__ == "__main__":
    unittest.main(verbosity=2)
