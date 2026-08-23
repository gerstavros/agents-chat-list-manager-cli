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


def _ms_to_dt(ms: int | None) -> datetime | None:
    """Convert an opencode epoch-milliseconds value to an aware UTC datetime."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _flatten_part(data: dict) -> tuple[str, bool]:
    """Flatten one opencode part record (parsed JSON from the `part` table's
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


@register
class OpenCodeAdapter(ToolAdapter):
    """Adapter for opencode (https://opencode.ai).

    Unlike the other tools, opencode keeps its data in a SQLite database
    (`opencode.db`) instead of JSON files: the `session` table holds one row
    per conversation, `message` one row per chat message, and `part` one row
    per content block (text / reasoning / tool call / …) — with the actual
    payloads as JSON text in each row's `data` column. All timestamps are
    epoch milliseconds.
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

    def list_conversations(self) -> Iterator[ConversationMeta]:
        db_path = self._db_path()
        if not db_path.exists():
            return
        conn = None
        try:
            conn = self._connect_ro()
            if conn is None:
                return
            rows = conn.execute(
                "SELECT id, directory, title, model, time_created, time_updated,"
                " (SELECT COUNT(*) FROM message WHERE message.session_id = session.id) AS message_count"
                " FROM session"
                " WHERE time_archived IS NULL"
                " ORDER BY time_updated DESC"
            ).fetchall()
            for session_id, directory, title, model_raw, time_created, time_updated, message_count in rows:
                model_id = None
                if model_raw:
                    try:
                        model_id = json.loads(model_raw).get("id")
                    except (json.JSONDecodeError, AttributeError):
                        model_id = None
                yield ConversationMeta(
                    tool_id=self.tool_id,
                    session_id=session_id,
                    title=title or "(untitled)",
                    project_path=directory or "",
                    created_at=_ms_to_dt(time_created),
                    updated_at=_ms_to_dt(time_updated),
                    message_count=message_count or 0,
                    primary_file=db_path,
                    extra={"model": model_id},
                )
        except sqlite3.Error:
            logger.warning("Could not read opencode database %s", db_path)
        finally:
            if conn is not None:
                conn.close()

    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        db_path = self._db_path()
        if not db_path.exists():
            return []
        conn = None
        try:
            conn = self._connect_ro()
            if conn is None:
                return []
            message_rows = conn.execute(
                "SELECT id, time_created, data FROM message"
                " WHERE session_id = ? ORDER BY time_created, id",
                (meta.session_id,),
            ).fetchall()
            part_rows = conn.execute(
                "SELECT message_id, data FROM part"
                " WHERE session_id = ? ORDER BY time_created, id",
                (meta.session_id,),
            ).fetchall()

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
        except sqlite3.Error:
            logger.warning("Could not read opencode database %s", db_path)
            return []
        finally:
            if conn is not None:
                conn.close()

    def delete_conversation(self, meta: ConversationMeta) -> None:
        db_path = self._db_path()
        if not db_path.exists():
            return
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            with conn:
                # opencode's schema uses ON DELETE CASCADE for message/part.
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("DELETE FROM session WHERE id = ?", (meta.session_id,))
        except sqlite3.Error:
            logger.warning("Could not update opencode database %s", db_path)
        finally:
            if conn is not None:
                conn.close()
