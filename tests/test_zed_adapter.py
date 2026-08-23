from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.adapters.zed import ZedAdapter, has_zstd_decoder
from core.models import ConversationMeta

FIXTURES = Path(__file__).parent / "fixtures"

SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data_type TEXT NOT NULL,
    data BLOB NOT NULL,
    parent_id TEXT,
    folder_paths TEXT,
    folder_paths_order TEXT,
    created_at TEXT
);
"""

SAMPLE_BLOB = (FIXTURES / "zed_thread_sample.zst").read_bytes()

NEEDS_DECODER = unittest.skipUnless(has_zstd_decoder(), "no zstd decoder (libzstd or zstd CLI) available")


def _make_meta(db_path: Path, session_id: str = "thread-sample") -> ConversationMeta:
    return ConversationMeta(
        tool_id="zed",
        session_id=session_id,
        title="Zed sample thread",
        project_path="/tmp/zed-sample-project",
        created_at=None,
        updated_at=None,
        message_count=4,
        primary_file=db_path,
    )


class ZedAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        threads_dir = self.tmp / "threads"
        threads_dir.mkdir(parents=True)
        self.db_path = threads_dir / "threads.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO threads (id, summary, updated_at, data_type, data, folder_paths, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-sample",
                "Zed sample thread",
                "2026-08-23T15:11:08.674604104Z",
                "zstd",
                SAMPLE_BLOB,
                "/tmp/zed-sample-project",
                "2026-08-23T15:01:47.298260369+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO threads (id, summary, updated_at, data_type, data, folder_paths, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "thread-second",
                "Second Zed thread",
                "2026-08-22T14:44:35.855812785+00:00",
                "zstd",
                SAMPLE_BLOB,
                "/tmp/zed-other-project",
                "2026-08-22T13:43:10.667222962+00:00",
            ),
        )
        conn.commit()
        conn.close()
        self.adapter = ZedAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_conversations_reads_metadata_without_decompression(self):
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        self.assertEqual(len(convs), 2)
        meta = convs["thread-sample"]
        self.assertEqual(meta.title, "Zed sample thread")
        self.assertEqual(meta.project_path, "/tmp/zed-sample-project")
        self.assertEqual(meta.primary_file, self.db_path)
        self.assertIsInstance(meta.created_at, datetime)
        self.assertIsInstance(meta.updated_at, datetime)
        # nanosecond ISO timestamps are truncated to microseconds, still aware/UTC
        self.assertEqual(meta.updated_at.isoformat(), "2026-08-23T15:11:08.674604+00:00")

    @NEEDS_DECODER
    def test_message_count_and_model_when_decoder_available(self):
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        meta = convs["thread-sample"]
        # 4 dict messages; the "Resume" string marker is not counted
        self.assertEqual(meta.message_count, 4)
        self.assertEqual(meta.extra.get("model"), "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertFalse(meta.extra.get("decoder_missing"))

    @NEEDS_DECODER
    def test_load_conversation_flattens_blocks_and_skips_markers(self):
        meta = next(c for c in self.adapter.list_conversations() if c.session_id == "thread-sample")
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "hello zed, look at this project")
        self.assertEqual(messages[1].role, "assistant")
        self.assertEqual(messages[1].text, "I will help you inspect it.")
        self.assertFalse(messages[1].has_tool_call)
        # third message: thinking + tool call + tool result flattened together
        self.assertIn("[thinking] let me grep", messages[2].text)
        self.assertIn("[tool_call: grep]", messages[2].text)
        self.assertIn("[tool_result: grep] src/main.rs:10: class Foo", messages[2].text)
        # dict-typed outputs (e.g. find_path match pages) are JSON-dumped, not crashed on
        self.assertIn(
            '[tool_result: find_path] {"offset":0,"current_matches_page":[],"all_matches_len":0}',
            messages[2].text,
        )
        self.assertTrue(messages[2].has_tool_call)
        self.assertEqual(messages[3].text, "thanks")
        # Zed has no per-message timestamps
        self.assertTrue(all(m.timestamp is None for m in messages))

    def test_unknown_data_type_lists_but_does_not_load(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO threads (id, summary, updated_at, data_type, data) VALUES (?, ?, ?, ?, ?)",
            ("thread-weird", "weird", "2026-08-22T07:34:12Z", "other", b"not zstd"),
        )
        conn.commit()
        conn.close()
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        self.assertIn("thread-weird", convs)
        self.assertEqual(convs["thread-weird"].message_count, 0)
        self.assertEqual(self.adapter.load_conversation(convs["thread-weird"]), [])

    def test_missing_database_yields_empty_results(self):
        empty_dir = Path(tempfile.mkdtemp())
        try:
            adapter = ZedAdapter(base_dir=empty_dir)
            self.assertEqual(list(adapter.list_conversations()), [])
            self.assertEqual(adapter.load_conversation(_make_meta(empty_dir / "threads" / "threads.db")), [])
            adapter.delete_conversation(_make_meta(empty_dir / "threads" / "threads.db"))  # no raise
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_corrupt_database_is_skipped_not_raised(self):
        self.db_path.write_text("{ this is not a sqlite database", encoding="utf-8")
        adapter = ZedAdapter(base_dir=self.tmp)
        self.assertEqual(list(adapter.list_conversations()), [])
        self.assertEqual(adapter.load_conversation(_make_meta(self.db_path)), [])
        adapter.delete_conversation(_make_meta(self.db_path))  # no raise

    def test_delete_conversation_removes_only_that_row(self):
        meta = next(c for c in self.adapter.list_conversations() if c.session_id == "thread-second")
        self.adapter.delete_conversation(meta)
        remaining = {c.session_id for c in self.adapter.list_conversations()}
        self.assertEqual(remaining, {"thread-sample"})
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM threads WHERE id = ?", ("thread-second",)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
