from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.adapters.codex import CodexAdapter

SESSION_ID = "01a1e4c5-8aae-75b1-97c6-3f40be88a479"
OTHER_ID = "01a1de98-459c-7203-8606-019055bb465d"


def _record(obj: dict, timestamp: str) -> str:
    return json.dumps({**obj, "timestamp": timestamp})


def _message(role: str, text: str) -> dict:
    """A `response_item` message record as codex writes it."""
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text, "annotations": [], "logprobs": []}],
        },
    }


def _meta(session_id: str = SESSION_ID, cwd: str = "/tmp/codex-project") -> list[str]:
    return [
        _record(
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": cwd,
                    "cli_version": "0.169.0",
                    "base_instructions": "IGNORED — must never show up in a transcript",
                },
            },
            "2026-01-02T03:04:05.000Z",
        )
    ]


def _rollout_lines() -> list[str]:
    return _meta() + [
        _record(
            {"type": "turn_context", "payload": {"model": "gpt-6-sol", "cwd": "/tmp/codex-project"}},
            "2026-01-02T03:04:05.100Z",
        ),
        _record(_message("developer", "sandbox policy: never edit /etc"), "2026-01-02T03:04:05.200Z"),
        _record(_message("user", "γεια, φτιάξε ένα script"), "2026-01-02T03:04:06.000Z"),
        _record(
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "ο χρήστης ζητά script"}],
                    "encrypted_content": "gAAAA-encrypted-blob",
                },
            },
            "2026-01-02T03:04:07.000Z",
        ),
        _record(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_abc",
                    "name": "shell",
                    "input": "echo hi",
                },
            },
            "2026-01-02T03:04:08.000Z",
        ),
        _record(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_abc",
                    "output": "hi\n",
                },
            },
            "2026-01-02T03:04:09.000Z",
        ),
        _record(_message("assistant", "έτοιμο"), "2026-01-02T03:04:10.000Z"),
    ]


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions = self.tmp / "sessions" / "2026" / "01" / "02"
        self.sessions.mkdir(parents=True)
        self.rollout = self.sessions / f"rollout-2026-01-02T03-04-05-{SESSION_ID}.jsonl"
        self.rollout.write_text("\n".join(_rollout_lines()) + "\n", encoding="utf-8")
        self.adapter = CodexAdapter(base_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_other(self, lines: list[str]) -> Path:
        path = self.sessions / f"rollout-2026-01-02T03-04-05-{OTHER_ID}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_list_reads_session_meta_and_counts_turns(self):
        convs = list(self.adapter.list_conversations())
        self.assertEqual(len(convs), 1)
        meta = convs[0]
        self.assertEqual(meta.tool_id, "codex")
        self.assertEqual(meta.session_id, SESSION_ID)
        self.assertEqual(meta.project_path, "/tmp/codex-project")
        self.assertEqual(meta.extra.get("model"), "gpt-6-sol")
        self.assertEqual(meta.primary_file, self.rollout)
        # one user turn + one assistant turn (reasoning/tool records folded in)
        self.assertEqual(meta.message_count, 2)
        self.assertEqual(meta.created_at.year, 2026)

    def test_message_count_matches_transcript_length(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.message_count, len(self.adapter.load_conversation(meta)))

    def test_title_derived_from_first_user_message(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.title, "γεια, φτιάξε ένα script")

    def test_thread_name_in_session_index_wins_over_derived_title(self):
        (self.tmp / "session_index.jsonl").write_text(
            json.dumps(
                {"id": SESSION_ID, "thread_name": "Επίλυση σύγκρουσης θύρας", "updated_at": "2026-01-02T03:05:00Z"}
            )
            + "\n",
            encoding="utf-8",
        )
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.title, "Επίλυση σύγκρουσης θύρας")

    def test_reasoning_and_tool_records_fold_into_one_assistant_turn(self):
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual([m.role for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0].text, "γεια, φτιάξε ένα script")
        assistant = messages[1]
        self.assertIn("[thinking] ο χρήστης ζητά script", assistant.text)
        self.assertIn("[tool_call: shell]", assistant.text)
        # tool output is labelled with the tool name resolved from call_id
        self.assertIn("[tool_result: shell]", assistant.text)
        self.assertIn("hi", assistant.text)
        self.assertTrue(assistant.text.endswith("έτοιμο"))
        self.assertTrue(assistant.has_tool_call)

    def test_consecutive_user_prompts_split_into_separate_turns(self):
        self.rollout.write_text(
            "\n".join(
                _meta()
                + [
                    _record(_message("user", "πρώτο"), "2026-01-02T03:04:06.000Z"),
                    _record(_message("assistant", "απάντηση ένα"), "2026-01-02T03:04:07.000Z"),
                    _record(_message("user", "δεύτερο"), "2026-01-02T03:04:08.000Z"),
                    _record(_message("assistant", "απάντηση δύο"), "2026-01-02T03:04:09.000Z"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        meta = next(iter(self.adapter.list_conversations()))
        messages = self.adapter.load_conversation(meta)
        self.assertEqual(
            [m.role for m in messages], ["user", "assistant", "user", "assistant"]
        )
        self.assertEqual([m.text for m in messages], ["πρώτο", "απάντηση ένα", "δεύτερο", "απάντηση δύο"])
        self.assertEqual(meta.message_count, 4)

    def test_developer_messages_are_excluded(self):
        meta = next(iter(self.adapter.list_conversations()))
        for message in self.adapter.load_conversation(meta):
            self.assertNotIn("sandbox policy", message.text)

    def test_reasoning_without_summary_is_omitted(self):
        self._write_other(
            _meta(OTHER_ID, cwd="/tmp/x")
            + [
                _record(_message("user", "μόνο σκέψη"), "2026-01-02T03:04:06.000Z"),
                _record(
                    {
                        "type": "response_item",
                        "payload": {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-x"},
                    },
                    "2026-01-02T03:04:07.000Z",
                ),
            ]
        )
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == OTHER_ID)
        messages = self.adapter.load_conversation(meta)
        self.assertEqual([m.text for m in messages], ["μόνο σκέψη"])
        self.assertEqual(meta.message_count, 1)

    def test_long_tool_output_is_truncated_with_a_marker(self):
        payload = "x" * 900
        self._write_other(
            _meta(OTHER_ID, cwd="/tmp/x")
            + [
                _record(_message("user", "τρέξε"), "2026-01-02T03:04:06.000Z"),
                _record(
                    {
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "shell"},
                    },
                    "2026-01-02T03:04:07.000Z",
                ),
                _record(
                    {
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": payload},
                    },
                    "2026-01-02T03:04:08.000Z",
                ),
            ]
        )
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == OTHER_ID)
        assistant = self.adapter.load_conversation(meta)[1]
        self.assertIn("chars truncated", assistant.text)
        self.assertNotIn("x" * 600, assistant.text)

    def test_environment_context_is_not_used_as_title(self):
        self._write_other(
            _meta(OTHER_ID, cwd="/tmp/x")
            + [
                _record(
                    _message("user", "<environment_context>\n  <cwd>/tmp/x</cwd>\n</environment_context>"),
                    "2026-01-02T03:04:06.000Z",
                ),
                _record(_message("user", "πραγματικό ερώτημα"), "2026-01-02T03:04:07.000Z"),
            ]
        )
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == OTHER_ID)
        self.assertEqual(meta.title, "πραγματικό ερώτημα")
        # ...but the scaffolding record is still part of the transcript
        texts = [m.text for m in self.adapter.load_conversation(meta)]
        self.assertTrue(texts[0].startswith("<environment_context>"))

    def test_session_with_only_scaffolding_is_untitled(self):
        self._write_other(
            _meta(OTHER_ID, cwd="/tmp/x")
            + [
                _record(
                    _message("user", "<environment_context><cwd>/tmp/x</cwd></environment_context>"),
                    "2026-01-02T03:04:06.000Z",
                )
            ]
        )
        meta = next(m for m in self.adapter.list_conversations() if m.session_id == OTHER_ID)
        self.assertEqual(meta.title, "(untitled)")

    def test_trailing_partial_line_is_skipped(self):
        # codex appends while running: a half-written last line is normal
        self.rollout.write_text(
            "\n".join(_rollout_lines()) + "\n" + '{"type":"response_item","payl',
            encoding="utf-8",
        )
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.message_count, 2)

    def test_missing_sessions_directory_yields_empty_results(self):
        empty = Path(tempfile.mkdtemp())
        try:
            adapter = CodexAdapter(base_dir=empty)
            self.assertEqual(list(adapter.list_conversations()), [])
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_session_id_falls_back_to_filename(self):
        self.rollout.write_text(
            "\n".join(line for line in _rollout_lines() if '"session_meta"' not in line) + "\n",
            encoding="utf-8",
        )
        meta = next(iter(self.adapter.list_conversations()))
        self.assertEqual(meta.session_id, SESSION_ID)

    def test_duplicate_session_ids_are_listed_once(self):
        # a resumed/forked rollout can repeat the session id in a second file
        dup = self.sessions / f"rollout-2026-01-02T04-04-05-{SESSION_ID}.jsonl"
        dup.write_text("\n".join(_rollout_lines()) + "\n", encoding="utf-8")
        convs = list(self.adapter.list_conversations())
        self.assertEqual([c.session_id for c in convs], [SESSION_ID])

    def test_delete_removes_rollout_file(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.adapter.delete_conversation(meta)
        self.assertFalse(self.rollout.exists())
        self.assertEqual(list(self.adapter.list_conversations()), [])

    def test_delete_missing_file_does_not_raise(self):
        meta = next(iter(self.adapter.list_conversations()))
        self.rollout.unlink()
        self.adapter.delete_conversation(meta)  # must not raise

    def test_delete_propagates_permission_error(self):
        """A failed delete must surface, not be reported as success."""
        meta = next(iter(self.adapter.list_conversations()))
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.adapter.delete_conversation(meta)

    def test_codex_home_env_var_overrides_default_base_dir(self):
        with mock.patch.dict("os.environ", {"CODEX_HOME": "/custom/codex-home"}):
            self.assertEqual(CodexAdapter.default_base_dir(), Path("/custom/codex-home"))


if __name__ == "__main__":
    unittest.main()
