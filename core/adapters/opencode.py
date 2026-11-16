from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message
from ..registry import register
from .base import ToolAdapter

logger = logging.getLogger(__name__)

DB_FILE_NAME = "opencode.db"

# opencode 1.x keeps everything in `session` / `message` / `part`.
# opencode 2.x keeps the same tables for compatibility but writes new data to
# `session_v2` / `session_message`, where a whole message (text, reasoning and
# tool blocks) is one `data` JSON document with a `seq` ordering column.
# Both schemas can coexist in one database — 2.x leaves migrated sessions in
# `session` — so the adapter detects which tables exist and reads accordingly.
_V2_SESSION_TABLE = "session_v2"
_V2_MESSAGE_TABLE = "session_message"

# `session_message.type` values that carry actual conversation text; the rest
# (idle, agent-switched, model-switched) are runtime markers with no content.
_V2_TEXT_TYPES = ("user", "assistant", "system", "synthetic", "compaction")


def _ms_to_dt(ms: int | None) -> datetime | None:
    """Convert an opencode epoch-milliseconds value to an aware UTC datetime."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _flatten_part(data: dict) -> tuple[str, bool]:
    """Flatten one opencode 1.x part record (parsed JSON from the `part` table's
    `data` column) into (text, has_tool_call)."""
    part_type = data.get("type")
    if part_type == "text":
        return data.get("text", ""), False
    if part_type == "reasoning":
        text = data.get("text", "")
        return (f"[thinking] {text}", False) if text else ("", False)
    if part_type == "tool":
        label = f"[tool_call: {data.get('tool', '?')}]"
        state = data.get("state") or {}
        if state.get("status") == "error":
            label += " [error]"
        return label, True
    return "", False


def _flatten_v2_block(block: dict) -> tuple[str, bool]:
    """Flatten one opencode 2.x assistant content block into (text, has_tool_call)."""
    block_type = block.get("type")
    if block_type == "text":
        return block.get("text", ""), False
    if block_type == "reasoning":
        text = block.get("text", "")
        return (f"[thinking] {text}", False) if text else ("", False)
    if block_type == "tool":
        label = f"[tool_call: {block.get('name', '?')}]"
        state = block.get("state") or {}
        if isinstance(state, dict) and state.get("status") == "error":
            label += " [error]"
        return label, True
    return "", False


def _error_text(error: object) -> str:
    """Render an opencode 2.x assistant error ({type, message}) as text.

    A failed turn has `content: []` plus an `error` object; without this it
    would flatten to nothing and the transcript would silently lose the fact
    that the model errored.
    """
    if not error:
        return ""
    if isinstance(error, dict):
        message = error.get("message") or error.get("type")
        if isinstance(message, str) and message.strip():
            return f"[error] {message.strip()}"
        return f"[error] {json.dumps(error, ensure_ascii=False, separators=(',', ':'))}"
    if isinstance(error, str):
        return f"[error] {error.strip()}"
    return "[error]"


def _v2_message_text(row_type: str, data: dict) -> tuple[str, bool]:
    """Flatten one opencode 2.x `session_message` row into (text, has_tool_call)."""
    if row_type == "user":
        return data.get("text", "") or "", False
    if row_type == "assistant":
        blocks = data.get("content")
        if isinstance(blocks, dict):
            blocks = [blocks]
        text_parts: list[str] = []
        has_tool_call = False
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            text, block_has_tool = _flatten_v2_block(block)
            if text:
                text_parts.append(text)
            has_tool_call = has_tool_call or block_has_tool
        if not text_parts and not has_tool_call:
            error = _error_text(data.get("error"))
            if error:
                return error, False
        return "\n".join(text_parts), has_tool_call
    # system / synthetic / compaction rows carry a single text (or summary) field
    for key in ("text", "summary"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, False
    return "", False


@register
class OpenCodeAdapter(ToolAdapter):
    """Adapter for opencode (https://opencode.ai).

    Unlike the other tools, opencode keeps its data in a SQLite database
    (`opencode.db`) instead of JSON files. All timestamps are epoch
    milliseconds.

    Supports both storage versions:

    * **1.x** — `session` holds one row per conversation, `message` one row per
      chat message, and `part` one row per content block (text / reasoning /
      tool call / …), with payloads as JSON text in each row's `data` column.
    * **2.x** — `session_v2` holds the conversations and `session_message` the
      transcript, one row per whole message (`type` = user / assistant /
      system / synthetic / compaction, ordered by `seq`). The 1.x tables are
      still present in a 2.x database and still hold sessions that have not
      been migrated, so both are read and their results are merged.
    """

    tool_id = "opencode"
    display_name_key = "tool.opencode"
    env_var = "OPENCODE_DATA"

    @classmethod
    def default_base_dir(cls) -> Path:
        env = os.environ.get("OPENCODE_DATA")
        if env:
            return Path(env)
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(base) / "opencode"
        # Linux/macOS: opencode follows the XDG data home, which defaults to
        # ~/.local/share — so the standard location is ~/.local/share/opencode.
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "opencode"

    def _db_path(self) -> Path:
        return self.base_dir / DB_FILE_NAME

    def _connect_ro(self) -> sqlite3.Connection | None:
        """Open the database read-only; return None if it's missing."""
        db_path = self._db_path()
        if not db_path.exists():
            return None
        # uri=True + as_uri() keeps paths with spaces/Windows drive letters safe.
        return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}

    def list_conversations(self) -> Iterator[ConversationMeta]:
        db_path = self._db_path()
        if not db_path.exists():
            return
        conn = None
        try:
            conn = self._connect_ro()
            if conn is None:
                return
            tables = self._tables(conn)
            if _V2_SESSION_TABLE in tables:
                yield from self._list_v2(conn, db_path, tables)
            if "session" in tables:
                yield from self._list_v1(conn, db_path, tables)
        except sqlite3.Error:
            logger.warning("Could not read opencode database %s", db_path)
        finally:
            if conn is not None:
                conn.close()

    def _list_v2(
        self, conn: sqlite3.Connection, db_path: Path, tables: set[str]
    ) -> Iterator[ConversationMeta]:
        count_sql = (
            f" (SELECT COUNT(*) FROM {_V2_MESSAGE_TABLE}"
            f" WHERE {_V2_MESSAGE_TABLE}.session_id = {_V2_SESSION_TABLE}.id"
            f" AND type IN {_V2_TEXT_TYPES}) AS message_count"
            if _V2_MESSAGE_TABLE in tables
            else " 0 AS message_count"
        )
        rows = conn.execute(
            f"SELECT id, directory, title, model, time_created, time_updated,{count_sql}"
            f" FROM {_V2_SESSION_TABLE}"
            f" WHERE time_archived IS NULL"
            f" ORDER BY time_updated DESC"
        ).fetchall()
        for session_id, directory, title, model_raw, time_created, time_updated, message_count in rows:
            yield ConversationMeta(
                tool_id=self.tool_id,
                session_id=session_id,
                title=title or "(untitled)",
                project_path=directory or "",
                created_at=_ms_to_dt(time_created),
                updated_at=_ms_to_dt(time_updated),
                message_count=message_count or 0,
                primary_file=db_path,
                extra={"model": _model_id(model_raw), "schema": "v2"},
            )

    def _list_v1(
        self, conn: sqlite3.Connection, db_path: Path, tables: set[str]
    ) -> Iterator[ConversationMeta]:
        if "session" not in tables:
            return
        # Sessions already present in session_v2 are skipped: after a 2.x
        # migration the same conversation would otherwise be listed twice.
        exclude = (
            f" AND id NOT IN (SELECT id FROM {_V2_SESSION_TABLE})"
            if _V2_SESSION_TABLE in tables
            else ""
        )
        count_sql = (
            " (SELECT COUNT(*) FROM message WHERE message.session_id = session.id) AS message_count"
            if "message" in tables
            else " 0 AS message_count"
        )
        rows = conn.execute(
            f"SELECT id, directory, title, model, time_created, time_updated,{count_sql}"
            f" FROM session"
            f" WHERE time_archived IS NULL"
            f"{exclude}"
            f" ORDER BY time_updated DESC"
        ).fetchall()
        for session_id, directory, title, model_raw, time_created, time_updated, message_count in rows:
            yield ConversationMeta(
                tool_id=self.tool_id,
                session_id=session_id,
                title=title or "(untitled)",
                project_path=directory or "",
                created_at=_ms_to_dt(time_created),
                updated_at=_ms_to_dt(time_updated),
                message_count=message_count or 0,
                primary_file=db_path,
                extra={"model": _model_id(model_raw), "schema": "v1"},
            )

    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        db_path = self._db_path()
        if not db_path.exists():
            return []
        conn = None
        try:
            conn = self._connect_ro()
            if conn is None:
                return []
            tables = self._tables(conn)
            if _V2_MESSAGE_TABLE in tables:
                messages = self._load_v2(conn, meta.session_id)
                if messages:
                    return messages
            return self._load_v1(conn, meta.session_id, tables)
        except sqlite3.Error:
            logger.warning("Could not read opencode database %s", db_path)
            return []
        finally:
            if conn is not None:
                conn.close()

    def _load_v2(self, conn: sqlite3.Connection, session_id: str) -> list[Message]:
        rows = conn.execute(
            f"SELECT type, time_created, data FROM {_V2_MESSAGE_TABLE}"
            f" WHERE session_id = ? AND type IN {_V2_TEXT_TYPES}"
            f" ORDER BY seq, time_created, id",
            (session_id,),
        ).fetchall()
        messages: list[Message] = []
        for row_type, time_created, data_raw in rows:
            try:
                data = json.loads(data_raw) if data_raw else {}
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            text, has_tool_call = _v2_message_text(row_type, data)
            # Every content-bearing row becomes exactly one Message, even when
            # it flattens to empty text — the listing's SQL count is per row,
            # so skipping here would make `message_count` over-report.
            messages.append(
                Message(
                    role=row_type if row_type in ("user", "assistant") else "system",
                    timestamp=_ms_to_dt(time_created),
                    text=text,
                    has_tool_call=has_tool_call,
                    raw=data,
                )
            )
        return messages

    def _load_v1(self, conn: sqlite3.Connection, session_id: str, tables: set[str]) -> list[Message]:
        if "message" not in tables:
            return []
        message_rows = conn.execute(
            "SELECT id, time_created, data FROM message"
            " WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        part_rows = (
            conn.execute(
                "SELECT message_id, data FROM part"
                " WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            ).fetchall()
            if "part" in tables
            else []
        )

        parts_by_message: dict[str, list[dict]] = {}
        for message_id, part_data in part_rows:
            try:
                part = json.loads(part_data) if part_data else {}
            except json.JSONDecodeError:
                continue
            if isinstance(part, dict):
                parts_by_message.setdefault(message_id, []).append(part)

        messages: list[Message] = []
        for message_id, time_created, data_raw in message_rows:
            try:
                data = json.loads(data_raw) if data_raw else {}
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            text_parts: list[str] = []
            has_tool_call = False
            for part in parts_by_message.get(message_id, []):
                part_text, part_has_tool = _flatten_part(part)
                if part_text:
                    text_parts.append(part_text)
                has_tool_call = has_tool_call or part_has_tool
            messages.append(
                Message(
                    role=data.get("role", "unknown"),
                    timestamp=_ms_to_dt(time_created),
                    text="\n".join(text_parts),
                    has_tool_call=has_tool_call,
                    raw=data,
                )
            )
        return messages

    def delete_conversation(self, meta: ConversationMeta) -> None:
        db_path = self._db_path()
        if not db_path.exists():
            return
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            with conn:
                # Both schemas use ON DELETE CASCADE for their child tables
                # (v1: message → part; v2: session_message, session_inbox,
                # session_pending), so deleting the session row is enough — but
                # the session can live in either table, or in both after a
                # partial migration, so both are cleared.
                conn.execute("PRAGMA foreign_keys = ON")
                tables = self._tables(conn)
                if _V2_SESSION_TABLE in tables:
                    conn.execute(
                        f"DELETE FROM {_V2_SESSION_TABLE} WHERE id = ?", (meta.session_id,)
                    )
                if "session" in tables:
                    conn.execute("DELETE FROM session WHERE id = ?", (meta.session_id,))
        except sqlite3.Error:
            logger.warning("Could not update opencode database %s", db_path)
        finally:
            if conn is not None:
                conn.close()


def _model_id(model_raw: object) -> str | None:
    """opencode stores the model as a JSON object in a text column."""
    if not model_raw:
        return None
    try:
        return json.loads(str(model_raw)).get("id")
    except (json.JSONDecodeError, AttributeError):
        return None
