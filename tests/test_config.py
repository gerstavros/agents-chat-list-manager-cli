from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.config import AppConfig, CONFIG_FILE_NAME


class AppConfigTest(unittest.TestCase):
    def test_save_load_roundtrip_preserves_appearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("core.config.get_config_dir", return_value=Path(tmp)):
                cfg = AppConfig(language="en", appearance="light", path_overrides={"zed": "/data/zed"})
                cfg.save()
                loaded = AppConfig.load()
        self.assertEqual(loaded.language, "en")
        self.assertEqual(loaded.appearance, "light")
        self.assertEqual(loaded.path_overrides, {"zed": "/data/zed"})

    def test_missing_config_defaults_to_dark_appearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("core.config.get_config_dir", return_value=Path(tmp)):
                loaded = AppConfig.load()
        self.assertEqual(loaded.appearance, "dark")

    def test_cli_save_does_not_drop_gui_appearance_setting(self):
        """The GUI and CLI share ~/.config/agentchatmanager/config.json.

        save() rewrites the whole file from the dataclass, so any key the CLI
        does not model is silently discarded. 'appearance' is GUI-owned: the CLI
        only has to carry it through, or a `chatlistctl config set-path` would
        reset the user's theme back to dark.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            # What the GUI writes: language + appearance + path overrides.
            (config_dir / CONFIG_FILE_NAME).write_text(
                json.dumps({"language": "el", "appearance": "light", "path_overrides": {}}),
                encoding="utf-8",
            )
            with mock.patch("core.config.get_config_dir", return_value=config_dir):
                cfg = AppConfig.load()
                cfg.set_path_override("zed", "/data/zed")
                cfg.save()

            on_disk = json.loads((config_dir / CONFIG_FILE_NAME).read_text(encoding="utf-8"))

        self.assertEqual(on_disk["appearance"], "light")
        self.assertEqual(on_disk["language"], "el")
        self.assertEqual(on_disk["path_overrides"], {"zed": "/data/zed"})


if __name__ == "__main__":
    unittest.main()
