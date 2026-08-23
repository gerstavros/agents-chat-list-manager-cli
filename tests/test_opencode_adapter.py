from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.adapters.opencode import OpenCodeAdapter
from core.models import ConversationMeta

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    title TEXT NOT NULL,
    model TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    time_archived INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
"""

# Fixed epoch so assertions don't depend on wall-clock time.
BASE_MS = 1_800_000_000_000


def _build_sample_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO session (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_open1",
            "proj1",
            "/tmp/opencode-sample-project",
            "Διόρθωση σφάλματος",
            '{"id":"big-pickle","providerID":"opencode"}',
            BASE_MS,
            BASE_MS + 60_000,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO session (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_archived",
            "proj1",
            "/tmp/opencode-other-project",
            "παλιά αρχειοθετημένη συνομιλία",
            "{}",
            BASE_MS - 1_000,
            BASE_MS - 500,
            BASE_MS,
        ),
    )
    conn.executemany(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        [
            ("msg_open1", "ses_open1", BASE_MS, BASE_MS + 10, '{"role":"user"}'),
            ("msg_open2", "ses_open1", BASE_MS + 20, BASE_MS + 30, '{"role":"assistant"}'),
        ],
    )
    conn.executemany(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "prt_1",
                "msg_open1",
                "ses_open1",
                BASE_MS,
                BASE_MS + 5,
                '{"type":"text","text":"γεια, δοκιμάζω το opencode"}',
            ),
            (
                "prt_2",
                "msg_open2",
                "ses_open1",
                BASE_MS + 20,
                BASE_MS + 25,
                '{"type":"reasoning","text":"the user greets me"}',
            ),
            (
                "prt_3",
                "msg_open2",
                "ses_open1",
                BASE_MS + 26,
                BASE_MS + 27,
                '{"type":"tool","tool":"bash","state":{"status":"completed"}}',
            ),
            (
                "prt_4",
                "msg_open2",
                "ses_open1",
                BASE_MS + 28,
                BASE_MS + 29,
                '{"type":"text","text":"έτοιμο"}',
            ),
        ],
    )
    conn.commit()
    conn.close()


def _make_meta(db_path: Path, session_id: str = "ses_open1") -> ConversationMeta:
    return ConversationMeta(
        tool_id="opencode",
        session_id=session_id,
        title="Διόρθωση σφάλματος",
        project_path="/tmp/opencode-sample-project",
        created_at=None,
        updated_at=None,
        message_count=2,
        primary_file=db_path,
    )


class OpenCodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _build_sample_db(self.tmp / "opencode.db")
        self.adapter = OpenCodeAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_conversations_reads_metadata_and_skips_archived(self):
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)
        meta = convs[0]
        self.assertEqual(meta.session_id, "ses_open1")
        self.assertEqual(meta.title, "Διόρθωση σφάλματος")
        self.assertEqual(meta.project_path, "/tmp/opencode-sample-project")
        self.assertEqual(meta.message_count, 2)
        self.assertEqual(meta.extra.get("model"), "big-pickle")
        self.assertIsInstance(meta.created_at, datetime)
        self.assertIsInstance(meta.updated_at, datetime)
        self.assertEqual(meta.primary_file, self.tmp / "opencode.db")

    def test_load_conversation_flattens_parts_and_detects_tool_call(self):
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[1].role, "assistant")
        self.assertIn("γεια", messages[0].text)
        self.assertIn("thinking", messages[1].text)
        self.assertIn("έτοιμο", messages[1].text)
        self.assertTrue(messages[1].has_tool_call)
        self.assertFalse(messages[0].has_tool_call)
        self.assertIsInstance(messages[0].timestamp, datetime)
        self.assertEqual(messages[0].raw.get("role"), "user")

    def test_missing_database_yields_empty_results(self):
        empty_dir = Path(tempfile.mkdtemp())
        try:
            adapter = OpenCodeAdapter(base_dir=empty_dir)
            self.assertEqual(list(adapter.list_conversations()), [])
            self.assertEqual(adapter.load_conversation(_make_meta(empty_dir / "opencode.db")), [])
            adapter.delete_conversation(_make_meta(empty_dir / "opencode.db"))  # must not raise either
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_corrupt_database_is_skipped_not_raised(self):
        db_path = self.tmp / "opencode.db"
        db_path.write_text("{ this is not a sqlite database", encoding="utf-8")
        adapter = OpenCodeAdapter(base_dir=self.tmp)
        self.assertEqual(list(adapter.list_conversations()), [])
        self.assertEqual(adapter.load_conversation(_make_meta(db_path)), [])
        adapter.delete_conversation(_make_meta(db_path))  # must not raise either

    def test_delete_conversation_removes_session_and_cascades(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.adapter.delete_conversation(meta)
        self.assertEqual(list(self.adapter.list_conversations()), [])
        conn = sqlite3.connect(self.tmp / "opencode.db")
        try:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM session WHERE id = ?", (meta.session_id,)
            ).fetchone()[0]
            message_count = conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?", (meta.session_id,)
            ).fetchone()[0]
            part_count = conn.execute(
                "SELECT COUNT(*) FROM part WHERE session_id = ?", (meta.session_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(session_count, 0)
        self.assertEqual(message_count, 0)
        self.assertEqual(part_count, 0)


if __name__ == "__main__":
    unittest.main()
