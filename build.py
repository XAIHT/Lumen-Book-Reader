# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""build.py - freeze Lumen and produce ``pkg.zip``.

Stage 1 of the three-stage release pipeline:

    python build.py               ->  pkg.zip            (this file)
    python build_uninstaller.py   ->  Uninstaller.exe
    python build_installer.py     ->  dist/Lumen_Release_v<ver>/

Or all three plus the distributable archive in one command:

    python build_complete_release.py

What this script does
---------------------
  1. Resolve the version (``--version`` > ``$LUMEN_VERSION`` > git tag >
     pyproject) and emit ``lumen_reader/_version.py`` + the Win32 VERSIONINFO
     resource, so the frozen ``Lumen.exe`` reports the same number everywhere.
  2. Run PyInstaller in ``--onedir --windowed`` mode over
     ``lumen_reader/launcher.py``.  ONEDIR IS MANDATORY: Lumen renders pages in
     QtWebEngine, whose helper process ``QtWebEngineProcess.exe`` cannot be
     re-launched out of a ``--onefile`` temp extraction reliably.
  3. Freeze the headless MCP sidecar as a separate console ``LumenMCP.exe``.
     It is deliberately one-file so it cannot collide with the GUI's ONEDIR
     ``_internal`` tree and so STDIO remains a real protocol pipe.
  4. Stage the payload - the frozen tree plus the MCP sidecar, PowerShell registrars, the
     icon, the licence and the shared preserve list - into ``dist/payload/``.
  5. Zip that staging tree into ``pkg.zip`` at the repo root, preserving empty
     directories, then delete ``build/`` and ``dist/`` so the next stage starts
     from a clean slate.

``pkg.zip`` is the single artefact the installer extracts.  Its contents ARE
the installed directory, one-for-one - no nesting, no surprises.
"""

from __future__ import annotations

import os
import importlib.metadata
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from build_support import (
    assert_free_space,
    banner,
    clean_directory,
    ensure_pyinstaller,
    step,
    utf8_env,
)
from versioning import (
    AUTHOR,
    PRODUCT_NAME,
    emit_build_artifacts,
    extract_cli_version,
    resolve_build_version,
    warn_if_tag_behind,
)

# pip's "A new release of pip is available" banner is pure build-log noise and
# is NOT fixable by upgrading: the build interpreter usually lives in a
# read-only Program Files prefix. Silence it here and in every child process.
os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
PAYLOAD_DIR = DIST_DIR / "payload"
PKG_ZIP = ROOT / "pkg.zip"

APP_NAME = "Lumen"                       # -> Lumen.exe
ENTRY_POINT = ROOT / "lumen_main.py"   # NOT launcher.py - see its docstring
MCP_NAME = "LumenMCP"                  # -> LumenMCP.exe, console/STDIO sidecar
MCP_ENTRY_POINT = ROOT / "lumen_mcp.py"
ICON_SRC = ROOT / "lumen_reader" / "assets" / "lumen.ico"

# Files copied into the payload root, beside Lumen.exe. These are exactly the
# files install.py and Uninstaller.exe expect to find in the install directory.
SUPPORT_FILES = [
    "CreateShortcut.ps1",
    "RemoveShortcut.ps1",
    "register_associations.ps1",
    "unregister_associations.ps1",
    "preserved_user_state.json",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
]

# Modules PyInstaller's static analysis cannot see. NLTK reaches for its
# corpora reader by name at call time, and PySide6's WebEngine trio is only
# imported inside ui.py's function bodies on some paths.
HIDDEN_IMPORTS = [
    "nltk.corpus",
    "nltk.corpus.reader.wordnet",
    "nltk.tokenize",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtPrintSupport",
    "bs4",
    "fitz",
]

# Qt ships far more than a book reader needs, and NLTK's optional integrations
# reach further still. Everything named here was PROVEN unnecessary before it
# was excluded, not guessed at:
#
#   * The Qt modules were checked against lumen_reader's imports one by one.
#   * The scientific stack is the expensive one. Importing `nltk.corpus` pulls
#     numpy, pandas, scipy and scikit-learn, and PyInstaller's hooks then follow
#     those into torch, torchvision, torchaudio, av and soundfile - several
#     gigabytes, for a reader that never imports any of them. NLTK guards those
#     integrations behind try/except, so WordNet keeps working without them;
#     that was verified by blocking every one of these packages and confirming
#     `lookup_offline_wordnet_entries("book")` still returned definitions.
EXCLUDES = [
    # Qt modules Lumen does not use
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtQuick3D",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    # The scientific stack, reached only through NLTK's optional integrations
    "torch", "torchvision", "torchaudio", "tensorflow", "keras",
    "pandas", "numpy", "scipy", "sklearn", "joblib", "pyarrow",
    "matplotlib", "av", "soundfile", "librosa", "gensim", "spacy",
    "sqlalchemy", "PIL", "cv2",
    # Developer tooling that has no business in a shipped reader
    "tkinter", "pytest", "setuptools", "pip", "IPython", "jupyter",
    "notebook", "pydoc_data", "lib2to3",
]


# ── Build-specific helpers (the shared ones live in build_support.py) ──

def assert_dependencies() -> None:
    """Fail early and by name, rather than deep inside PyInstaller's analysis."""
    step("Verifying runtime dependencies are importable")
    required = {
        "PySide6": "PySide6",
        "PySide6.QtWebEngineWidgets": "PySide6 (QtWebEngine)",
        "bs4": "beautifulsoup4",
        "nltk": "nltk",
        "fitz": "PyMuPDF",
        "mcp": "mcp",
        "regex": "regex",
    }
    missing = []
    for module, package in required.items():
        probe = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            print(f"  [OK]   {module}")
        else:
            print(f"  [MISS] {module}  (pip install {package})")
            missing.append(package)
    if missing:
        sys.exit(
            "\nERROR: cannot freeze Lumen - missing: " + ", ".join(sorted(set(missing)))
            + f"\nInstall them into THIS interpreter:\n"
            f'    "{sys.executable}" -m pip install -r requirements.txt'
        )
    try:
        mcp_major = int(importlib.metadata.version("mcp").split(".", 1)[0])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        mcp_major = 0
    if mcp_major != 2:
        sys.exit(
            "\nERROR: release packaging requires the pinned MCP Python SDK 2.x.\n"
            f'Install it into THIS interpreter:\n    "{sys.executable}" -m pip install -r requirements.txt'
        )

