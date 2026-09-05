import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instance_plugin import _load_instance_plugin


METHODS = """
    def system_prompt_context(self): return "context"
    def on_turn_received(self, turn): pass
    def context_for_turn(self, turn): return ""
    def transform_response(self, turn, response_text): return response_text
    def on_turn_completed(self, turn, clean_response_text): pass
"""


class InstancePluginLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_plugin(self, source: str) -> Path:
        entry = self.directory / "plugin.py"
        entry.write_text(source)
        return entry

    def test_unset_or_blank_path_returns_none_without_import_attempt(self):
        for path in ("", "   "):
            with self.subTest(path=path), patch(
                "instance_plugin.importlib.util.spec_from_file_location"
            ) as make_spec:
                self.assertIsNone(_load_instance_plugin(path))
                make_spec.assert_not_called()

    def test_missing_file_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            _load_instance_plugin(str(self.directory / "missing.py"))

    def test_non_python_file_raises(self):
        entry = self.directory / "plugin.txt"
        entry.write_text("def create_plugin(): return None\n")
        with self.assertRaisesRegex(ValueError, "must be a .py file"):
            _load_instance_plugin(str(entry))

    def test_directory_with_python_suffix_raises(self):
        entry = self.directory / "plugin.py"
        entry.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            _load_instance_plugin(str(entry))

    def test_syntax_error_raises(self):
        entry = self.write_plugin("def broken(:\n")
        with self.assertRaises(SyntaxError):
            _load_instance_plugin(str(entry))

    def test_missing_create_plugin_raises(self):
        entry = self.write_plugin("VALUE = 1\n")
        with self.assertRaisesRegex(TypeError, "create_plugin"):
            _load_instance_plugin(str(entry))

    def test_create_plugin_exception_propagates(self):
        entry = self.write_plugin(
            "def create_plugin():\n    raise RuntimeError('factory failed')\n"
        )
        with self.assertRaisesRegex(RuntimeError, "factory failed"):
            _load_instance_plugin(str(entry))

    def test_incomplete_plugin_raises(self):
        entry = self.write_plugin(
            "class Plugin:\n"
            "    def system_prompt_context(self): return ''\n\n"
            "def create_plugin(): return Plugin()\n"
        )
        with self.assertRaisesRegex(TypeError, "on_turn_received"):
            _load_instance_plugin(str(entry))

    def test_valid_plugin_loads_and_factory_runs_once(self):
        entry = self.write_plugin(
            "factory_calls = 0\n"
            "class Plugin:\n"
            f"{METHODS}\n"
            "def create_plugin():\n"
            "    global factory_calls\n"
            "    factory_calls += 1\n"
            "    return Plugin()\n"
        )
        plugin = _load_instance_plugin(str(entry))
        module = sys.modules[plugin.__class__.__module__]
        self.assertEqual(plugin.system_prompt_context(), "context")
        self.assertEqual(module.factory_calls, 1)
        self.assertTrue(plugin.__class__.__module__.startswith("_axon_instance_extension_"))

    def test_relative_sibling_import_uses_private_package(self):
        (self.directory / "helper.py").write_text("VALUE = 'from sibling'\n")
        entry = self.write_plugin(
            "from .helper import VALUE\n"
            "class Plugin:\n"
            f"{METHODS}\n"
            "    def system_prompt_context(self): return VALUE\n\n"
            "def create_plugin(): return Plugin()\n"
        )
        original_path = list(sys.path)
        plugin = _load_instance_plugin(str(entry))
        self.assertEqual(plugin.system_prompt_context(), "from sibling")
        self.assertEqual(sys.path, original_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
