"""Provision Lumen's release dependencies without changing shared Python packages."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from build_support import assert_free_space, step, utf8_env


def release_env() -> dict[str, str]:
    """Keep index/proxy settings, but prevent foreign Python/pip install paths."""
    env = utf8_env()
    for key in list(env):
        if key.upper() in {
            "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV",
            "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_REQUIREMENT",
            "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT",
        }:
            del env[key]
    env["PYTHONNOUSERSITE"] = "1"
    # A user's pip.ini may otherwise redirect installation outside the venv.
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def _run(command: list[str], root: Path, env: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=root, env=env)
    if result.returncode:
        raise SystemExit(
            "ERROR: release environment preparation failed. No freeze was started.\n"
            "Correct the error above and rerun python build_complete_release.py; "
            "the dedicated environment will be reused."
        )


def prepare_release_python(root: Path, explicit_python: str | None = None) -> str:
    """Use a caller's explicit interpreter, or create/update the owned venv.

    pip installs into the isolated environment on every invocation so a stale
    MCP major or a changed requirements file cannot silently survive reuse.
    Satisfied pins are reused; unrelated global packages are never installed.
    """
    if explicit_python:
        return explicit_python
    root = root.resolve()
    directory = root / ".venv-release"
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = release_env()
    step("Preparing the dedicated release Python")
    print(f"  environment: {directory}", flush=True)
    if directory.is_symlink() or directory.resolve().parent != root:
        raise SystemExit(f"ERROR: release environment must stay inside {root}")
    if not python.is_file():
        assert_free_space(root, 6.0, "release dependencies and build intermediates")
        _run([sys.executable, "-I", "-m", "venv", str(directory)], root, env)

    config = directory / "pyvenv.cfg"
    if not config.is_file() or "include-system-site-packages = false" not in (
        config.read_text(encoding="utf-8").lower()
    ):
        raise SystemExit(f"ERROR: {directory} must be a venv with system site packages disabled.")
    # Also check the executable really belongs to this environment before pip
    # can run (a stale or replaced executable must not install into the host).
    _run([
        str(python), "-I", "-c",
        "import pathlib,sys; "
        "assert sys.prefix != sys.base_prefix; "
        "assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve()",
        str(directory),
    ], root, env)
    _run([
        str(python), "-I", "-m", "pip", "--require-virtualenv", "install",
        "-r", str(root / "requirements-release.txt"),
    ], root, env)
    _run([str(python), "-I", "-m", "pip", "check"], root, env)
    return str(python)
