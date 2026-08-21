# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""build_support.py - the machinery every Lumen build script shares.

``build.py``, ``build_installer.py``, ``build_uninstaller.py`` and
``build_complete_release.py`` all need the same handful of awkward Windows
chores: tearing down a directory the indexer is still holding, finding the
Python and UCRT DLLs the PyInstaller bootloader needs, moving a 500 MB file
without silently corrupting it, and writing an ``asInvoker`` manifest.

They live here ONCE. Tlamatini carries three near-identical copies of the DLL
collector, and they have already drifted - one of them searches the Windows
SDK redistributable directories and the others do not. A single copy cannot
drift.

Standard library only: these run before anything is installed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

# pip's "A new release of pip is available" banner is pure build-log noise and
# is NOT fixable by upgrading: the build interpreter usually lives in a
# read-only Program Files prefix, so its pip cannot be replaced.
os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"


# ── Logging ────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print("\n" + "=" * 74, flush=True)
    print(f"== {msg}", flush=True)
    print("=" * 74, flush=True)


def step(msg: str) -> None:
    print(f"\n--- {msg} ---", flush=True)


def run_step(label: str, func, *args, **kwargs):
    """Execute a build step with consistent logging and error reporting."""
    step(label)
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        print(f"ERROR during '{label}': {exc}")
        raise


