# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""build_installer.py - freeze ``install.py`` and assemble the release folder.

Stage 3 of the three-stage release pipeline:

    python build.py               ->  pkg.zip
    python build_uninstaller.py   ->  Uninstaller.exe
    python build_installer.py     ->  dist/Lumen_Release_v<ver>/   (this file)

Two decisions in here are worth stating plainly, because both were paid for in
somebody's wasted afternoon:

**``--onedir``, and ``pkg.zip`` stays OUTSIDE the executable.**  Bundling a
half-gigabyte package into a ``--onefile`` installer means the bootloader
silently extracts it to ``%TEMP%`` on every launch. The window takes ten to
twenty seconds to appear, the user concludes nothing happened and
double-clicks again, and the second instance locks the first instance's DLLs.
With the package sitting beside the exe, the wizard opens instantly.

**Rename, do not copy.**  The release folder IS the PyInstaller output
directory, renamed. ``pkg.zip`` and ``Uninstaller.exe`` are MOVED into it under
SHA-256 verification. Copying would put a second half-gigabyte on the disk for
no reason and give a truncated copy somewhere to hide.

The finished folder holds exactly three things a user needs, and they must
travel together:

    Lumen_Release_v<version>/
        Installer.exe        <- double-click this
        _internal/           <- the frozen wizard's runtime
        pkg.zip              <- everything that gets installed
        Uninstaller.exe      <- copied into the installation by the wizard
        README-INSTALL.txt   <- what to do, in plain words
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from build_support import (
    assert_free_space,
    banner,
    clean_directory,
    collect_python_dll_binaries,
    configure_tcl_tk,
    ensure_pyinstaller,
    rename_with_retry,
    run_step,
    step,
    utf8_env,
    verified_move,
    write_asinvoker_manifest,
)
from versioning import (
    ABOUT_URL,
    AUTHOR,
    PRODUCT_NAME,
    extract_cli_version,
    render_versioninfo_for,
    resolve_build_version,
    safe_version_for_path,
)

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "install.py"
ICON_SRC = ROOT / "lumen_reader" / "assets" / "lumen.ico"
PKG_ZIP = ROOT / "pkg.zip"
UNINSTALLER = ROOT / "Uninstaller.exe"
PRESERVE_LIST = ROOT / "preserved_user_state.json"

README_TEMPLATE = """\
{product} {version}
{underline}

Created by {author}

INSTALL
    1. Keep these files together in one folder.
    2. Double-click  Installer.exe
    3. Choose where Lumen goes and where your books live, tick the file types
       you want Lumen to open, and press Install.

    No administrator rights are needed. Everything is installed for your user
    account only, under HKEY_CURRENT_USER.

WHAT IS IN THIS FOLDER
    Installer.exe      The installation wizard. Start here.
    _internal/         The wizard's own runtime. Do not delete or move it.
    pkg.zip            Lumen itself. The wizard needs it BESIDE Installer.exe.
    Uninstaller.exe    Copied into the installation so you can remove Lumen.

FILE TYPES
    The wizard offers .epub and .pdf as separate tick-boxes. Ticking a type
    adds Lumen to its "Open with" menu and gives it Lumen's icon; it does NOT
    take the type away from your current default app unless you also tick
    "make Lumen the default". You can change any of it later in
    Settings > Apps > Default apps.

UNINSTALL
    Settings > Apps > Installed apps > {product} > Uninstall
    or run Uninstaller.exe from the installation folder.

    Your books are never touched. Your reading positions, bookmarks, notes and
    tags are kept unless you tick the box that says otherwise.

{url}
"""


