from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# opencode 2.x schema
#
# A 2.x database carries BOTH the 1.x tables (left over from the migration) and
# the new `session_v2` / `session_message` pair. New sessions are written to the
# v2 tables only; migrated ones exist in both, with the transcript moved to
# `session_message`.
# ---------------------------------------------------------------------------

SCHEMA_V2 = """
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
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    title TEXT,
    model TEXT,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    time_archived INTEGER
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session_v2(id) ON DELETE CASCADE
);
"""


def _build_v2_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_V2)
    # A session that exists in both tables (migrated): the v2 transcript is
    # authoritative and it must be listed exactly once.
    conn.execute(
        "INSERT INTO session_v2 (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            "ses_v2_migrated",
            "proj1",
            "/tmp/opencode-v2-project",
            "νεότερη συνομιλία v2",
            '{"id":"deepseek-v4.1-flash-free"}',
            BASE_MS + 100_000,
            BASE_MS + 200_000,
        ),
    )
    conn.execute(
        "INSERT INTO session (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            "ses_v2_migrated",
            "proj1",
            "/tmp/opencode-v2-project",
            "νεότερη συνομιλία v2",
            '{"id":"deepseek-v4.1-flash-free"}',
            BASE_MS + 100_000,
            BASE_MS + 200_000,
        ),
    )
    # A session that only ever existed in the legacy table (never migrated).
    conn.execute(
        "INSERT INTO session (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            "ses_legacy_only",
            "proj1",
            "/tmp/opencode-1x-project",
            "παλιά συνομιλία 1.x",
            "{}",
            BASE_MS - 10_000,
            BASE_MS - 5_000,
        ),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        ("msg_legacy1", "ses_legacy_only", BASE_MS - 9_000, BASE_MS - 8_000, '{"role":"user"}'),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt_legacy1",
            "msg_legacy1",
            "ses_legacy_only",
            BASE_MS - 9_000,
            BASE_MS - 8_000,
            '{"type":"text","text":"μόνο στο legacy schema"}',
        ),
    )
    # An archived v2 session must stay hidden.
    conn.execute(
        "INSERT INTO session_v2 (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ses_v2_archived", "proj1", "/tmp/old", "αρχειοθετημένη", "{}", BASE_MS, BASE_MS, BASE_MS + 1),
    )
    conn.executemany(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "smsg_1",
                "ses_v2_migrated",
                "user",
                1,
                BASE_MS + 100_000,
                BASE_MS + 100_000,
                '{"text":"δοκίμασε το v2","time":{"created":1}}',
            ),
            (
                "smsg_2",
                "ses_v2_migrated",
                "assistant",
                2,
                BASE_MS + 110_000,
                BASE_MS + 110_000,
                json.dumps(
                    {
                        "agent": "build",
                        "content": [
                            {"type": "reasoning", "text": "πρέπει να τρέξω ls", "time": {}},
                            {"type": "tool", "name": "bash", "state": {"status": "completed"}, "time": {}},
                            {"type": "tool", "name": "read", "state": {"status": "error", "error": "boom"}, "time": {}},
                            {"type": "text", "text": "τελείωσε"},
                        ],
                        "time": {},
                    }
                ),
            ),
            (
                "smsg_3",
                "ses_v2_migrated",
                "idle",
                3,
                BASE_MS + 120_000,
                BASE_MS + 120_000,
                '{"outcome":"success","time":{}}',
            ),
            (
                "smsg_4",
                "ses_v2_migrated",
                "system",
                4,
                BASE_MS + 130_000,
                BASE_MS + 130_000,
                '{"text":"σύστημα: πλαίσιο"}',
            ),
        ],
    )
    conn.commit()
    conn.close()