def utf8_env() -> dict:
    """An environment where child builds speak UTF-8 and pip stays quiet."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


# ── Directory teardown ─────────────────────────────────────────────────────

def _on_rmtree_error(func, path, _exc_info):
    """Strip the read-only bit and retry - the usual Windows rmtree failure."""
    try:
        os.chmod(path, stat.S_IWUSR | stat.S_IREAD)
        func(path)
    except Exception:
        pass


def clean_directory(path, max_retries: int = 3, delay: float = 2.0) -> None:
    """Remove a directory tree, working around Windows file locking.

    1. ``shutil.rmtree`` up to *max_retries* times, stripping read-only flags.
    2. If it still exists, RENAME it out of the way, so PyInstaller gets a
       clean directory instead of crashing on a locked DLL. An antivirus
       scanner or the search indexer holding one file must not fail a release.
    """
    p = Path(path)
    if not p.exists():
        return
    for attempt in range(1, max_retries + 1):
        try:
            shutil.rmtree(p, onerror=_on_rmtree_error)
        except Exception as exc:
            print(f"  rmtree attempt {attempt}/{max_retries} failed: {exc}")
        if not p.exists():
            print(f"Removed: {p}")
            return
        if attempt < max_retries:
            print(f"  Retrying in {delay}s (locked files in {p.name})...")
            time.sleep(delay)

    stale = p.parent / f"{p.name}_stale_{os.getpid()}"
    try:
        p.rename(stale)
        print(f"WARNING: could not fully remove {p.name} - renamed to {stale.name}")
        print(f"  You can delete '{stale}' later.")
    except OSError as exc:
        print(f"ERROR: could not remove or rename {p}: {exc}")
        print("  Close any program using this directory and re-run.")


def rename_with_retry(src: Path, dst: Path, attempts: int = 5,
                      delay: float = 2.0) -> None:
    """Rename a directory, retrying past transient Explorer/AV handles."""
    for attempt in range(1, attempts + 1):
        try:
            src.rename(dst)
            return
        except PermissionError as exc:
            if attempt == attempts:
                raise
            print(f"  Rename attempt {attempt}/{attempts} failed ({exc}); retrying in {delay}s...")
            time.sleep(delay)


# ── PyInstaller ────────────────────────────────────────────────────────────

def ensure_pyinstaller() -> None:
    """Import PyInstaller, installing it into this interpreter if missing."""
    step("Checking PyInstaller")
    try:
        import PyInstaller  # noqa: F401
        try:
            from PyInstaller import __version__ as pyi_version
            print(f"PyInstaller {pyi_version} is available.")
        except Exception:
            print("PyInstaller is available.")
        return
    except ImportError:
        pass
    print("PyInstaller not found - installing...")
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "--disable-pip-version-check",
         "install", "pyinstaller"]
    ).returncode
    if rc != 0:
        sys.exit("ERROR: could not install PyInstaller. Aborting build.")


def gather_search_dirs() -> list[Path]:
    """Ordered, de-duplicated directories to search for runtime DLLs.

    Resolution order (first match wins):
      1. The Python actually executing this script (base_prefix / prefix / exe dir)
      2. Its ``DLLs`` sub-folders (standard layout and the MS Store layout)
      3. Windows 10/11 SDK UCRT redistributables, if the SDK is installed
      4. ``C:\\Windows\\System32`` as the last-resort VC-runtime fallback
    """
    dirs: list[Path] = [
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(sys.executable).parent,
        Path(sys.base_prefix) / "DLLs",
        Path(sys.executable).parent / "DLLs",
    ]

    sdk_base = Path("C:/Program Files (x86)/Windows Kits/10/Redist")
    if sdk_base.is_dir():
        dirs.append(sdk_base / "ucrt/DLLs/x64")
        try:
            for ver_dir in sdk_base.iterdir():
                if ver_dir.is_dir():
                    dirs.append(ver_dir / "ucrt/DLLs/x64")
        except OSError:
            pass

    dirs.append(Path("C:/Windows/System32"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        try:
            resolved = d.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def find_first_dll(name: str, search_dirs: list[Path]) -> Path | None:
    """First existing path for *name* across *search_dirs*."""
    for d in search_dirs:
        candidate = d / name
        if candidate.exists():
            return candidate
    return None


def collect_python_dll_binaries() -> list[str]:
    """Every DLL the frozen bootloader needs, as ``--add-binary`` arguments.

    Bundling ``python3XX.dll`` alone is not enough. Windows reports "The
    specified module could not be found" - naming the DLL that IS present -
    when one of its transitive dependencies is missing from the extraction
    directory. So we also carry:

      * ``python3.dll``          - the stable-ABI DLL the bootloader may want
      * ``vcruntime140*.dll``    - the VC runtime
      * ``ucrtbase.dll``         - the Universal CRT
      * ``api-ms-win-crt-*.dll`` - the UCRT API-set forwarders
    """
    binaries: list[str] = []
    ver = sys.version_info
    dll_name = f"python{ver.major}{ver.minor}.dll"
    search_dirs = gather_search_dirs()

    print(f"Python executable : {sys.executable}")
    print(f"Python version    : {ver.major}.{ver.minor}.{ver.micro}")
    print(f"Looking for       : {dll_name}")
    print(f"Search directories: {len(search_dirs)}")

    for name, label, warn in (
        (dll_name, "Python DLL", True),
        ("python3.dll", "stable ABI DLL", True),
        ("vcruntime140.dll", "VC runtime", True),
        ("vcruntime140_1.dll", "VC runtime", True),
        ("ucrtbase.dll", "UCRT base", False),
    ):
        found = find_first_dll(name, search_dirs)
        if found:
            binaries.append(f"--add-binary={found};.")
            print(f"Bundling {label}: {found}")
        elif warn:
            print(f"WARNING: could not locate {name}")

    forwarders = 0
    seen_names: set[str] = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            lname = f.name.lower()
            if lname.startswith("api-ms-win-crt-") and lname.endswith(".dll"):
                if lname not in seen_names:
                    seen_names.add(lname)
                    binaries.append(f"--add-binary={f};.")
                    forwarders += 1
    if forwarders:
        print(f"Bundling {forwarders} UCRT forwarder DLLs")
    else:
        print("WARNING: could not locate any api-ms-win-crt-*.dll forwarders")

    return binaries


def write_asinvoker_manifest(path: Path) -> Path:
    """Write an application manifest declaring ``asInvoker``.

    Lumen installs per-user under HKCU. Without this manifest Windows'
    installer-detection heuristics see an executable called "Installer" and
    prompt for administrator rights the install does not need and will not
    use - and a UAC prompt on a per-user install teaches people to click
    through UAC prompts.
    """
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">\n'
        '  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">\n'
        '    <security>\n'
        '      <requestedPrivileges>\n'
        '        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>\n'
        '      </requestedPrivileges>\n'
        '    </security>\n'
        '  </trustInfo>\n'
        '</assembly>\n',
        encoding="utf-8",
    )
    print(f"Created asInvoker manifest: {path}")
    return path


def configure_tcl_tk() -> None:
    """Point ``TCL_LIBRARY`` / ``TK_LIBRARY`` at this Python's Tcl data files.

    The installer and uninstaller are Tkinter GUIs. PyInstaller can only bundle
    the Tcl/Tk data files if it can find them, and the layout differs between a
    python.org install, an MS Store install and a conda prefix.
    """
    py_prefix = Path(sys.prefix)
    base_prefix = Path(sys.base_prefix)
    candidates = [
        (py_prefix / "tcl" / "tcl8.6", py_prefix / "tcl" / "tk8.6"),
        (base_prefix / "tcl" / "tcl8.6", base_prefix / "tcl" / "tk8.6"),
        (py_prefix / "lib" / "tcl8.6", py_prefix / "lib" / "tk8.6"),
        (base_prefix / "lib" / "tcl8.6", base_prefix / "lib" / "tk8.6"),
        (py_prefix / "Lib" / "tcl8.6", py_prefix / "Lib" / "tk8.6"),
        (py_prefix / "Library" / "lib" / "tcl8.6",
         py_prefix / "Library" / "lib" / "tk8.6"),
    ]
    for tcl_dir, tk_dir in candidates:
        if tcl_dir.exists():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tk_dir)
            print(f"Set TCL_LIBRARY={tcl_dir}")
            print(f"Set TK_LIBRARY={tk_dir}")
            return
    print("WARNING: could not locate the Tcl/Tk data directories.")

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("WARNING: tkinter is not installed - the wizard GUI will not build.")


# ── Disk space ─────────────────────────────────────────────────────────────

def assert_free_space(path, required_gb: float, what: str) -> None:
    """Refuse to start a build the volume cannot hold.

    Freezing Lumen writes roughly a gigabyte across ``build/``, ``dist/``, the
    staged payload and ``pkg.zip``. When the volume runs out midway, PyInstaller
    dies with ``OSError: [Errno 28] No space left on device`` after several
    minutes of work, leaving hundreds of megabytes of rubble behind - and the
    rubble makes the next attempt fail sooner. Checking first costs microseconds
    and turns a ten-minute mystery into one sentence.
    """
    path = Path(path)
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return                      # unknowable (a network path); do not block
    free_gb = free / (1024 ** 3)
    print(f"  free space on {path.drive or path}: {free_gb:.1f} GB "
          f"(need about {required_gb:.1f} GB for {what})")
    if free_gb >= required_gb:
        return

    roomiest = ""
    try:
        best = 0
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if not drive.exists():
                continue
            candidate = shutil.disk_usage(drive).free
            if candidate > best:
                best, roomiest = candidate, f"{letter}: ({candidate / (1024 ** 3):.0f} GB free)"
    except OSError:
        pass

    sys.exit(
        f"\nABORT: not enough free space on {path.drive or path} - "
        f"{free_gb:.1f} GB available, about {required_gb:.1f} GB needed for {what}.\n"
        f"  Free some space and re-run."
        + (f"\n  The roomiest volume on this machine is {roomiest}." if roomiest else "")
    )


# ── Verified file operations ───────────────────────────────────────────────

def sha256(filepath: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """SHA-256 hex digest of a file, read in 8 MB chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verified_move(src: Path, dst: Path) -> str:
    """Move *src* to *dst* and PROVE the bytes survived. Returns the digest.

    A cross-volume ``shutil.move`` is a copy followed by a delete, and a copy
    that is truncated by a full disk raises nothing you would notice until a
    user's installer says "pkg.zip is corrupt". Hash before, hash after, refuse
    to continue on a mismatch. Moving rather than copying also keeps a 500 MB
    package from existing twice on Angela's disk.
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"source file does not exist: {src}")

    src_size = src.stat().st_size
    src_hash = sha256(src)
    print(f"Source: {src}")
    print(f"  Size   : {src_size / (1024 * 1024):.1f} MB")
    print(f"  SHA-256: {src_hash[:16]}…")

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    print(f"Moving -> {dst}")
    shutil.move(str(src), str(dst))

    if not dst.exists():
        raise RuntimeError(f"{dst} does not exist after the move")
    dst_size = dst.stat().st_size
    if dst_size != src_size:
        raise RuntimeError(f"size mismatch after move: src={src_size}, dst={dst_size}")
    dst_hash = sha256(dst)
    if dst_hash != src_hash:
        raise RuntimeError(f"SHA-256 mismatch after move: src={src_hash}, dst={dst_hash}")

    print(f"  OK: move verified (SHA-256={dst_hash[:12]}…, "
          f"{dst_size / (1024 * 1024):.1f} MB)")
    return dst_hash


__all__ = [
    "assert_free_space",
    "banner",
    "clean_directory",
    "collect_python_dll_binaries",
    "configure_tcl_tk",
    "ensure_pyinstaller",
    "find_first_dll",
    "gather_search_dirs",
    "rename_with_retry",
    "run_step",
    "sha256",
    "step",
    "utf8_env",
    "verified_move",
    "write_asinvoker_manifest",
]
