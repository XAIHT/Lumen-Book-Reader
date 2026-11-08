"""Qt-free discovery of Lumen's durable runtime files.

The desktop application and MCP sidecar resolve the same files without needing
Qt to discover them. Defaults mirror ``QStandardPaths`` on each platform.
Explicit environment/CLI overrides are useful for tests and portable installs;
the platform defaults mirror the GUI's organization/application names.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def scratch_directory() -> Path:
    """Book extraction/rendering scratch, never inside a Windows user profile."""
    override = os.environ.get("LUMEN_SCRATCH_DIR")
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "win32":
        # A separate top-level directory per user avoids depending on write
        # access to a shared directory first created by another account.
        identity = os.path.normcase(str(Path.home().resolve()))
        user_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        program_data = Path(os.environ.get(
            "ProgramData", os.environ.get("SystemDrive", "C:") + "/ProgramData"
        ))
        base = program_data / f"Lumen-Book-Reader-{user_key}" / "scratch"
    else:
        base = Path(tempfile.gettempdir()) / f"lumen-reader-{os.getuid()}"
    base = base.resolve()
    if sys.platform == "win32":
        profiles = {
            Path.home().resolve(),
            Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve(),
            Path(os.environ.get("SystemDrive", "C:") + "/Users").resolve(),
        }
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList") as key:
                directory, _ = winreg.QueryValueEx(key, "ProfilesDirectory")
            profiles.add(Path(os.path.expandvars(directory)).resolve())
        except (ImportError, OSError):
            pass
        if any(base.is_relative_to(profile_root) for profile_root in profiles):
            raise OSError("Lumen's scratch folder must be outside Windows user profiles.")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def configure_process_scratch() -> None:
    """Keep Qt and child-process temporary folders in the same owned location."""
    directory = str(scratch_directory())
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = directory
    tempfile.tempdir = directory


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
