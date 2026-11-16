from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message, derive_title_from_text, parse_iso8601
from ..registry import register
from .base import ToolAdapter

logger = logging.getLogger(__name__)

# Codex CLI (OpenAI) keeps one JSONL event stream per conversation under
# ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl.
# The first record is always `session_meta` (session id, cwd, cli_version);
# after that come `response_item` records carrying the actual turns, plus
# bookkeeping records (event_msg, turn_context, world_state, token_usage_record)
# that carry no transcript text.
#
# ~/.codex/session_index.jsonl holds {id, thread_name, updated_at} rows: the
# thread name is what codex itself shows as the title, so it wins over a title
# derived from the first user message.
SESSIONS_DIR_NAME = "sessions"
INDEX_FILE_NAME = "session_index.jsonl"
ROLLOUT_PREFIX = "rollout-"
_MAX_TOOL_OUTPUT = 500

# rollout-2026-11-16T16-31-53-01a1e4c5-8aae-75b1-97c6-3f40be88a479.jsonl
_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-fA-F-]{36})\.jsonl$"
)

# `developer` messages are injected instructions (sandbox policy, tool list),
# not something a human wrote, so they never belong in a transcript.
_ROLE_MAP = {"user": "user", "assistant": "assistant"}

# Codex writes these as literal "user" messages before the conversation starts
# (working directory, shell, timezone, repo instructions). They are real records
# and stay in the transcript, but they must never be picked as the title.
_SCAFFOLDING_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<INSTRUCTIONS>",
)


@dataclass
class _Rollout:
    """Everything one rollout file has to say, from a single pass."""

    session_id: str = ""
    cwd: str = ""
    model: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    first_user_text: str = ""
    messages: list[Message] = field(default_factory=list)


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping bad lines.

    Codex appends while a session runs, so a partially written trailing line is
    normal; it must be skipped rather than fail the whole conversation.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        logger.warning("Could not read codex session file %s", path)


def _block_texts(value: object) -> str:
    """Join the `text` field of a content-block list (or single block)."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ""
    texts = [
        block.get("text", "")
        for block in value
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(t for t in texts if t).strip()


def _reasoning_text(payload: dict) -> str:
    """Extract the human-readable reasoning summary.

    Current codex versions keep the raw chain of thought in
    `encrypted_content` and expose only a short plain-text `summary`; older
    ones put the text in `content`. Either may be absent or empty.
    """
    return _block_texts(payload.get("summary")) or _block_texts(payload.get("content"))


def _tool_output_text(output: object) -> str:
    """Codex writes tool output as a plain string, but some versions use a
    content-block list or a dict — normalise all of them to text."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        chunks: list[str] = []
        for block in output:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(block, str):
                chunks.append(block)
        return "\n".join(chunks)
    if isinstance(output, dict):
        text = output.get("text") or output.get("output")
        if isinstance(text, str):
            return text
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return str(output)


def _truncate(text: str) -> str:
    """Cap a tool output, saying so instead of dropping the tail silently."""
    text = text.strip()
    if len(text) <= _MAX_TOOL_OUTPUT:
        return text
    dropped = len(text) - _MAX_TOOL_OUTPUT
    return f"{text[:_MAX_TOOL_OUTPUT]} [… {dropped} chars truncated]"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return parse_iso8601(value)


