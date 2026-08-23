from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..models import ConversationMeta, Message, parse_iso8601
from ..registry import register
from .base import ToolAdapter

logger = logging.getLogger(__name__)

# Zed stores agent threads in ~/.local/share/zed/threads/threads.db (Linux),
# one row per conversation; each row's `data` BLOB is a zstd-compressed JSON
# document. zstd is NOT in the Python stdlib, so decompression is done with
# zero third-party dependencies: the system libzstd via ctypes first, then a
# `zstd` CLI fallback. If neither exists, conversations still list (title /
# timestamps / project come from the DB columns) but transcripts are empty.
THREADS_DIR_NAME = "threads"
DB_FILE_NAME = "threads.db"
_MAX_DECOMPRESSED = 256 * 1024 * 1024
_MAX_TOOL_OUTPUT = 500
_ZSTD_CONTENTSIZE_ERROR = 0xFFFFFFFFFFFFFFFF
_ZSTD_CONTENTSIZE_UNKNOWN = 0xFFFFFFFFFFFFFFFE

_libzstd: object | None | bool = None  # None = not tried yet, False = unavailable


def _load_libzstd() -> object | None:
    global _libzstd
    if _libzstd is None:
        name = ctypes.util.find_library("zstd")
        if name:
            try:
                lib = ctypes.CDLL(name)
                getattr(lib, "ZSTD_decompress")  # sanity check the symbol exists
                _libzstd = lib
            except (OSError, AttributeError):
                _libzstd = False
        else:
            _libzstd = False
    return _libzstd or None


def _decompress_libzstd(data: bytes) -> bytes | None:
    lib = _load_libzstd()
    if lib is None:
        return None
    try:
        src = ctypes.c_char_p(data)
        lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        size = lib.ZSTD_getFrameContentSize(src, len(data))
        if size in (_ZSTD_CONTENTSIZE_ERROR, _ZSTD_CONTENTSIZE_UNKNOWN) or size > _MAX_DECOMPRESSED:
            return None
        if size == 0:
            return b""
        out = ctypes.create_string_buffer(size)
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        res = lib.ZSTD_decompress(out, size, src, len(data))
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        if lib.ZSTD_isError(res):
            return None
        return out.raw[:res]
    except (OSError, ValueError):
        return None


