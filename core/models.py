from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_title_from_text(text: str, max_len: int = 60) -> str:
    flat = " ".join(text.split())
    if not flat:
        return "(untitled)"
    return (flat[:max_len] + "…") if len(flat) > max_len else flat


@dataclass(frozen=True)
class ConversationMeta:
    tool_id: str
    session_id: str
    title: str
    project_path: str
    created_at: datetime | None
    updated_at: datetime | None
    message_count: int
    primary_file: Path
    sidecar_paths: tuple[Path, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def iid(self) -> str:
        return f"{self.tool_id}:{self.session_id}"


@dataclass(frozen=True)
class Message:
    role: str
    timestamp: datetime | None
    text: str
    has_tool_call: bool
    raw: dict
