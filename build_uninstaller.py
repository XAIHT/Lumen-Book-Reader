# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""build_uninstaller.py - freeze ``uninstall.py`` into ``Uninstaller.exe``.

Stage 2 of the three-stage release pipeline:

    python build.py               ->  pkg.zip
    python build_uninstaller.py   ->  Uninstaller.exe    (this file)
    python build_installer.py     ->  dist/Lumen_Release_v<ver>/

``--onefile`` here, unlike everywhere else in this project. The uninstaller is
copied INTO the installation and must later delete the folder it is running
from; a single self-contained executable can be scheduled for deletion in one
step, whereas a ``--onedir`` uninstaller would have to delete its own
``_internal`` directory out from under itself while Windows holds those DLLs
open. It is a small Tkinter GUI with no QtWebEngine, so the one-file
extraction cost is a fraction of a second.

The result is left at the REPO ROOT, where ``build_installer.py`` picks it up
and moves it into the release folder.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from build_support import (
    banner,
    clean_directory,
    collect_python_dll_binaries,
    configure_tcl_tk,
    ensure_pyinstaller,
    run_step,
    step,
    utf8_env,
)
from versioning import (
    PRODUCT_NAME,
    extract_cli_version,
    render_versioninfo_for,
    resolve_build_version,
)

import subprocess

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "uninstall.py"
ICON_SRC = ROOT / "lumen_reader" / "assets" / "lumen.ico"
OUTPUT = ROOT / "Uninstaller.exe"


def main() -> int:
    start = time.time()

    cli_version = extract_cli_version(sys.argv)
    version = resolve_build_version(cli_version)

    banner(f"LUMEN UNINSTALLER BUILD  ·  v{version}")
    print(f"repo   : {ROOT}")
    print(f"python : {sys.executable}")
    print(f"source : {SOURCE.name}")

    if not SOURCE.is_file():
        sys.exit(f"ERROR: {SOURCE} not found.")

    version_file = render_versioninfo_for(
        version,
        ROOT / "Uninstaller.version.txt",
        product_name=f"{PRODUCT_NAME} Uninstaller",
        original_filename="Uninstaller.exe",
    )
    print(f"VERSIONINFO : {version_file.name}")

    run_step("Cleaning previous uninstaller artefacts", lambda: [
        clean_directory(ROOT / "build" / "Uninstaller"),
        clean_directory(ROOT / "dist" / "Uninstaller"),
    ])
    for stale in (ROOT / "dist" / "Uninstaller.exe", ROOT / "Uninstaller.spec", OUTPUT):
        if stale.exists():
            stale.unlink()
            print(f"Removed old: {stale}")

    ensure_pyinstaller()
    run_step("Configuring Tcl/Tk for the wizard GUI", configure_tcl_tk)
    dll_args = run_step("Collecting Python DLL binaries", collect_python_dll_binaries)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--noupx",
        "--name", "Uninstaller",
        f"--version-file={version_file}",
        "--hidden-import=_tkinter",
        "--collect-all", "tkinter",
        *dll_args,
    ]
    if ICON_SRC.is_file():
        command.append(f"--icon={ICON_SRC}")
    command.append(str(SOURCE))

    step("Running PyInstaller")
    print("$ " + " ".join(command))
    if subprocess.run(command, cwd=str(ROOT), env=utf8_env()).returncode != 0:
        sys.exit(f"\nPyInstaller FAILED after {time.time() - start:.0f}s")

    built = ROOT / "dist" / "Uninstaller.exe"
    if not built.is_file():
        sys.exit(f"ERROR: expected output not found: {built}")

    step("Placing Uninstaller.exe at the repo root")
    built.replace(OUTPUT)
    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    print(f"Uninstaller.exe ({size_mb:.1f} MB) -> {OUTPUT}")

    step("Cleaning up intermediate artefacts")
    clean_directory(ROOT / "build" / "Uninstaller")
    for cleanup in (ROOT / "Uninstaller.spec", version_file):
        if cleanup.exists():
            cleanup.unlink()
            print(f"Removed: {cleanup.name}")
    dist_dir = ROOT / "dist"
    if dist_dir.is_dir() and not any(dist_dir.iterdir()):
        dist_dir.rmdir()
        print(f"Removed empty: {dist_dir}")

    banner(f"UNINSTALLER BUILD COMPLETE  ·  v{version}  ·  {time.time() - start:.0f}s")
    print(f"  artefact : {OUTPUT}")
    print("  next     : python build_installer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
