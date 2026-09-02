"""Qt-free discovery of Lumen's durable runtime files.

The desktop application asks ``QStandardPaths`` for these locations.  The MCP
sidecar deliberately does not import Qt, but it must resolve the same files.
Explicit environment/CLI overrides are useful for tests and portable installs;
the platform defaults mirror the GUI's organization/application names.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    state_file: Path
    index_file: Path
    logs_dir: Path
    cache_dir: Path

    @classmethod
    def discover(
        cls,
        *,
        data_dir: str | Path | None = None,
        index_file: str | Path | None = None,
    ) -> "RuntimePaths":
        explicit_data = data_dir or os.environ.get("LUMEN_DATA_DIR")
        base = Path(explicit_data).expanduser() if explicit_data else _default_data_dir()
        base = base.resolve(strict=False)
        explicit_index = index_file or os.environ.get("LUMEN_INDEX_PATH")
        index = (Path(explicit_index).expanduser().resolve(strict=False)
                 if explicit_index else base / "library-index.db")
        return cls(
            data_dir=base,
            state_file=base / "reader-state.json",
            index_file=index,
            logs_dir=base / "logs",
            cache_dir=base / "mcp-cache",
        )

    def read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        # QStandardPaths.AppDataLocation is the roaming location on Windows.
        parent = os.environ.get("APPDATA")
        if parent:
            return Path(parent) / "Lumen Reader" / "Lumen"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lumen Reader" / "Lumen"
    else:
        parent = os.environ.get("XDG_DATA_HOME")
        if parent:
            return Path(parent) / "Lumen Reader" / "Lumen"
        return Path.home() / ".local" / "share" / "Lumen Reader" / "Lumen"
    return Path.home() / ".lumen-reader"
