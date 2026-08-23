from __future__ import annotations

from pathlib import Path

from .models import ConversationMeta, Message


def export_conversation(meta: ConversationMeta, messages: list[Message], path: Path, fmt: str) -> None:
    """fmt is 'markdown' or 'text'."""
    if fmt == "markdown":
        content = _render_markdown(meta, messages)
    else:
        content = _render_text(meta, messages)
    path.write_text(content, encoding="utf-8")


def _render_markdown(meta: ConversationMeta, messages: list[Message]) -> str:
    lines = [f"# {meta.title}", "", f"- Tool: {meta.tool_id}", f"- Project: {meta.project_path}", ""]
    for msg in messages:
        ts = msg.timestamp.isoformat() if msg.timestamp else ""
        header = f"## {msg.role}" + (f" — {ts}" if ts else "")
        lines.append(header)
        lines.append("")
        lines.append(msg.text)
        if msg.has_tool_call:
            lines.append("")
            lines.append("_[contains tool call]_")
        lines.append("")
    return "\n".join(lines)


def _render_text(meta: ConversationMeta, messages: list[Message]) -> str:
    lines = [meta.title, f"Tool: {meta.tool_id}", f"Project: {meta.project_path}", "=" * 40, ""]
    for msg in messages:
        ts = msg.timestamp.isoformat() if msg.timestamp else ""
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{prefix}{msg.role}:")
        lines.append(msg.text)
        lines.append("")
    return "\n".join(lines)