def stage_payload(version: str) -> Path:
    """Copy the frozen tree plus the support files into ``dist/payload``."""
    step("Staging the installation payload")
    frozen = DIST_DIR / APP_NAME
    if not frozen.is_dir():
        sys.exit(f"ERROR: PyInstaller produced no {frozen}")

    clean_directory(PAYLOAD_DIR)
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # The frozen tree's CONTENTS become the install directory's contents.
    for entry in frozen.iterdir():
        dst = PAYLOAD_DIR / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dst)
        else:
            shutil.copy2(entry, dst)
    print(f"  copied the frozen {APP_NAME} tree")

    mcp_executable = DIST_DIR / f"{MCP_NAME}.exe"
    if not mcp_executable.is_file():
        sys.exit(f"ERROR: PyInstaller produced no {mcp_executable}")
    shutil.copy2(mcp_executable, PAYLOAD_DIR / mcp_executable.name)
    print(f"  copied the headless {MCP_NAME}.exe sidecar")

    # The standalone .ico: shortcuts, ProgID DefaultIcon and the ARP entry all
    # point at it, and a real .ico renders sharper at 16px than an extracted
    # exe resource does.
    if ICON_SRC.is_file():
        shutil.copy2(ICON_SRC, PAYLOAD_DIR / "lumen.ico")
        print("  copied lumen.ico")
    else:
        print(f"  WARNING: {ICON_SRC} not found - shortcuts will use the exe icon")

    for name in SUPPORT_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, PAYLOAD_DIR / name)
            print(f"  copied {name}")
        else:
            print(f"  WARNING: {name} not found at the repo root - skipping")

    # A stamp the user (and any support conversation) can read without tools.
    (PAYLOAD_DIR / "VERSION.txt").write_text(
        f"{PRODUCT_NAME} {version}\n"
        f"Created by {AUTHOR}\n"
        f"Built {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        encoding="utf-8",
    )

    # Directories that must exist in a fresh installation even though they
    # start out empty. Written as explicit zip entries below.
    (PAYLOAD_DIR / "logs").mkdir(exist_ok=True)

    return PAYLOAD_DIR


