from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.adapters.claude_code import ClaudeCodeAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class ClaudeCodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project_dir = self.tmp / "projects" / "-tmp-sample-project"
        self.project_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "claude_sample.jsonl", self.project_dir / "claude-sample-session.jsonl")
        self.adapter = ClaudeCodeAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _copy_fixture(self, fixture_name: str, session_id: str):
        project_dir = self.tmp / "projects" / f"-tmp-{session_id}-project"
        project_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / fixture_name, project_dir / f"{session_id}.jsonl")

    def test_list_conversations_skips_corrupt_line_and_counts_only_chat_messages(self):
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)
        meta = convs[0]
        # 3 user (incl. the scaffolding caveat line) + 1 assistant, corrupt line skipped
        self.assertEqual(meta.message_count, 4)
        self.assertEqual(meta.project_path, "/tmp/sample-project")

    def test_ai_title_line_is_used_as_display_title(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.title, "Sample Claude session about greetings")

    def test_load_conversation_flattens_tool_use_and_marks_has_tool_call(self):
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0].role, "user")  # the scaffolding caveat line
        self.assertFalse(messages[0].has_tool_call)
        self.assertTrue(messages[2].has_tool_call)  # the assistant tool_use message
        self.assertIn("tool_use", messages[2].text)

    def test_missing_base_dir_yields_nothing(self):
        adapter = ClaudeCodeAdapter(base_dir=self.tmp / "does-not-exist")
        self.assertEqual(list(adapter.list_conversations()), [])

    def test_delete_conversation_removes_file(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.assertTrue(meta.primary_file.exists())
        self.adapter.delete_conversation(meta)
        self.assertFalse(meta.primary_file.exists())

    def test_custom_title_takes_priority_over_ai_title(self):
        self._copy_fixture("claude_custom_title_sample.jsonl", "custom-title-session")
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        self.assertEqual(convs["custom-title-session"].title, "my-renamed-session")

    def test_scaffolding_only_session_falls_back_to_untitled(self):
        self._copy_fixture("claude_untitled_sample.jsonl", "untitled-session")
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        meta = convs["untitled-session"]
        # No ai-title/custom-title, and every "user" line is CLI scaffolding
        # (caveat/command-name/local-command-stdout) rather than typed text.
        self.assertEqual(meta.title, "(untitled)")


if __name__ == "__main__":
    unittest.main()
