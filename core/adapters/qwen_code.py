from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message, derive_title_from_text, parse_iso8601
from ..registry import register
from .base import ToolAdapter

logger = logging.getLogger(__name__)


def _flatten_parts(parts) -> tuple[str, bool]:
    if not isinstance(parts, list):
        return "", False
    text_parts: list[str] = []
    has_tool_call = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "functionCall" in part:
            has_tool_call = True
            name = part.get("functionCall", {}).get("name", "?")
            text_parts.append(f"[tool_call: {name}]")
        elif part.get("thought"):
            thought_text = part.get("text", "")
            if thought_text:
                text_parts.append(f"[thinking] {thought_text}")
        elif "text" in part:
            text_parts.append(part.get("text", ""))
    return "\n".join(p for p in text_parts if p), has_tool_call


@register
class QwenCodeAdapter(ToolAdapter):
    tool_id = "qwen_code"
    display_name_key = "tool.qwen_code"
    env_var = "QWEN_HOME"

    @classmethod
    def default_base_dir(cls) -> Path:
        env = os.environ.get("QWEN_HOME")
        if env:
            return Path(env)
        home = Path.home() if os.path.expanduser("~") != "~" else Path(tempfile.gettempdir())
        return home / ".qwen"

    def list_conversations(self) -> Iterator[ConversationMeta]:
        projects_dir = self.base_dir / "projects"
        if not projects_dir.exists():
            return
        for project_dir in projects_dir.iterdir():
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            for jsonl_path in chats_dir.glob("*.jsonl"):
                meta = self._scan_file(jsonl_path)
                if meta is not None:
                    yield meta

    def _scan_file(self, path: Path) -> ConversationMeta | None:
        count = 0
        first_ts = last_ts = None
        cwd = None
        first_user_text = None
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    obj_type = obj.get("type")
                    if obj_type not in ("user", "assistant"):
                        continue
                    count += 1
                    ts = parse_iso8601(obj.get("timestamp"))
                    if ts:
                        first_ts = first_ts or ts
                        last_ts = ts
                    cwd = cwd or obj.get("cwd")
                    if obj_type == "user" and first_user_text is None:
                        text, _ = _flatten_parts(obj.get("message", {}).get("parts"))
                        if text:
                            first_user_text = text
        except OSError:
            logger.warning("Could not read %s", path, exc_info=True)
            return None

        if count == 0:
            return None

        return ConversationMeta(
            tool_id=self.tool_id,
            session_id=path.stem,
            title=derive_title_from_text(first_user_text or ""),
            project_path=cwd or str(path.parent.parent),
            created_at=first_ts,
            updated_at=last_ts,
            message_count=count,
            primary_file=path,
        )

    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        messages: list[Message] = []
        try:
            with meta.primary_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    obj_type = obj.get("type")
                    if obj_type not in ("user", "assistant"):
                        continue
                    message = obj.get("message", {})
                    role = "assistant" if message.get("role") == "model" else message.get("role", obj_type)
                    text, has_tool_call = _flatten_parts(message.get("parts"))
                    messages.append(
                        Message(
                            role=role,
                            timestamp=parse_iso8601(obj.get("timestamp")),
                            text=text,
                            has_tool_call=has_tool_call,
                            raw=obj,
                        )
                    )
        except OSError:
            logger.warning("Could not load %s", meta.primary_file, exc_info=True)
        return messages

    def delete_conversation(self, meta: ConversationMeta) -> None:
        meta.primary_file.unlink(missing_ok=True)