def make_pkg_zip(payload: Path) -> Path:
    """Zip the staging tree into ``pkg.zip``, empty directories included."""
    step(f"Creating {PKG_ZIP.name}")
    if PKG_ZIP.exists():
        PKG_ZIP.unlink()
        print(f"Removed old {PKG_ZIP.name}")

    file_count = 0
    dir_count = 0
    with zipfile.ZipFile(PKG_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(payload):
            root_path = Path(root)
            # An empty directory has no member to carry it, so write one
            # explicitly - otherwise `logs/` silently vanishes on extraction.
            if not files and not dirs:
                arc = str(root_path.relative_to(payload)).replace("\\", "/") + "/"
                if arc != "./":
                    zf.write(root, arc)
                    dir_count += 1
            for name in files:
                full = root_path / name
                zf.write(full, full.relative_to(payload))
                file_count += 1

    size_mb = PKG_ZIP.stat().st_size / (1024 * 1024)
    print(f"Added {file_count} files and {dir_count} empty directories")
    print(f"{PKG_ZIP.name} created successfully ({size_mb:.1f} MB)")
    return PKG_ZIP


def verify_pkg_zip(zip_path: Path) -> None:
    """PROVE the package is installable - never merely claim it.

    An installer that extracts a package missing ``Lumen.exe`` or a registrar
    script fails at the user's machine, minutes into a 500 MB extraction. It is
    far cheaper to fail here.
    """
    step("Verifying the package payload")
    required = {
        f"{APP_NAME}.exe",
        f"{MCP_NAME}.exe",
        "lumen.ico",
        "CreateShortcut.ps1",
        "RemoveShortcut.ps1",
        "register_associations.ps1",
        "unregister_associations.ps1",
        "preserved_user_state.json",
        "VERSION.txt",
    }
    with zipfile.ZipFile(zip_path) as zf:
        names = {n.replace("\\", "/") for n in zf.namelist()}
        bad = zf.testzip()
    if bad is not None:
        sys.exit(f"ABORT: {zip_path.name} is corrupt - first bad member: {bad}")

    missing = sorted(n for n in required if n not in names)
    if missing:
        sys.exit(
            f"ABORT: {zip_path.name} is missing required members: {', '.join(missing)}"
        )
    has_internal = any(n.startswith("_internal/") for n in names)
    if not has_internal:
        sys.exit(
            f"ABORT: {zip_path.name} has no _internal/ tree - the PyInstaller "
            f"--onedir payload did not make it into the package."
        )
    print(f"  [OK] all {len(required)} required members present, archive intact")
    print(f"  [OK] _internal/ tree present ({sum(1 for n in names if n.startswith('_internal/'))} members)")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    start = time.time()

    cli_version = extract_cli_version(sys.argv)
    version = resolve_build_version(cli_version)

    banner(f"LUMEN BUILD  ·  v{version}")
    print(f"repo        : {ROOT}")
    print(f"python      : {sys.executable}")
    print(f"entry point : {ENTRY_POINT.relative_to(ROOT)}")
    print(f"MCP entry   : {MCP_ENTRY_POINT.relative_to(ROOT)}")
    warn_if_tag_behind(version)

    if not ENTRY_POINT.is_file():
        sys.exit(f"ERROR: entry point not found: {ENTRY_POINT}")
    if not MCP_ENTRY_POINT.is_file():
        sys.exit(f"ERROR: MCP entry point not found: {MCP_ENTRY_POINT}")

    step("Checking there is room to build")
    # Measured from a real run: ~600 MB frozen tree, the same again staged into
    # dist/payload, plus pkg.zip and PyInstaller's own build/ scratch.
    assert_free_space(ROOT, 4.0, "the frozen app, the staged payload and pkg.zip")

    assert_dependencies()
    ensure_pyinstaller()

    step("Emitting version artefacts")
    version_file = emit_build_artifacts(version)
    print(f"  lumen_reader/_version.py  ->  {version}")
    print(f"  VERSIONINFO resource      ->  {version_file.name}")

    step("Cleaning previous build artefacts")
    clean_directory(BUILD_DIR / APP_NAME)
    clean_directory(BUILD_DIR / MCP_NAME)
    clean_directory(DIST_DIR / APP_NAME)
    clean_directory(PAYLOAD_DIR)
    spec = ROOT / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()
        print(f"Removed: {spec}")
    mcp_spec = ROOT / f"{MCP_NAME}.spec"
    if mcp_spec.exists():
        mcp_spec.unlink()
        print(f"Removed: {mcp_spec}")

    command = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",            # QtWebEngine cannot live in a --onefile bundle
        "--windowed",          # no console window behind the reader
        "--noconfirm",
        "--noupx",             # UPX-packed Qt DLLs trip antivirus heuristics
        "--name", APP_NAME,
        f"--version-file={version_file}",
        # NOT --collect-all nltk: that walks every submodule, including the
        # optional scikit-learn / torch integrations, and turns a 300 MB reader
        # into a multi-gigabyte one. Lumen uses exactly one corner of NLTK -
        # the WordNet corpus reader - so collect that corner and its data files.
        "--collect-submodules", "nltk.corpus",
        "--collect-data", "nltk",
        "--collect-data", "lumen_reader",
        # PyInstaller anchors its search path on the ENTRY SCRIPT's directory.
        # Naming the repo root explicitly keeps `lumen_reader` importable no
        # matter where the entry script is moved to.
        "--paths", str(ROOT),
    ]
    if ICON_SRC.is_file():
        command += [f"--icon={ICON_SRC}"]
    for mod in HIDDEN_IMPORTS:
        command += ["--hidden-import", mod]
    for mod in EXCLUDES:
        command += ["--exclude-module", mod]
    command.append(str(ENTRY_POINT))

    step("Running PyInstaller")
    print("$ " + " ".join(command))
    result = subprocess.run(command, cwd=str(ROOT), env=utf8_env())
    if result.returncode != 0:
        sys.exit(f"\nPyInstaller FAILED after {time.time() - start:.0f}s")

    exe = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    if not exe.is_file():
        sys.exit(f"ERROR: expected output not found: {exe}")
    print(f"\n{APP_NAME}.exe built: {exe} ({exe.stat().st_size / (1024 * 1024):.1f} MB)")

    step("Running PyInstaller for the MCP sidecar")
    mcp_command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--noconfirm",
        "--noupx",
        "--name", MCP_NAME,
        f"--version-file={version_file}",
        "--paths", str(ROOT),
        "--collect-submodules", "mcp",
        # SDK v2 moved the protocol types out of mcp into their own top-level
        # distribution, so collecting mcp alone no longer reaches them.
        "--collect-submodules", "mcp_types",
        "--collect-submodules", "nltk.corpus",
        "--collect-data", "nltk",
        "--collect-data", "lumen_reader",
        "--hidden-import", "regex",
        "--hidden-import", "bs4",
        "--hidden-import", "fitz",
        "--hidden-import", "nltk.corpus.reader.wordnet",
    ]
    for module in [*EXCLUDES, "PySide6"]:
        mcp_command += ["--exclude-module", module]
    mcp_command.append(str(MCP_ENTRY_POINT))
    if ICON_SRC.is_file():
        mcp_command.insert(-1, f"--icon={ICON_SRC}")
    print("$ " + " ".join(mcp_command))
    mcp_result = subprocess.run(mcp_command, cwd=str(ROOT), env=utf8_env())
    if mcp_result.returncode != 0:
        sys.exit(f"\nPyInstaller for {MCP_NAME} FAILED")
    mcp_exe = DIST_DIR / f"{MCP_NAME}.exe"
    if not mcp_exe.is_file():
        sys.exit(f"ERROR: expected output not found: {mcp_exe}")
    print(f"{MCP_NAME}.exe built: {mcp_exe} ({mcp_exe.stat().st_size / (1024 * 1024):.1f} MB)")

    payload = stage_payload(version)
    zip_path = make_pkg_zip(payload)
    verify_pkg_zip(zip_path)

    step("Cleaning up intermediate artefacts")
    clean_directory(BUILD_DIR)
    clean_directory(DIST_DIR)
    if version_file.exists():
        version_file.unlink()
        print(f"Removed: {version_file.name}")
    if spec.exists():
        spec.unlink()
        print(f"Removed: {spec.name}")
    if mcp_spec.exists():
        mcp_spec.unlink()
        print(f"Removed: {mcp_spec.name}")

    banner(f"BUILD COMPLETE  ·  v{version}  ·  {time.time() - start:.0f}s")
    print(f"  package : {zip_path}  ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")
    print("  next    : python build_uninstaller.py  &&  python build_installer.py")
    print("  or just : python build_complete_release.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
