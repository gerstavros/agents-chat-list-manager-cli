from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message, parse_iso8601
from ..registry import register
from ._content_blocks import flatten_anthropic_content
from .base import ToolAdapter

logger = logging.getLogger(__name__)


@register
class CodewhaleTuiAdapter(ToolAdapter):
    tool_id = "codewhale_tui"
    display_name_key = "tool.codewhale_tui"
    env_var = "CODEWHALE_HOME"

    @classmethod
    def default_base_dir(cls) -> Path:
        env = os.environ.get("CODEWHALE_HOME")
        return Path(env) if env else Path.home() / ".codewhale"

    def list_conversations(self) -> Iterator[ConversationMeta]:
        sessions_dir = self.base_dir / "sessions"
        if not sessions_dir.exists():
            return
        for json_path in sessions_dir.glob("*.json"):
            meta = self._scan_file(json_path)
            if meta is not None:
                yield meta

    def _scan_file(self, path: Path) -> ConversationMeta | None:
        session_dir = path.parent / path.stem
        sidecars = (session_dir,) if session_dir.is_dir() else ()
        try:
            with path.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not parse %s", path, exc_info=True)
            return ConversationMeta(
                tool_id=self.tool_id,
                session_id=path.stem,
                title="(unreadable session file)",
                project_path="",
                created_at=None,
                updated_at=None,
                message_count=0,
                primary_file=path,
                sidecar_paths=sidecars,
                extra={"corrupt": True},
            )

        metadata = doc.get("metadata", {})
        return ConversationMeta(
            tool_id=self.tool_id,
            session_id=metadata.get("id", path.stem),
            title=metadata.get("title") or "(untitled)",
            project_path=metadata.get("workspace", ""),
            created_at=parse_iso8601(metadata.get("created_at")),
            updated_at=parse_iso8601(metadata.get("updated_at")),
            message_count=metadata.get("message_count", 0),
            primary_file=path,
            sidecar_paths=sidecars,
            extra={"model": metadata.get("model")},
        )

    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        messages: list[Message] = []
        try:
            with meta.primary_file.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not load %s", meta.primary_file, exc_info=True)
            return messages

        for raw in doc.get("messages", []):
            role = raw.get("role", "unknown")
            text, has_tool_call = flatten_anthropic_content(raw.get("content"))
            messages.append(
                Message(
                    role=role,
                    timestamp=None,
                    text=text,
                    has_tool_call=has_tool_call,
                    raw=raw,
                )
            )
        return messages

    def delete_conversation(self, meta: ConversationMeta) -> None:
        meta.primary_file.unlink(missing_ok=True)
        for sidecar in meta.sidecar_paths:
            if sidecar.is_dir():
                shutil.rmtree(sidecar, ignore_errors=True)
            else:
                sidecar.unlink(missing_ok=True)