def write_release_readme(release_dir: Path, version: str) -> Path:
    title = f"{PRODUCT_NAME} {version}"
    path = release_dir / "README-INSTALL.txt"
    path.write_text(
        README_TEMPLATE.format(
            product=PRODUCT_NAME,
            version=version,
            underline="=" * len(title),
            author=AUTHOR,
            url=ABOUT_URL,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {path.name}")
    return path


def main() -> int:
    start = time.time()

    cli_version = extract_cli_version(sys.argv)
    version = resolve_build_version(cli_version)

    banner(f"LUMEN INSTALLER BUILD  ·  v{version}")
    print(f"repo   : {ROOT}")
    print(f"python : {sys.executable}")
    print(f"source : {SOURCE.name}")

    # ── 0) Prerequisites ────────────────────────────────────────────────
    step("Verifying prerequisites")
    if not SOURCE.is_file():
        sys.exit(f"ERROR: {SOURCE} not found.")
    if not PKG_ZIP.is_file():
        sys.exit("ERROR: pkg.zip not found. Run build.py first to generate it.")
    print(f"Found pkg.zip ({PKG_ZIP.stat().st_size / (1024 * 1024):.1f} MB)")
    # The wizard freeze plus room to move pkg.zip into the release folder.
    assert_free_space(ROOT, 1.5, "the wizard and the release folder")
    print(f"Found {SOURCE.name}")
    have_uninstaller = UNINSTALLER.is_file()
    if have_uninstaller:
        print(f"Found Uninstaller.exe ({UNINSTALLER.stat().st_size / (1024 * 1024):.1f} MB)")
    else:
        print("WARNING: Uninstaller.exe not found at the repo root.")
        print("         Run build_uninstaller.py first, or the release ships")
        print("         without an uninstaller and no Add/Remove entry.")

    version_file = render_versioninfo_for(
        version,
        ROOT / "Installer.version.txt",
        product_name=f"{PRODUCT_NAME} Installer",
        original_filename="Installer.exe",
    )
    print(f"VERSIONINFO : {version_file.name}")

    # ── 1) Clean ────────────────────────────────────────────────────────
    run_step("Cleaning previous installer artefacts", lambda: [
        clean_directory(ROOT / "build" / "Installer"),
        clean_directory(ROOT / "dist" / "Installer"),
    ])
    for stale in (ROOT / "dist" / "Installer.exe", ROOT / "Installer.spec"):
        if stale.exists():
            stale.unlink()
            print(f"Removed old: {stale}")

    # ── 2) Freeze ───────────────────────────────────────────────────────
    ensure_pyinstaller()
    run_step("Configuring Tcl/Tk for the wizard GUI", configure_tcl_tk)
    dll_args = run_step("Collecting Python DLL binaries", collect_python_dll_binaries)
    manifest_path = run_step(
        "Writing the asInvoker manifest",
        write_asinvoker_manifest, ROOT / "Installer.manifest",
    )

    command = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",            # pkg.zip stays external; the window opens at once
        "--windowed",
        "--noconfirm",
        "--noupx",
        "--name", "Installer",
        f"--manifest={manifest_path}",
        f"--version-file={version_file}",
        "--hidden-import=_tkinter",
        "--collect-all", "tkinter",
        *dll_args,
    ]
    if ICON_SRC.is_file():
        command.append(f"--icon={ICON_SRC}")
    if PRESERVE_LIST.is_file():
        # The wizard reads this list out of pkg.zip first; carrying a copy makes
        # it fail-safe even against a package that predates the file.
        command.append(f"--add-data={PRESERVE_LIST};.")
    command.append(str(SOURCE))

    step("Running PyInstaller")
    print("$ " + " ".join(command))
    if subprocess.run(command, cwd=str(ROOT), env=utf8_env()).returncode != 0:
        sys.exit(f"\nPyInstaller FAILED after {time.time() - start:.0f}s")

    # ── 3) Verify the frozen output ─────────────────────────────────────
    step("Verifying output")
    installer_dir = ROOT / "dist" / "Installer"
    installer_exe = installer_dir / "Installer.exe"
    if not installer_exe.is_file():
        sys.exit(f"ERROR: expected output not found: {installer_exe}")
    print(f"Installer.exe created ({installer_exe.stat().st_size / (1024 * 1024):.1f} MB)")

    # ── 4) Tidy the intermediates BEFORE the rename ─────────────────────
    step("Cleaning up build artefacts")
    clean_directory(ROOT / "build" / "Installer")
    for cleanup in (ROOT / "Installer.spec", manifest_path, version_file):
        if cleanup.exists():
            cleanup.unlink()
            print(f"Removed: {cleanup.name}")

    # ── 5) dist/Installer -> dist/Lumen_Release_v<version> ──────────────
    step("Assembling the release folder")
    safe_version = safe_version_for_path(version)
    release_dir = ROOT / "dist" / f"Lumen_Release_v{safe_version}"
    if release_dir.exists():
        clean_directory(release_dir)
    rename_with_retry(installer_dir, release_dir)
    print(f"Renamed {installer_dir.name}/ -> {release_dir.name}/")

    # ── 6) Move the payload in, under SHA-256 verification ──────────────
    step("Moving pkg.zip into the release folder")
    verified_move(PKG_ZIP, release_dir / "pkg.zip")

    if have_uninstaller:
        step("Moving Uninstaller.exe into the release folder")
        verified_move(UNINSTALLER, release_dir / "Uninstaller.exe")

    write_release_readme(release_dir, version)

    # ── 7) Prove the folder is complete ─────────────────────────────────
    step("Verifying the release folder")
    required = ["Installer.exe", "_internal", "pkg.zip", "README-INSTALL.txt"]
    if have_uninstaller:
        required.append("Uninstaller.exe")
    missing = [name for name in required if not (release_dir / name).exists()]
    if missing:
        sys.exit(f"ABORT: the release folder is missing: {', '.join(missing)}")
    total = sum(f.stat().st_size for f in release_dir.rglob("*") if f.is_file())
    for name in required:
        print(f"  [OK] {name}")
    print(f"  total size: {total / (1024 * 1024):.1f} MB")

    banner(f"INSTALLER BUILD COMPLETE  ·  v{version}  ·  {time.time() - start:.0f}s")
    print(f"  release folder : {release_dir}")
    print("\n  Zip this folder and hand it to a user, or run")
    print("  build_complete_release.py to have the archive made for you.")
    print("\n  NOTE: pkg.zip and Uninstaller.exe were MOVED, not copied.")
    print("        They are no longer at the repo root.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
