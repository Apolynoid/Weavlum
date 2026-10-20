import json
import tempfile
import unittest
from pathlib import Path

from server import debuglog


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = debuglog.LOG_DIR
        debuglog.LOG_DIR = Path(self.tmp.name)
        debuglog.disable()
        debuglog._file_path = None

    def tearDown(self):
        debuglog.LOG_DIR = self._orig_dir
        debuglog.disable()
        debuglog._file_path = None
        self.tmp.cleanup()

    def test_disabled_writes_nothing(self):
        debuglog.log("ws", "in", sid="x")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertFalse(debuglog.is_enabled())

    def test_enable_and_write_event(self):
        debuglog.enable()
        self.assertTrue(debuglog.is_enabled())
        debuglog.log("ws", "in", sid="s1", message={"type": "roll", "expr": "1d20"})
        lines = Path(debuglog.current_file()).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["cat"], "ws")
        self.assertEqual(event["event"], "in")
        self.assertEqual(event["message"]["expr"], "1d20")

    def test_sensitive_masking(self):
        debuglog.enable()
        debuglog.log(
            "ai",
            "request",
            api_key="secret-key-123",
            headers={"authorization": "Bearer abc"},
            url="http://host/?host_token=ABC123",
            nested={"token": "tok", "host_token": "XYZ"},
        )
        content = Path(debuglog.current_file()).read_text(encoding="utf-8")
        event = json.loads(content.strip())
        self.assertEqual(event["api_key"], "***")
        self.assertEqual(event["headers"]["authorization"], "***")
        self.assertEqual(event["nested"]["token"], "***")
        self.assertEqual(event["nested"]["host_token"], "***")
        self.assertIn("host_token=***", event["url"])
        self.assertNotIn("secret-key-123", content)
        self.assertNotIn("ABC123", content)

    def test_truncation(self):
        debuglog.enable()
        debuglog.log("ai", "prompt", text="x" * (debuglog.MAX_FIELD + 100))
        event = json.loads(Path(debuglog.current_file()).read_text(encoding="utf-8").strip())
        self.assertIn("截断", event["text"])
        self.assertLess(len(event["text"]), debuglog.MAX_FIELD + 100)

    def test_resolve_file_safety(self):
        debuglog.enable()
        name = Path(debuglog.current_file()).name
        self.assertIsNotNone(debuglog.resolve_file(name))
        self.assertIsNone(debuglog.resolve_file("../settings.json"))
        self.assertIsNone(debuglog.resolve_file("other.txt"))

    def test_list_files(self):
        debuglog.enable()
        files = debuglog.list_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["name"].startswith("debug-"))


if __name__ == "__main__":
    unittest.main()
