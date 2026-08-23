from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "agentchatmanager"
XDG_DIR_NAME = "agentchatmanager"
CONFIG_FILE_NAME = "config.json"


def get_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / XDG_DIR_NAME


@dataclass
class AppConfig:
    language: str = "en"
    path_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "AppConfig":
        path = get_config_dir() / CONFIG_FILE_NAME
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Config file at %s is corrupt or unreadable, using defaults", path)
            return cls()
        return cls(
            language=data.get("language", "en"),
            path_overrides=dict(data.get("path_overrides", {})),
        )

    def save(self) -> None:
        config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / CONFIG_FILE_NAME
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    def set_path_override(self, tool_id: str, path: str | None) -> None:
        if path:
            self.path_overrides[tool_id] = path
        else:
            self.path_overrides.pop(tool_id, None)
