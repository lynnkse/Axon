import json
import io
import threading
import unittest
from unittest.mock import Mock, patch
from session_manager_codex import SessionManagerCodexNode, QueueItem


class DisplayMirrorTests(unittest.TestCase):
    def node(self):
        n = SessionManagerCodexNode.__new__(SessionManagerCodexNode)
        n.display_lock = threading.Lock()
        n.display_clients = []
        n._display_history = bytearray()
        n._remote_mode = False
        n.pty_lock = threading.Lock()
        n._thread_registry = {}
        n.response_subs_lock = threading.Lock()
        n.response_subscribers = []
        n._transform_instance_response = lambda item, text: text
        n._on_instance_turn_completed = Mock()
        return n

    def test_clients_receive_identical_replay_and_live_bytes(self):
        n = self.node()
        n._forward_display(b'earlier\r\n')
        a, b = Mock(), Mock()
        n._attach_display(a)
        n._attach_display(b)
        n._forward_display(b'next\r\n')
        self.assertEqual(a.sendall.call_args_list, b.sendall.call_args_list)
        self.assertEqual(b''.join(c.args[0] for c in a.sendall.call_args_list), b'earlier\r\nnext\r\n')

    def test_replay_bounded_and_dead_client_does_not_block_others(self):
        n = self.node()
        bad, good = Mock(), Mock()
        bad.sendall.side_effect = OSError()
        n.display_clients = [bad, good]
        n._forward_display(b'x' * 300000)
        self.assertEqual(len(n._display_history), 262144)
        self.assertEqual(n.display_clients, [good])

    @patch('session_manager_codex.supabase_client.save_message')
    @patch('session_manager_codex.supabase_client.process_response', return_value='clean reply')
    def test_relay_reply_never_enters_native_display(self, process, save):
        n = self.node()
        item = QueueItem(text='question', source='telegram', user_id='anton', request_id='test-123')
        subscriber = Mock()
        n.response_subscribers = [subscriber]
        for remote, routed in ((False, True), (False, False), (True, True)):
            with self.subTest(remote=remote, routed=routed):
                n._remote_mode, n._main_uses_exec = remote, routed
                n._publish_clean_response(item, 'raw answer\n\nActors\nsummary')
                self.assertEqual(bytes(n._display_history), b'')
                payload = json.loads(subscriber.sendall.call_args.args[0])
                self.assertEqual(payload, {'text': 'clean reply', 'source': 'telegram',
                                          'user_id': 'anton', 'request_id': 'test-123'})

    def test_exec_main_and_actor_output_never_becomes_display_bytes(self):
        n = self.node()
        n.current_item = QueueItem(text='question', source='telegram', user_id='anton')
        n._publish_activity = Mock()
        for lane in ('main', 'actor'):
            proc = Mock(returncode=0)
            proc.poll.return_value = 0
            proc.stdin = io.StringIO()
            proc.stdout = io.StringIO(json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': 'exec answer'}}) + '\n')
            proc.stderr = io.StringIO()
            with self.subTest(lane=lane), patch('session_manager_codex.subprocess.Popen', return_value=proc):
                self.assertEqual(n._run_exec_codex_turn('prompt', lane, 'test-model'),
                                 ('exec answer', None))
                self.assertEqual(bytes(n._display_history), b'')

    def test_native_bytes_are_preserved_during_routed_turn(self):
        n = self.node()
        n._running = True
        n._main_uses_exec = True
        n.master_fd = 1
        chunks = [b'\x1b[2J\x1b[Hnative prompt\r\n', b'\x1b[32mtool ',
                  b'activity\x1b[0m\r\n\xe2', b'\x80\xba ']
        client = Mock()
        n.display_clients = [client]
        with patch('session_manager_codex.os.read', side_effect=chunks + [b'']):
            n._pty_reader_thread()
        self.assertEqual(b''.join(c.args[0] for c in client.sendall.call_args_list),
                         b''.join(chunks))

if __name__ == '__main__':
    unittest.main()