def _decompress_zstd_cli(data: bytes) -> bytes | None:
    try:
        proc = subprocess.run(["zstd", "-d", "-c"], input=data, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    if len(proc.stdout) > _MAX_DECOMPRESSED:
        return None
    return proc.stdout


def _decompress_zstd(data: bytes) -> bytes | None:
    """Best-effort zstd decompression with no third-party Python packages."""
    out = _decompress_libzstd(data)
    if out is None:
        out = _decompress_zstd_cli(data)
    return out


def has_zstd_decoder() -> bool:
    """Whether a zstd decoder is available (used by tests to skip transcript
    assertions on machines with neither libzstd nor the zstd CLI)."""
    return _load_libzstd() is not None or _decompress_zstd_cli(b"") is not None


def _parse_zed_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # Zed writes nanosecond-precision ISO timestamps; datetime.fromisoformat
    # only accepts microseconds on Python < 3.11, so truncate to 6 digits.
    if "." in value:
        head, frac = value.split(".", 1)
        tz = ""
        for sep in ("Z", "+", "-"):
            if sep in frac:
                frac, tz = frac.split(sep, 1)
                tz = sep + tz
                break
        value = f"{head}.{frac[:6]}{tz}"
    return parse_iso8601(value)


def _project_path(folder_paths: str | None) -> str:
    """Zed's folder_paths is usually a plain path string; guard against the
    JSON-array form newer versions may use for multi-root workspaces."""
    if not folder_paths:
        return ""
    text = folder_paths.strip()
    if text.startswith("["):
        try:
            arr = json.loads(text)
            return arr[0] if arr else ""
        except json.JSONDecodeError:
            return text
    return text


def _flatten_thread_message(payload: dict) -> tuple[str, bool]:
    """Flatten one Zed message payload (content blocks + tool_results dict)
    into (text, has_tool_call)."""
    blocks = payload.get("content", [])
    if isinstance(blocks, dict):
        blocks = [blocks]
    text_parts: list[str] = []
    has_tool_call = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if "Text" in block:
            text = block["Text"]
            if text:
                text_parts.append(text)
        elif "Thinking" in block:
            thinking = block["Thinking"]
            text = thinking.get("text", "") if isinstance(thinking, dict) else ""
            if text:
                text_parts.append(f"[thinking] {text}")
        elif "ToolUse" in block:
            has_tool_call = True
            tool_use = block["ToolUse"]
            name = tool_use.get("name", "?") if isinstance(tool_use, dict) else "?"
            text_parts.append(f"[tool_call: {name}]")
    for result in (payload.get("tool_results") or {}).values():
        if not isinstance(result, dict):
            continue
        name = result.get("tool_name", "?")
        out = result.get("output")
        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        elif out is None:
            out = ""
        else:
            out = str(out)
        out = out.strip()
        if out:
            out = out[: _MAX_TOOL_OUTPUT]
        label = f"[tool_result: {name}]"
        if result.get("is_error"):
            label = f"[tool_result: {name} ERROR]"
        if out:
            label += f" {out}"
        text_parts.append(label)
    return "\n".join(p for p in text_parts if p), has_tool_call


@register
class ZedAdapter(ToolAdapter):
    """Adapter for the Zed editor's Agent threads.

    Storage: `threads.db` under the Zed data dir (Linux: `~/.local/share/zed`,
    macOS: `~/Library/Application Support/Zed`, Windows: `%APPDATA%\\Zed`), a
    SQLite database whose `threads` table holds one row per conversation. The
    row's `data` BLOB is a zstd-compressed JSON document
    (`{title, model, updated_at, messages: [{User|Agent: {...}}, "Resume", ...]}`).
    """

    tool_id = "zed"
    display_name_key = "tool.zed"
    env_var = None

    @classmethod
    def default_base_dir(cls) -> Path:
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(base) / "Zed"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Zed"
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "zed"

    def _db_path(self) -> Path:
        return self.base_dir / THREADS_DIR_NAME / DB_FILE_NAME

    def _connect_ro(self) -> sqlite3.Connection | None:
        db_path = self._db_path()
        if not db_path.exists():
            return None
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
                "SELECT id, summary, folder_paths, created_at, updated_at, data_type, data"
                " FROM threads ORDER BY updated_at DESC"
            ).fetchall()
            for tid, summary, folder_paths, created_at, updated_at, data_type, blob in rows:
                doc = None
                decoder_missing = False
                if data_type == "zstd":
                    raw = _decompress_zstd(blob)
                    if raw is not None:
                        try:
                            doc = json.loads(raw)
                        except json.JSONDecodeError:
                            doc = None
                    else:
                        decoder_missing = True
                count = 0
                if isinstance(doc, dict):
                    count = sum(1 for m in doc.get("messages", []) if isinstance(m, dict))
                title = summary or ((doc or {}).get("title") or "(untitled)")
                model = None
                if isinstance(doc, dict):
                    mdl = doc.get("model")
                    if isinstance(mdl, dict):
                        model = mdl.get("model")
                yield ConversationMeta(
                    tool_id=self.tool_id,
                    session_id=tid,
                    title=title,
                    project_path=_project_path(folder_paths),
                    created_at=_parse_zed_ts(created_at),
                    updated_at=_parse_zed_ts(updated_at),
                    message_count=count,
                    primary_file=db_path,
                    extra={"model": model, "decoder_missing": decoder_missing},
                )
        except sqlite3.Error:
            logger.warning("Could not read zed threads database %s", db_path)
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
            row = conn.execute(
                "SELECT data_type, data FROM threads WHERE id = ?", (meta.session_id,)
            ).fetchone()
            if row is None or row[0] != "zstd":
                return []
            raw = _decompress_zstd(row[1])
            if raw is None:
                logger.warning(
                    "Could not decompress zed thread %s (no zstd decoder available)", meta.session_id
                )
                return []
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Could not parse zed thread %s", meta.session_id)
                return []
            if not isinstance(doc, dict):
                return []

            messages: list[Message] = []
            for m in doc.get("messages", []):
                if not isinstance(m, dict):
                    continue  # string markers such as "Resume"
                role = None
                payload = None
                for candidate in ("User", "Agent"):
                    if candidate in m:
                        role, payload = candidate, m[candidate]
                        break
                if role is None or not isinstance(payload, dict):
                    continue
                text, has_tool_call = _flatten_thread_message(payload)
                messages.append(
                    Message(
                        role="user" if role == "User" else "assistant",
                        # Zed threads carry no per-message timestamps, only
                        # thread-level created_at/updated_at.
                        timestamp=None,
                        text=text,
                        has_tool_call=has_tool_call,
                        raw=m,
                    )
                )
            return messages
        except sqlite3.Error:
            logger.warning("Could not read zed threads database %s", db_path)
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
                conn.execute("DELETE FROM threads WHERE id = ?", (meta.session_id,))
        except sqlite3.Error:
            logger.warning("Could not update zed threads database %s", db_path)
        finally:
            if conn is not None:
                conn.close()
