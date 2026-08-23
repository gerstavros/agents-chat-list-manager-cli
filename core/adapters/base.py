from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message


class ToolAdapter(ABC):
    tool_id: str
    display_name_key: str
    env_var: str | None = None

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir if base_dir is not None else self.default_base_dir()

    @classmethod
    @abstractmethod
    def default_base_dir(cls) -> Path:
        ...

    def is_available(self) -> bool:
        return self.base_dir.exists()

    @abstractmethod
    def list_conversations(self) -> Iterator[ConversationMeta]:
        """Yield lightweight metadata for every conversation. Must never raise,
        even if base_dir is missing or unreadable — yield nothing instead."""

    @abstractmethod
    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        ...

    @abstractmethod
    def delete_conversation(self, meta: ConversationMeta) -> None:
        ...
