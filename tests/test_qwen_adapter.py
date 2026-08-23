from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.adapters.qwen_code import QwenCodeAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class QwenCodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        chats_dir = self.tmp / "projects" / "-tmp-qwen-sample-project" / "chats"
        chats_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "qwen_sample.jsonl", chats_dir / "qwen-sample-session.jsonl")
        self.adapter = QwenCodeAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_conversations_skips_system_telemetry_and_corrupt_line(self):
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)
        meta = convs[0]
        self.assertEqual(meta.message_count, 3)  # 2 user + 1 assistant, system+corrupt excluded
        self.assertEqual(meta.project_path, "/tmp/qwen-sample-project")

    def test_load_conversation_normalizes_model_role_and_detects_tool_call(self):
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1].role, "assistant")  # "model" normalized to "assistant"
        self.assertTrue(messages[1].has_tool_call)
        self.assertIn("thinking", messages[1].text)

    def test_missing_chats_subdir_is_skipped_gracefully(self):
        empty_project = self.tmp / "projects" / "-tmp-no-chats-project"
        empty_project.mkdir(parents=True)
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)  # unaffected by the project with no chats/ dir

    def test_delete_conversation_removes_file(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.adapter.delete_conversation(meta)
        self.assertFalse(meta.primary_file.exists())


if __name__ == "__main__":
    unittest.main()
