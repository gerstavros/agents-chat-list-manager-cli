from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CLEANUP_KEY = "cleanupPeriodDays"
CLEANUP_VALUE = 36500
DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def ensure_claude_cleanup_period(path: Path | None = None) -> str:
    """Make sure Claude Code never auto-deletes old conversations by pinning
    ``cleanupPeriodDays`` in ``~/.claude/settings.json`` (the user-level
    settings file Claude Code reads itself).

    Idempotent: if the key already holds the target value, the file is left
    untouched. Other keys in the file are always preserved. Returns one of:

    - ``"created"``   settings file did not exist; created with the key
    - ``"updated"``   file existed; key was missing or had a different value
    - ``"unchanged"`` file already had the correct value; untouched
    - ``"skipped"``   file existed but is corrupt or not a JSON object; untouched
    """
    settings_path = path or DEFAULT_SETTINGS_PATH
    if not settings_path.exists():
        _write(settings_path, {CLEANUP_KEY: CLEANUP_VALUE})
        return "created"

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("%s is unreadable or corrupt; leaving it untouched", settings_path)
        return "skipped"
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object; leaving it untouched", settings_path)
        return "skipped"

    if data.get(CLEANUP_KEY) == CLEANUP_VALUE:
        return "unchanged"

    data[CLEANUP_KEY] = CLEANUP_VALUE
    _write(settings_path, data)
    return "updated"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