class OpenCodeV2AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _build_v2_db(self.tmp / "opencode.db")
        self.adapter = OpenCodeAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lists_v2_sessions_and_skips_archived(self):
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        self.assertNotIn("ses_v2_archived", convs)
        self.assertIn("ses_v2_migrated", convs)
        meta = convs["ses_v2_migrated"]
        self.assertEqual(meta.title, "νεότερη συνομιλία v2")
        self.assertEqual(meta.project_path, "/tmp/opencode-v2-project")
        self.assertEqual(meta.extra.get("model"), "deepseek-v4.1-flash-free")
        self.assertEqual(meta.extra.get("schema"), "v2")
        self.assertEqual(meta.message_count, 3)  # user + assistant + system (idle skipped)

    def test_session_in_both_tables_is_listed_once(self):
        ids = [c.session_id for c in self.adapter.list_conversations()]
        self.assertEqual(ids.count("ses_v2_migrated"), 1)

    def test_unmigrated_legacy_session_still_listed(self):
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        legacy = convs["ses_legacy_only"]
        self.assertEqual(legacy.extra.get("schema"), "v1")
        self.assertEqual(legacy.message_count, 1)
        messages = self.adapter.load_conversation(legacy)
        self.assertEqual([m.text for m in messages], ["μόνο στο legacy schema"])

    def test_load_v2_transcript_flattens_content_blocks(self):
        convs = {c.session_id: c for c in self.adapter.list_conversations()}
        messages = self.adapter.load_conversation(convs["ses_v2_migrated"])
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "δοκίμασε το v2")
        assistant = messages[1]
        self.assertEqual(assistant.role, "assistant")
        self.assertIn("[thinking] πρέπει να τρέξω ls", assistant.text)
        self.assertIn("[tool_call: bash]", assistant.text)
        self.assertIn("[tool_call: read] [error]", assistant.text)
        self.assertIn("τελείωσε", assistant.text)
        self.assertTrue(assistant.has_tool_call)
        self.assertEqual(messages[2].role, "system")
        self.assertEqual(messages[2].text, "σύστημα: πλαίσιο")
        # the `idle` marker row carries no content and is not a message
        self.assertEqual(len(messages), 3)

    def test_message_count_matches_transcript_length(self):
        # The listing counts rows of content-bearing types; loading emits exactly
        # one Message per such row (runtime markers such as `idle` are excluded
        # from both), so the two must agree.
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == "ses_v2_migrated")
        self.assertEqual(meta.message_count, 3)
        self.assertEqual(meta.message_count, len(self.adapter.load_conversation(meta)))

    def test_failed_assistant_turn_is_rendered_as_error(self):
        conn = sqlite3.connect(self.tmp / "opencode.db")
        try:
            conn.execute(
                "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "smsg_err",
                    "ses_v2_migrated",
                    "assistant",
                    5,
                    BASE_MS + 140_000,
                    BASE_MS + 140_000,
                    json.dumps(
                        {
                            "agent": "build",
                            "content": [],
                            "error": {"type": "provider.error", "message": "invalid API key"},
                            "time": {},
                        }
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        meta = next(m for m in self.adapter.list_conversations() if m.session_id == "ses_v2_migrated")
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(messages[-1].role, "assistant")
        self.assertIn("[error]", messages[-1].text)
        self.assertIn("invalid API key", messages[-1].text)
        # the failed turn is still counted, so count and transcript stay aligned
        self.assertEqual(meta.message_count, len(messages))

    def test_v2_session_without_v2_rows_falls_back_to_legacy_transcript(self):
        conn = sqlite3.connect(self.tmp / "opencode.db")
        try:
            conn.execute(
                "INSERT INTO session_v2 (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                ("ses_v2_empty", "proj1", "/tmp/x", "χωρίς v2 μηνύματα", "{}", BASE_MS, BASE_MS),
            )
            conn.execute(
                "INSERT INTO session (id, project_id, directory, title, model, time_created, time_updated, time_archived)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                ("ses_v2_empty", "proj1", "/tmp/x", "χωρίς v2 μηνύματα", "{}", BASE_MS, BASE_MS),
            )
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
                ("msg_fb", "ses_v2_empty", BASE_MS, BASE_MS, '{"role":"user"}'),
            )
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
                ("prt_fb", "msg_fb", "ses_v2_empty", BASE_MS, BASE_MS, '{"type":"text","text":"από το v1"}'),
            )
            conn.commit()
        finally:
            conn.close()

        meta = next(m for m in self.adapter.list_conversations() if m.session_id == "ses_v2_empty")
        self.assertEqual([m.text for m in self.adapter.load_conversation(meta)], ["από το v1"])

    def test_delete_removes_session_from_both_schemas_and_cascades(self):
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == "ses_v2_migrated")
        self.adapter.delete_conversation(meta)

        ids = [c.session_id for c in self.adapter.list_conversations()]
        self.assertNotIn("ses_v2_migrated", ids)
        conn = sqlite3.connect(self.tmp / "opencode.db")
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM session_v2 WHERE id = ?", (meta.session_id,)).fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM session WHERE id = ?", (meta.session_id,)).fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM session_message WHERE session_id = ?", (meta.session_id,)
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
