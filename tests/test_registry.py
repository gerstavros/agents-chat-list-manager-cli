from __future__ import annotations

import unittest

from core import registry
from core.adapters.base import ToolAdapter
from core.config import AppConfig


class RegistryTest(unittest.TestCase):
    def test_discover_adapters_registers_all_three_builtin_tools(self):
        classes = registry.get_adapter_classes()
        tool_ids = {cls.tool_id for cls in classes}
        self.assertEqual(tool_ids, {"claude_code", "qwen_code", "codewhale_tui"})
        for cls in classes:
            self.assertTrue(issubclass(cls, ToolAdapter))

    def test_build_adapters_applies_path_overrides(self):
        config = AppConfig(path_overrides={"claude_code": "/tmp/custom-claude-dir"})
        adapters = registry.build_adapters(config)
        claude_adapter = next(a for a in adapters if a.tool_id == "claude_code")
        self.assertEqual(str(claude_adapter.base_dir), "/tmp/custom-claude-dir")

    def test_build_adapters_without_override_uses_default(self):
        config = AppConfig()
        adapters = registry.build_adapters(config)
        for adapter in adapters:
            self.assertEqual(adapter.base_dir, type(adapter).default_base_dir())


if __name__ == "__main__":
    unittest.main()
