from __future__ import annotations

import logging

from .adapters.base import ToolAdapter
from .models import ConversationMeta, Message

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, adapters: list[ToolAdapter]):
        self._adapters = {a.tool_id: a for a in adapters}

    def adapters(self) -> list[ToolAdapter]:
        return list(self._adapters.values())

    def list_all(self) -> list[ConversationMeta]:
        results: list[ConversationMeta] = []
        for adapter in self._adapters.values():
            try:
                results.extend(adapter.list_conversations())
            except Exception:
                logger.exception("Adapter %s failed to list conversations", adapter.tool_id)
                continue
        return results

    def load(self, meta: ConversationMeta) -> list[Message]:
        return self._adapters[meta.tool_id].load_conversation(meta)

    def delete(self, meta: ConversationMeta) -> None:
        self._adapters[meta.tool_id].delete_conversation(meta)
