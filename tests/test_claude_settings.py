from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.claude_settings import CLEANUP_VALUE, ensure_claude_cleanup_period


class ClaudeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings_path = self.tmp / ".claude" / "settings.json"

    def _write(self, data: dict) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_missing_file_creates_it_with_cleanup_period(self):
        status = ensure_claude_cleanup_period(self.settings_path)
        self.assertEqual(status, "created")
        self.assertTrue(self.settings_path.exists())
        self.assertEqual(
            json.loads(self.settings_path.read_text(encoding="utf-8")),
            {"cleanupPeriodDays": CLEANUP_VALUE},
        )

    def test_existing_correct_value_is_left_untouched(self):
        data = {"cleanupPeriodDays": CLEANUP_VALUE, "theme": "light", "effortLevel": "xhigh"}
        self._write(data)
        before = self.settings_path.read_text(encoding="utf-8")
        status = ensure_claude_cleanup_period(self.settings_path)
        self.assertEqual(status, "unchanged")
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), before)

    def test_different_value_is_rewritten_and_other_keys_preserved(self):
        data = {
            "cleanupPeriodDays": 30,
            "theme": "light",
            "permissions": {"allow": ["Read(/**)"]},
        }
        self._write(data)
        status = ensure_claude_cleanup_period(self.settings_path)
        self.assertEqual(status, "updated")
        result = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(result["cleanupPeriodDays"], CLEANUP_VALUE)
        self.assertEqual(result["theme"], "light")
        self.assertEqual(result["permissions"], {"allow": ["Read(/**)"]})

    def test_missing_key_is_added_and_other_keys_preserved(self):
        data = {"theme": "dark"}
        self._write(data)
        status = ensure_claude_cleanup_period(self.settings_path)
        self.assertEqual(status, "updated")
        result = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(result["cleanupPeriodDays"], CLEANUP_VALUE)
        self.assertEqual(result["theme"], "dark")

    def test_corrupt_file_is_left_untouched(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text("{ this is not valid json", encoding="utf-8")
        status = ensure_claude_cleanup_period(self.settings_path)
        self.assertEqual(status, "skipped")
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{ this is not valid json")


if __name__ == "__main__":
    unittest.main()
