import tempfile
import unittest
from pathlib import Path

from axon_voice_bridge import auth_cookie_value, build_server, live_session_payload, task_packet


class VoiceBridgeTests(unittest.TestCase):
    def test_live_session_uses_gpt_live_and_client_delegation(self):
        payload = live_session_payload("v=0")
        self.assertEqual("gpt-live-1", payload["session"]["model"])
        self.assertEqual({"type": "client"}, payload["session"]["delegation"])
        self.assertEqual("webrtc", payload["transport"]["type"])

    def test_pair_cookie_is_derived_not_raw(self):
        token = "x" * 32
        cookie = auth_cookie_value(token)
        self.assertNotIn(token, cookie)
        self.assertEqual(cookie, auth_cookie_value(token))

    def test_task_packet_is_bounded(self):
        packet = task_packet("hello " * 10000, 123)
        self.assertLessEqual(len(packet.encode("utf-8")), 16 * 1024)
        self.assertIn("123 ms", packet)

    def test_server_refuses_non_loopback_bind_or_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory)
            (static / "index.html").write_text("ok")
            with self.assertRaises(ValueError):
                build_server(
                    "0.0.0.0", 0, openai_api_key="", gateway_token="g" * 32,
                    pair_token="p" * 32, static_dir=static,
                )
            with self.assertRaises(ValueError):
                build_server(
                    "127.0.0.1", 0, openai_api_key="", gateway_token="g" * 32,
                    pair_token="p" * 32, gateway_url="http://100.64.0.1:8765", static_dir=static,
                )


if __name__ == "__main__":
    unittest.main()
