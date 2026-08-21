# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Build-time entry point for Lumen's versioning system.

The implementation lives in ``lumen_reader/version.py`` (which is also the
*runtime* resolver, so the frozen app and the build scripts can never disagree).
This shim re-exports the public API for ``build.py`` /
``build_installer.py`` / ``build_uninstaller.py`` and adds the build-time
wrapper ``resolve_build_version()`` that handles the CLI-flag / env-var / git
fallback chain.

Precedence for the build version (highest to lowest):

  1. Explicit ``--version X.Y.Z`` CLI flag.
  2. ``$LUMEN_VERSION`` (``build.py`` exports it so all three artefacts -
     ``Lumen.exe``, ``Installer.exe``, ``Uninstaller.exe`` - share ONE version).
  3. ``git describe --tags --abbrev=0 --match 'v[0-9]*'``.
  4. The version declared in ``pyproject.toml``.
  5. ``0.0.0+unknown`` sentinel.

See ``RELEASING.md`` at the repo root for the full contract.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lumen_reader.version import (  # noqa: E402  (sys.path mutated above)
    ABOUT_URL,
    AUTHOR,
    COMPANY_NAME,
    COPYRIGHT,
    ENV_VAR_NAME,
    PRODUCT_NAME,
    UNKNOWN_VERSION,
    bump,
    compare,
    declared_version,
    derive_version_from_git,
    get_version,
    get_version_info,
    git_commit,
    parse_semver,
    public_version,
    render_pyinstaller_version_file,
    semver_to_win32_tuple,
    write_version_module,
)

# ── Path constants the build scripts share ─────────────────────────────────

REPO_ROOT = _REPO_ROOT
PACKAGE_ROOT = _REPO_ROOT / "lumen_reader"
VERSION_MODULE_PATH = PACKAGE_ROOT / "_version.py"
PYINSTALLER_VERSION_FILE = REPO_ROOT / "Lumen.version.txt"


# ── Build-time orchestrators ───────────────────────────────────────────────

def extract_cli_version(argv: list[str]) -> Optional[str]:
    """Pluck ``--version X.Y.Z`` (or ``--version=X.Y.Z``) out of *argv*.

    Mutates ``argv`` in place so a build script's own ``argparse`` (if any)
    never sees the flag.  Returns ``None`` when not present.
    """
    out: Optional[str] = None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--version" and i + 1 < len(argv):
            out = argv[i + 1].strip()
            del argv[i:i + 2]
            continue
        if token.startswith("--version="):
            out = token.split("=", 1)[1].strip()
            del argv[i]
            continue
        i += 1
    return out or None


def _sanitize_version(raw: str) -> str:
    """Drop a leading 'v'/'V'/space but otherwise pass the string through."""
    return (raw or "").lstrip(" vV").strip()


def resolve_build_version(cli_arg: Optional[str] = None) -> str:
    """Return the build version using the documented precedence.

    Pass the result of ``extract_cli_version(sys.argv)`` as *cli_arg*.
    """
    if cli_arg:
        return _sanitize_version(cli_arg)
    env_v = os.environ.get(ENV_VAR_NAME, "").strip()
    if env_v:
        return _sanitize_version(env_v)
    derived = derive_version_from_git()
    if derived and derived != "0.0.0":
        return derived
    declared = declared_version()
    if declared:
        return declared
    return derived or UNKNOWN_VERSION


def warn_if_tag_behind(version: str) -> Optional[str]:
    """Warn (loudly, but non-fatally) when git tags trail ``pyproject.toml``.

    The trap this closes: ``pyproject.toml`` says ``1.1.0`` while the newest
    tag is still ``v1.0.4``.  A tag-derived build would then quietly ship an
    installer stamped ``1.0.4`` for code that is demonstrably ``1.1.0``, and
    the "Installed apps" entry would lie to every user forever.  Returns the
    warning text (also printed) or ``None`` when everything lines up.
    """
    declared = declared_version()
    tagged = derive_version_from_git()
    if not declared or not tagged:
        return None
    if compare(declared, tagged) <= 0:
        return None
    msg = (
        f"WARNING: pyproject.toml declares {declared} but the newest git tag is "
        f"v{tagged}.\n"
        f"         This build is stamped {version}. If {declared} is the real "
        f"release, tag it first:\n"
        f"             git tag -a v{declared} -m \"Lumen {declared}\"\n"
        f"         (or re-run with --bump minor / --bump major, which tags for you)."
    )
    print("\n" + msg + "\n", flush=True)
    return msg


def emit_build_artifacts(
    version: str,
    *,
    product_name: str = PRODUCT_NAME,
    original_filename: str = "Lumen.exe",
) -> Path:
    """Write ``lumen_reader/_version.py`` and the Win32 VERSIONINFO file.

    Also exports ``LUMEN_VERSION`` so every downstream build script in the same
    shell picks up the identical value.  Returns the absolute path to the
    PyInstaller ``--version-file``.
    """
    public = public_version(version)
    commit_match = re.search(r"\+(?:g)?([0-9a-f]{7,})", version)
    commit = commit_match.group(1) if commit_match else git_commit()
    iso_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    write_version_module(
        VERSION_MODULE_PATH,
        version=public,
        build=version,
        commit=commit,
        date=iso_date,
    )

    PYINSTALLER_VERSION_FILE.write_text(
        render_pyinstaller_version_file(
            version,
            product_name=product_name,
            original_filename=original_filename,
        ),
        encoding="utf-8",
    )

    os.environ[ENV_VAR_NAME] = version
    return PYINSTALLER_VERSION_FILE


def render_versioninfo_for(
    version: str,
    target: Path,
    *,
    product_name: str,
    original_filename: str,
) -> Path:
    """Render a VERSIONINFO file at *target* (Installer / Uninstaller).

    Deliberately does NOT touch ``_version.py`` - that file is owned
    exclusively by ``build.py`` because it ends up inside the app bundle.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_pyinstaller_version_file(
            version,
            product_name=product_name,
            original_filename=original_filename,
        ),
        encoding="utf-8",
    )
    return target


def safe_version_for_path(version: str) -> str:
    """Sanitise a version for use inside a Windows directory name."""
    return "".join(
        c if (c.isalnum() or c in "._-") else "_"
        for c in (version or "").replace("+", "_")
    )


__all__ = [
    "ABOUT_URL",
    "AUTHOR",
    "COMPANY_NAME",
    "COPYRIGHT",
    "ENV_VAR_NAME",
    "PACKAGE_ROOT",
    "PRODUCT_NAME",
    "PYINSTALLER_VERSION_FILE",
    "REPO_ROOT",
    "UNKNOWN_VERSION",
    "VERSION_MODULE_PATH",
    "bump",
    "compare",
    "declared_version",
    "derive_version_from_git",
    "emit_build_artifacts",
    "extract_cli_version",
    "get_version",
    "get_version_info",
    "git_commit",
    "parse_semver",
    "public_version",
    "render_pyinstaller_version_file",
    "render_versioninfo_for",
    "resolve_build_version",
    "safe_version_for_path",
    "semver_to_win32_tuple",
    "warn_if_tag_behind",
    "write_version_module",
]
