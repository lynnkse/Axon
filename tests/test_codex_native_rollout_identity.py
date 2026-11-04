import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from session_manager_codex import SessionManagerCodexNode


class NativeRolloutIdentityTests(unittest.TestCase):
    def test_new_actor_rollout_is_never_adopted_as_native(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / '.codex/sessions'
            directory.mkdir(parents=True)
            for name, source in [('native', 'cli'), ('newer_actor', 'exec')]:
                (directory / (name + '.jsonl')).write_text(json.dumps({
                    'payload': {'id': name, 'source': source, 'cwd': '/project'}}) + '\n')
            node = SessionManagerCodexNode.__new__(SessionManagerCodexNode)
            node.current_thread_id = None
            node._rollouts_before_spawn = set()
            with patch('session_manager_codex.Path.home', return_value=Path(root)), patch('session_manager_codex.config.PROJECT_DIR', '/project'):
                self.assertEqual(node._rollout_path(), directory / 'native.jsonl')
                (directory / 'native.jsonl').unlink()
                self.assertIsNone(node._rollout_path())
