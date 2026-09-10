import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operational.gateway import Denied, KNOWN, load_manifest, probe, public_addresses


class ManifestTests(unittest.TestCase):
    def policy(self):
        return {"schema_version": 1, "policy_id": "test", "services": {
            name: {"host": f"{name}.example.com", "path": "/"} for name in KNOWN}}

    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value))
            return load_manifest(str(path))

    def test_valid_manifest_loads_and_hashes(self):
        digest, services = self.load(self.policy())
        self.assertEqual(64, len(digest))
        self.assertEqual(KNOWN, frozenset(services))

    def test_unknown_missing_and_extra_services_fail_closed(self):
        for mutate in (lambda p: p.update(extra=True),
                       lambda p: p["services"].pop("groq"),
                       lambda p: p["services"].update(evil={"host":"evil.example", "path":"/"})):
            policy = self.policy(); mutate(policy)
            with self.assertRaises(Denied): self.load(policy)

    def test_ip_hosts_and_ambiguous_paths_fail(self):
        for key, value in (("host", "127.0.0.1"), ("path", "//evil")):
            policy = self.policy(); policy["services"]["claude"][key] = value
            with self.assertRaises(Denied): self.load(policy)

    def test_unknown_service_is_visible_denial_without_resolution(self):
        with patch("operational.gateway.public_addresses") as resolver:
            with self.assertRaisesRegex(Denied, "service_disabled"):
                probe("unknown", self.policy()["services"])
            resolver.assert_not_called()

    def test_every_controlled_dns_answer_must_be_public(self):
        with patch("operational.gateway.dns_query", side_effect=[("203.0.113.8",), ("2606:4700::1111",)]):
            with self.assertRaisesRegex(Denied, "destination_denied"):
                public_addresses("claude.example.com")

    def test_controlled_dns_deduplicates_both_families(self):
        with patch("operational.gateway.dns_query", side_effect=[("8.8.8.8", "8.8.8.8"), ("2606:4700::1111",)]):
            self.assertEqual(("8.8.8.8", "2606:4700::1111"), public_addresses("claude.example.com"))


if __name__ == "__main__":
    unittest.main()
