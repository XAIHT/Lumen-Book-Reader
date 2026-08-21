# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Top-level entry script for the frozen build - the target PyInstaller gets.

PyInstaller executes its entry script as ``__main__`` with no package context,
so ``lumen_reader/launcher.py`` cannot be handed to it directly: the very first
``from .app import main`` would raise "attempted relative import with no known
parent package". This two-line shim lives OUTSIDE the package, imports the
launcher absolutely, and gives PyInstaller a repo-root anchor for its module
search path at the same time.

Humans keep using ``run_reader.py``; only the freezer comes through here.
"""

from lumen_reader.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
