from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message, derive_title_from_text, parse_iso8601
from ..registry import register
from ._content_blocks import flatten_anthropic_content
from .base import ToolAdapter

logger = logging.getLogger(__name__)

# Claude Code injects these as literal "user" message content for slash
# commands and local-command output/caveats — never text a human actually
# typed, so they must never be picked as the conversation's display title.
_SCAFFOLDING_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
)


def _is_real_user_text(obj: dict, text: str, has_tool_call: bool) -> bool:
    if obj.get("isMeta"):
        return False
    if has_tool_call:
        return False  # a tool_result block being returned, not typed text
    return not text.strip().startswith(_SCAFFOLDING_PREFIXES)


@register
class ClaudeCodeAdapter(ToolAdapter):
    tool_id = "claude_code"
    display_name_key = "tool.claude_code"
    env_var = None

    @classmethod
    def default_base_dir(cls) -> Path:
        return Path.home() / ".claude"

    def list_conversations(self) -> Iterator[ConversationMeta]:
        projects_dir = self.base_dir / "projects"
        if not projects_dir.exists():
            return
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_path in project_dir.glob("*.jsonl"):
                meta = self._scan_file(jsonl_path)
                if meta is not None:
                    yield meta

    def _scan_file(self, path: Path) -> ConversationMeta | None:
        count = 0
        first_ts = last_ts = None
        cwd = None
        ai_title = None
        custom_title = None
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
                    if obj_type in ("user", "assistant"):
                        count += 1
                        ts = parse_iso8601(obj.get("timestamp"))
                        if ts:
                            first_ts = first_ts or ts
                            last_ts = ts
                        cwd = cwd or obj.get("cwd")
                        if obj_type == "user" and first_user_text is None:
                            content = obj.get("message", {}).get("content")
                            text, has_tool_call = flatten_anthropic_content(content)
                            if text and _is_real_user_text(obj, text, has_tool_call):
                                first_user_text = text
                    # A session can carry several ai-title/custom-title lines over its
                    # life (e.g. renamed later) — always keep the latest one seen.
                    elif obj_type == "ai-title":
                        ai_title = obj.get("aiTitle") or ai_title
                    elif obj_type == "custom-title":
                        custom_title = obj.get("customTitle") or custom_title
        except OSError:
            logger.warning("Could not read %s", path, exc_info=True)
            return None

        if count == 0 and ai_title is None and custom_title is None:
            return None

        title = custom_title or ai_title or derive_title_from_text(first_user_text or "")
        return ConversationMeta(
            tool_id=self.tool_id,
            session_id=path.stem,
            title=title,
            project_path=cwd or str(path.parent),
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
                    role = message.get("role", obj_type)
                    text, has_tool_call = flatten_anthropic_content(message.get("content"))
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
