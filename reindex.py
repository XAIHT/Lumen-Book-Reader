# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Build or refresh the Lumen library index from the command line.

    python reindex.py                     # index the current folder
    python reindex.py D:\\Books            # index a specific datalake
    python reindex.py D:\\Books --no-text  # metadata only, much faster
    python reindex.py --search "sitchin"  # query the index that already exists
    python reindex.py --inside "detonation velocity"

The GUI does exactly this on a worker thread; having it as a script means a very
large datalake can be indexed once, ahead of time, and every later launch of the
reader starts against a warm index.
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths

from lumen_reader.library_index import (
    DEFAULT_TEXT_BUDGET,
    LibraryIndex,
    ScanProgress,
    normalize_root,
)


def index_database() -> Path:
    """The same file the GUI uses, resolved the same way."""
    QCoreApplication.setOrganizationName("Lumen Reader")
    QCoreApplication.setApplicationName("Lumen")
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / "library-index.db"


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index a Lumen book library.")
    parser.add_argument("root", nargs="?", default=".", help="library folder to index")
    parser.add_argument("--no-text", action="store_true",
                        help="skip full-text extraction (metadata only)")
    parser.add_argument("--budget", type=int, default=DEFAULT_TEXT_BUDGET,
                        help=f"characters of text indexed per book (default {DEFAULT_TEXT_BUDGET:,})")
    parser.add_argument("--workers", type=int, default=None, help="worker processes")
    parser.add_argument("--search", metavar="QUERY", help="search titles/authors and exit")
    parser.add_argument("--inside", metavar="QUERY", help="search inside book text and exit")
    parser.add_argument("--limit", type=int, default=20, help="results to show")
    arguments = parser.parse_args(argv)

    database = index_database()
    root = Path(arguments.root).expanduser().resolve()

    if arguments.search or arguments.inside:
        query = arguments.search or arguments.inside
        mode = "meta" if arguments.search else "content"
        with LibraryIndex(database) as index:
            started = time.perf_counter()
            total = index.count_matching(root, query, mode=mode)
            rows = index.search(root, query, mode=mode, limit=arguments.limit)
            elapsed = time.perf_counter() - started
        print(f"\n{total:,} match{'' if total == 1 else 'es'} for {query!r} "
              f"({mode}) in {elapsed * 1000:.1f} ms\n")
        for row in rows:
            print(f"  [{row.kind:4}] {row.title[:70]}")
            print(f"          {row.author[:60]}  ·  {human_bytes(row.size)}")
            if row.snippet:
                print(f"          … {row.snippet.strip()[:110]}")
        return 0

    print(f"\n  Lumen library indexer")
    print(f"  library : {root}")
    print(f"  index   : {database}")
    print(f"  text    : {'off' if arguments.no_text else f'{arguments.budget:,} chars/book'}")
    print(f"  workers : {arguments.workers or 'auto (machine-aware)'}\n")

    last_line = ""
    terminal_phase = ""
    started = time.perf_counter()

    def report(update: ScanProgress) -> None:
        nonlocal last_line, terminal_phase
        terminal_phase = update.phase
        if update.phase == "walk":
            line = f"  walking…  {update.detail}"
        elif update.phase == "extract":
            share = (update.done / update.total) if update.total else 0.0
            filled = int(share * 34)
            bar = "█" * filled + "·" * (34 - filled)
            rate = update.done / max(0.001, time.perf_counter() - started)
            line = f"  [{bar}] {share * 100:5.1f}%  {update.detail}  ({rate:,.0f} books/s)"
        elif update.phase == "error":
            line = f"  ERROR: {update.detail}"
        elif update.phase == "partial":
            line = f"  INCOMPLETE: {update.detail}"
        else:
            line = f"  done: {update.detail}"
        if line != last_line:
            print(line.ljust(len(last_line)), end="\r" if update.phase == "extract" else "\n",
                  flush=True)
            last_line = line

    with LibraryIndex(database, text_budget=arguments.budget) as index:
        counts = index.scan(
            root,
            progress=report,
            workers=arguments.workers,
            with_text=not arguments.no_text,
        )

    elapsed = time.perf_counter() - started
    print(f"\n\n  ── indexed in {elapsed:,.1f}s ─────────────────────────────")
    print(f"  TOTAL BOOKS : {counts.total:,}")
    print(f"  EPUB        : {counts.epub:,}")
    print(f"  PDF         : {counts.pdf:,}")
    print(f"  ON DISK     : {human_bytes(counts.bytes_total)}")
    print(f"  FULL-TEXT   : {counts.with_text:,} books searchable by topic")
    print(f"  ─────────────────────────────────────────────────────\n")
    return 2 if terminal_phase in {"error", "partial"} else 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
