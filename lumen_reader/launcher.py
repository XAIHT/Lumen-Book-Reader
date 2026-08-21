# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Frozen-application entry point - the target ``build.py`` hands PyInstaller.

Running from source, ``run_reader.py`` and ``python -m lumen_reader`` call
``app.main`` directly and the working directory is wherever you started the
shell, which is exactly what a developer wants.  An *installed* Lumen has no
such luxury, so this launcher does the three things that only matter once the
app has been frozen and registered with Windows:

1. **Anchor the working directory to the book.**  ``app.main`` reads the shelf
   from ``Path.cwd()`` and stores reading marks in ``lumen-reading-marks.json`` there.
   When Explorer opens a ``.epub`` through the file association, the inherited
   cwd is whatever Explorer felt like - often ``C:\\Windows\\system32``.  Left
   alone, Lumen would show an empty shelf and drop the reader's notes into a
   system folder.  We ``chdir`` to the book's own directory instead, so the
   shelf lists its siblings and the marks file lands beside the library.

2. **Pass an absolute book path.**  Because we just changed directory, a
   relative ``argv[1]`` would no longer resolve.  We hand ``app.main`` the
   fully-resolved path.

3. **Release the bundled DLL search path.**  PyInstaller prepends its
   ``_internal`` directory to the loader's search order; child processes -
   notably ``QtWebEngineProcess.exe`` - inherit it and can pin the bundled VC
   runtime open.  ``SetDllDirectoryW(NULL)`` restores the standard sequence.

Nothing here changes how the reader behaves when run from source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _release_bundled_dll_search_path() -> None:
    """Stop child processes inheriting PyInstaller's ``_internal`` DLL path."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
    except Exception:
        pass  # best effort; never fatal


def _anchor_to_book(arguments: list[str]) -> list[str]:
    """Chdir to the book's directory and absolutise its path in *arguments*."""
    from .ui import is_supported_book

    if len(arguments) < 2:
        return arguments
    candidate = arguments[1]
    if not candidate or candidate.startswith("-") or not is_supported_book(candidate):
        return arguments
    try:
        book = Path(candidate).expanduser().resolve()
    except OSError:
        return arguments
    if not book.is_file():
        return arguments
    parent = book.parent
    if parent.is_dir():
        try:
            os.chdir(parent)
        except OSError:
            pass  # a read-only or vanished folder must not block opening a book
    resolved = list(arguments)
    resolved[1] = str(book)
    return resolved


def main(argv: list[str] | None = None) -> int:
    """Launch Lumen the way an installed copy needs to be launched."""
    _release_bundled_dll_search_path()

    from .app import main as app_main

    arguments = _anchor_to_book(list(sys.argv if argv is None else argv))
    return app_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