def _parse_rollout(path: Path) -> _Rollout:
    """Single-pass parse: session metadata plus the flattened transcript.

    Codex emits one record per *event* — a reasoning summary, a tool call, a
    tool result and the final answer are separate records that all belong to
    one assistant turn. Every other adapter in this project models one
    `Message` per conversational turn (text blocks flattened into it), so the
    records are folded back the same way here: reasoning and tool records are
    appended to the assistant turn in progress. That keeps `message_count`
    comparable across tools and equal to the number of entries the transcript
    view shows.
    """
    rollout = _Rollout()
    tool_names: dict[str, str] = {}
    # The assistant turn currently being assembled.
    turn_text: list[str] = []
    turn_has_tool = False
    turn_ts: datetime | None = None

    def flush_turn() -> None:
        nonlocal turn_text, turn_has_tool, turn_ts
        if turn_text or turn_has_tool:
            rollout.messages.append(
                Message(
                    role="assistant",
                    timestamp=turn_ts,
                    text="\n".join(part for part in turn_text if part),
                    has_tool_call=turn_has_tool,
                    raw={},
                )
            )
        turn_text, turn_has_tool, turn_ts = [], False, None

    for obj in _read_jsonl(path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        record_type = obj.get("type")

        if record_type == "session_meta":
            sid = payload.get("session_id") or payload.get("id")
            if isinstance(sid, str) and sid:
                rollout.session_id = sid
            roots = payload.get("cwd") or payload.get("runtime_workspace_roots")
            if isinstance(roots, str):
                rollout.cwd = roots
            elif isinstance(roots, list) and roots and isinstance(roots[0], str):
                rollout.cwd = roots[0]
            # Fallback timestamps: a session aborted before its first turn still
            # has a real creation time in session_meta.
            meta_ts = _parse_timestamp(obj.get("timestamp"))
            if meta_ts is not None:
                rollout.created_at = rollout.created_at or meta_ts
                rollout.updated_at = rollout.updated_at or meta_ts
            continue

        if record_type == "turn_context":
            if isinstance(payload.get("model"), str):
                rollout.model = payload["model"]
            if not rollout.cwd and isinstance(payload.get("cwd"), str):
                rollout.cwd = payload["cwd"]
            continue

        if record_type != "response_item":
            continue

        item_type = payload.get("type")
        ts = _parse_timestamp(obj.get("timestamp"))
        if ts is not None:
            rollout.created_at = rollout.created_at or ts
            rollout.updated_at = ts

        if item_type == "message":
            role_raw = payload.get("role")
            role = _ROLE_MAP.get(role_raw) if isinstance(role_raw, str) else None
            if role is None:
                continue  # developer/system scaffolding
            text = _block_texts(payload.get("content"))
            if role == "user":
                flush_turn()  # a new prompt ends the previous assistant turn
                if not rollout.first_user_text and not text.strip().startswith(
                    _SCAFFOLDING_PREFIXES
                ):
                    rollout.first_user_text = text
                rollout.messages.append(
                    Message(role="user", timestamp=ts, text=text, has_tool_call=False, raw=payload)
                )
            elif text:
                turn_ts = turn_ts or ts
                turn_text.append(text)
            continue

        if item_type == "reasoning":
            text = _reasoning_text(payload)
            if not text:
                continue  # encrypted-only reasoning: nothing to show
            turn_ts = turn_ts or ts
            turn_text.append(f"[thinking] {text}")
            continue

        if item_type == "custom_tool_call":
            name = payload.get("name") or "?"
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                tool_names[call_id] = name
            turn_ts = turn_ts or ts
            turn_text.append(f"[tool_call: {name}]")
            turn_has_tool = True
            continue

        if item_type == "custom_tool_call_output":
            call_id = payload.get("call_id")
            name = tool_names.get(call_id, "?") if isinstance(call_id, str) else "?"
            output = _truncate(_tool_output_text(payload.get("output")))
            label = f"[tool_result: {name}]"
            if output:
                label += f" {output}"
            turn_ts = turn_ts or ts
            turn_text.append(label)
            turn_has_tool = True

    flush_turn()
    return rollout


@register
class CodexAdapter(ToolAdapter):
    """Adapter for the OpenAI Codex CLI (https://github.com/openai/codex).

    Storage layout (honouring `CODEX_HOME` when set):

        ~/.codex/
          session_index.jsonl                 # {id, thread_name, updated_at}
          sessions/2026/11/16/rollout-….jsonl  # one event stream per session

    A rollout file starts with a `session_meta` record carrying the session id
    and working directory, followed by `response_item` records for user and
    assistant messages, reasoning summaries and tool calls. Deleting a
    conversation removes its rollout file; codex's own `session_index.jsonl`
    is left untouched (a stale row there is harmless — it is only consulted
    for files that still exist).
    """

    tool_id = "codex"
    display_name_key = "tool.codex"
    env_var = "CODEX_HOME"

    @classmethod
    def default_base_dir(cls) -> Path:
        env = os.environ.get("CODEX_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".codex"

    def _sessions_dir(self) -> Path:
        return self.base_dir / SESSIONS_DIR_NAME

    def _index_titles(self) -> dict[str, str]:
        """session id → thread name, from session_index.jsonl (best-effort)."""
        titles: dict[str, str] = {}
        index_path = self.base_dir / INDEX_FILE_NAME
        if not index_path.exists():
            return titles
        for row in _read_jsonl(index_path):
            sid = row.get("id")
            name = row.get("thread_name")
            if isinstance(sid, str) and isinstance(name, str) and name.strip():
                titles[sid] = name.strip()
        return titles

    def _rollout_files(self) -> list[Path]:
        sessions_dir = self._sessions_dir()
        if not sessions_dir.exists():
            return []
        try:
            return sorted(sessions_dir.rglob(f"{ROLLOUT_PREFIX}*.jsonl"))
        except OSError:
            # An unreadable subdirectory must not hide every codex conversation.
            logger.warning("Could not scan codex sessions directory %s", sessions_dir)
            return []

    @staticmethod
    def _session_id_from_name(path: Path) -> str:
        match = _ROLLOUT_NAME_RE.match(path.name)
        return match.group(1) if match else ""

    def list_conversations(self) -> Iterator[ConversationMeta]:
        titles = self._index_titles()
        seen: set[str] = set()
        for path in self._rollout_files():
            try:
                rollout = _parse_rollout(path)
            except Exception:  # one unreadable file must not hide the rest
                logger.warning("Could not scan codex session file %s", path, exc_info=True)
                continue
            session_id = rollout.session_id or self._session_id_from_name(path)
            if not session_id or session_id in seen:
                # A resumed/forked rollout can repeat the same session id; the
                # first (oldest) file wins so ids stay unique for the UI keys.
                continue
            seen.add(session_id)
            # A codex thread name (when the user renamed the thread) wins over
            # a title derived from the first user message.
            title = titles.get(session_id, "")
            if not title and rollout.first_user_text:
                title = derive_title_from_text(rollout.first_user_text)
            yield ConversationMeta(
                tool_id=self.tool_id,
                session_id=session_id,
                title=title or "(untitled)",
                project_path=rollout.cwd,
                created_at=rollout.created_at,
                updated_at=rollout.updated_at,
                message_count=len(rollout.messages),
                primary_file=path,
                extra={"model": rollout.model},
            )

    def load_conversation(self, meta: ConversationMeta) -> list[Message]:
        if not meta.primary_file.exists():
            return []
        return _parse_rollout(meta.primary_file).messages

    def delete_conversation(self, meta: ConversationMeta) -> None:
        """Remove the rollout file.

        A missing file is a no-op (already deleted); any other OS error is
        propagated so the CLI/GUI report the failure instead of claiming the
        conversation was deleted while it is still on disk.
        """
        try:
            meta.primary_file.unlink()
        except FileNotFoundError:
            return
