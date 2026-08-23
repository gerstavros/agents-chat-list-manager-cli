from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.base import ToolAdapter
    from .config import AppConfig

_ADAPTER_CLASSES: list[type] = []
_discovered = False


def register(cls: type) -> type:
    _ADAPTER_CLASSES.append(cls)
    return cls


def discover_adapters() -> None:
    global _discovered
    if _discovered:
        return
    from . import adapters as adapters_pkg

    # Explicit imports so registration works inside a frozen/PyInstaller binary too,
    # where modules live in the bundled PYZ archive and pkgutil.iter_modules can't
    # enumerate them via filesystem scanning (it silently finds nothing there).
    from .adapters import claude_code, codewhale_tui, opencode, qwen_code, zed  # noqa: F401

    try:
        for _, name, _ in pkgutil.iter_modules(adapters_pkg.__path__):
            if name == "base" or name.startswith("_"):
                continue
            importlib.import_module(f"{adapters_pkg.__name__}.{name}")
    except Exception:
        pass  # best-effort: picks up extra adapter files dropped in during source-mode dev
    _discovered = True


def get_adapter_classes() -> list[type]:
    discover_adapters()
    return list(_ADAPTER_CLASSES)


def build_adapters(config: "AppConfig") -> list["ToolAdapter"]:
    discover_adapters()
    adapters = []
    for cls in _ADAPTER_CLASSES:
        override = config.path_overrides.get(cls.tool_id)
        base_dir = Path(override) if override else None
        adapters.append(cls(base_dir=base_dir))
    return adapters
