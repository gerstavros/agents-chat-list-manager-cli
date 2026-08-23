from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.adapters.codewhale_tui import CodewhaleTuiAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class CodewhaleTuiAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sessions_dir = self.tmp / "sessions"
        sessions_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "codewhale_sample.json", sessions_dir / "codewhale-sample-session.json")
        artifacts_dir = sessions_dir / "codewhale-sample-session" / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "note.txt").write_text("artifact placeholder", encoding="utf-8")
        self.adapter = CodewhaleTuiAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_conversations_reads_metadata_without_messages(self):
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)
        meta = convs[0]
        self.assertEqual(meta.title, "Sample codewhale session")
        self.assertEqual(meta.message_count, 2)
        self.assertEqual(meta.project_path, "/tmp/codewhale-sample-project")
        self.assertEqual(len(meta.sidecar_paths), 1)

    def test_load_conversation_has_no_per_message_timestamp(self):
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(m.timestamp is None for m in messages))
        self.assertTrue(messages[1].has_tool_call)
        self.assertIn("thinking", messages[1].text)

    def test_corrupt_session_file_is_listed_not_raised(self):
        shutil.copy(FIXTURES / "codewhale_corrupt.json", self.tmp / "sessions" / "broken-session.json")
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        self.assertIn("broken-session", convs)
        self.assertTrue(convs["broken-session"].extra.get("corrupt"))

    def test_delete_conversation_removes_file_and_artifacts_sidecar(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.adapter.delete_conversation(meta)
        self.assertFalse(meta.primary_file.exists())
        for sidecar in meta.sidecar_paths:
            self.assertFalse(sidecar.exists())


if __name__ == "__main__":
    unittest.main()
