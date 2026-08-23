# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Parallel library indexer and search engine for very large book datalakes.

The shelf used to be ``[p for p in Path.cwd().iterdir() if is_supported_book(p)]``
rebuilt into a ``QListWidget`` on every keystroke.  That is fine for a dozen
books and unusable for ten thousand.  This module replaces it with the three
pieces a datalake actually needs:

1. **A fast recursive walk.**  ``os.scandir`` over the whole tree, collecting
   only ``(path, size, mtime_ns)``.  No parsing, no stat storms - this stays
   quick into the millions of files.

2. **Parallel extraction.**  Every book's metadata (and a bounded slice of its
   text, for topic search) is pulled in a ``ProcessPoolExecutor`` spanning every
   core on the machine.  EPUBs are read straight out of the ZIP central
   directory - only the OPF and the spine documents, never a full extraction.
   PDFs go through MuPDF's metadata block and page text.

3. **A SQLite/FTS5 index.**  Metadata and content live in two FTS5 tables, so a
   query is an index lookup rather than a scan over every book.  The GUI never
   holds more than one page of rows, which is what lets a library of millions
   paint instantly.

Indexing is *incremental*: a book whose ``(size, mtime_ns)`` still matches the
stored row is skipped, so the second scan of an unchanged shelf costs one walk.
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from xml.etree import ElementTree as ET

BOOK_SUFFIXES = {".epub", ".pdf"}

#: How much extracted text is indexed per book for topic search.  Topic and
#: subject matter are overwhelmingly decided in the front matter, so a bounded
#: head keeps the index proportionate to the shelf instead of to the corpus.
DEFAULT_TEXT_BUDGET = 250_000

#: How many opening pages may come back with no text at all before a PDF is
#: taken to have no text layer.  Generous enough for a cover, front matter and
#: a plate section; short enough that a scanned encyclopedia is not parsed to
#: the last of its two thousand pages for nothing.
_PDF_EMPTY_PAGE_LIMIT = 24

#: Directories that never hold a reader's library but can hold thousands of
#: files.  Skipping them keeps the walk honest on a repository-shaped root.
SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".idea", ".vscode",
    "$RECYCLE.BIN", "System Volume Information",
}

_TAG_RE = re.compile(r"<[^>]+>")
_DROP_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_SPACE_RE = re.compile(r"\s+")
_FTS_SAFE_RE = re.compile(r"[^\w\s'-]", re.UNICODE)


# ───────────────────────────── data carriers ──────────────────────────────


@dataclass(slots=True)
class BookRow:
    """One indexed book, as the shelf needs it."""

    path: str
    name: str
    ext: str
    size: int
    title: str
    author: str
    publisher: str = ""
    language: str = ""
    subjects: str = ""
    pages: int = 0
    snippet: str = ""

    @property
    def kind(self) -> str:
        return self.ext.lstrip(".").upper()


@dataclass(slots=True)
class LibraryCounts:
    """Headline totals for the shelf banner."""

    total: int = 0
    by_ext: dict[str, int] = field(default_factory=dict)
    bytes_total: int = 0
    indexed: int = 0
    with_text: int = 0

    @property
    def epub(self) -> int:
        return self.by_ext.get(".epub", 0)

    @property
    def pdf(self) -> int:
        return self.by_ext.get(".pdf", 0)


@dataclass(slots=True)
class ScanProgress:
    phase: str
    done: int = 0
    total: int = 0
    detail: str = ""


# ───────────────────────────── text helpers ───────────────────────────────


def _local(tag: str) -> str:
    """The local part of a possibly namespaced XML tag, lowercased."""
    return tag.rsplit("}", 1)[-1].lower()


def strip_markup(markup: str) -> str:
    """Flatten XHTML into indexable prose."""
    without_code = _DROP_RE.sub(" ", markup)
    text = _TAG_RE.sub(" ", without_code)
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()


def _clean(value: Any) -> str:
    if not value:
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()


# ──────────────────────── per-book extraction (workers) ────────────────────
#
# These run inside ProcessPoolExecutor workers, so they must stay importable at
# module level and must never raise: a corrupt book becomes a row flagged
# ``ok=False`` rather than a crashed pool.


def _epub_opf_name(archive: zipfile.ZipFile) -> str:
    container = archive.read("META-INF/container.xml")
    root = ET.fromstring(container)
    for element in root.iter():
        if _local(element.tag) == "rootfile":
            full_path = element.get("full-path", "")
            if full_path:
                return full_path.lstrip("/")
    raise ValueError("no rootfile in container.xml")


def _epub_record(path: Path, text_budget: int) -> dict[str, Any]:
    record: dict[str, Any] = {"authors": [], "subjects": [], "body": ""}
    with zipfile.ZipFile(path) as archive:
        opf_name = _epub_opf_name(archive)
        opf_dir = os.path.dirname(opf_name)
        root = ET.fromstring(archive.read(opf_name))

        manifest: dict[str, tuple[str, str]] = {}
        spine: list[str] = []
        for element in root.iter():
            tag = _local(element.tag)
            if tag == "title" and not record.get("title"):
                record["title"] = _clean(element.text)
            elif tag == "creator":
                author = _clean(element.text)
                if author and author not in record["authors"]:
                    record["authors"].append(author)
            elif tag == "publisher" and not record.get("publisher"):
                record["publisher"] = _clean(element.text)
            elif tag == "language" and not record.get("language"):
                record["language"] = _clean(element.text)
            elif tag == "subject":
                subject = _clean(element.text)
                if subject and subject not in record["subjects"]:
                    record["subjects"].append(subject)
            elif tag == "description" and not record.get("description"):
                record["description"] = strip_markup(_clean(element.text))[:2000]
            elif tag == "item":
                item_id = element.get("id") or ""
                href = element.get("href") or ""
                media = (element.get("media-type") or "").lower()
                if item_id and href:
                    manifest[item_id] = (href, media)
            elif tag == "itemref":
                idref = element.get("idref") or ""
                if idref:
                    spine.append(idref)

        record["pages"] = len(spine)

        if text_budget > 0:
            names = {name.lstrip("/"): name for name in archive.namelist()}
            chunks: list[str] = []
            budget = text_budget
            for idref in spine:
                if budget <= 0:
                    break
                href, media = manifest.get(idref, ("", ""))
                if not href or ("html" not in media and "xml" not in media):
                    continue
                target = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                actual = names.get(target) or names.get(target.lstrip("./"))
                if actual is None:
                    continue
                try:
                    raw = archive.read(actual)
                except (KeyError, zipfile.BadZipFile, OSError):
                    continue
                text = strip_markup(raw.decode("utf-8", "replace"))
                if not text:
                    continue
                chunks.append(text[:budget])
                budget -= len(text)
            record["body"] = " ".join(chunks)[:text_budget]

    return record


def _pdf_record(path: Path, text_budget: int, page_cap: int = 0) -> dict[str, Any]:
    import pymupdf  # imported per worker; MuPDF is heavy to load

    # MuPDF narrates every unsupported annotation and broken colour profile it
    # meets straight to stderr.  Across a few thousand PDFs that is thousands of
    # lines of noise about files that parse perfectly well, so the chatter is
    # turned off and genuine failures are reported through the record instead.
    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass

    record: dict[str, Any] = {"authors": [], "subjects": [], "body": ""}
    with pymupdf.open(path) as document:
        if document.needs_pass:
            record["locked"] = True
        info = document.metadata or {}
        record["title"] = _clean(info.get("title"))
        author = _clean(info.get("author"))
        if author:
            record["authors"] = [part.strip() for part in re.split(r"[;,]", author) if part.strip()]
        record["publisher"] = _clean(info.get("producer"))
        keywords = _clean(info.get("keywords"))
        if keywords:
            record["subjects"] = [k.strip() for k in re.split(r"[;,]", keywords) if k.strip()]
        record["description"] = _clean(info.get("subject"))[:2000]
        record["pages"] = document.page_count

        if text_budget > 0 and not record.get("locked"):
            chunks: list[str] = []
            budget = text_budget
            empty_run = 0
            for number, page in enumerate(document):
                if budget <= 0 or (page_cap and number >= page_cap):
                    break
                try:
                    text = _SPACE_RE.sub(" ", page.get_text()).strip()
                except Exception:
                    continue
                if not text:
                    # A scanned book has no text layer at all, and asking MuPDF
                    # for one costs a full page parse every time.  Measured on a
                    # real 1,128-PDF shelf, a handful of 200 MB scans held the
                    # whole fleet at a standstill for minutes.  Pages that keep
                    # coming back empty mean there is nothing here to index.
                    empty_run += 1
                    if empty_run >= _PDF_EMPTY_PAGE_LIMIT and not chunks:
                        break
                    continue
                empty_run = 0
                chunks.append(text[:budget])
                budget -= len(text)
            record["body"] = " ".join(chunks)[:text_budget]

    return record


def extract_book(job: Sequence[Any]) -> dict[str, Any]:
    """Worker entry point: pull metadata and indexable text from one book.

    The job is ``(path, suffix, text_budget)`` with an optional fourth element
    capping how many PDF pages may be read.  Accepting both lengths keeps the
    signature stable for callers that never needed the cap.
    """
    path_text, suffix, text_budget = job[0], job[1], job[2]
    page_cap = int(job[3]) if len(job) > 3 else 0
    path = Path(path_text)
    result: dict[str, Any] = {
        "path": path_text,
        "title": "",
        "author": "",
        "publisher": "",
        "language": "",
        "subjects": "",
        "description": "",
        "pages": 0,
        "body": "",
        "ok": True,
        "error": "",
    }
    try:
        record = (_pdf_record(path, text_budget, page_cap) if suffix == ".pdf"
                  else _epub_record(path, text_budget))
    except Exception as exception:  # a broken book must never kill the pool
        result["ok"] = False
        result["error"] = f"{type(exception).__name__}: {exception}"[:400]
        result["title"] = path.stem
        return result

    result["title"] = record.get("title") or path.stem
    result["author"] = ", ".join(record.get("authors") or []) or "Unknown author"
    result["publisher"] = record.get("publisher", "")
    result["language"] = record.get("language", "")
    result["subjects"] = ", ".join(record.get("subjects") or [])
    result["description"] = record.get("description", "")
    result["pages"] = int(record.get("pages") or 0)
    result["body"] = record.get("body", "")
    return result


# ─────────────────────────────── filesystem ───────────────────────────────


def walk_library(root: Path, skip: set[str] | None = None) -> Iterator[tuple[str, int, int, str]]:
    """Yield ``(path, size, mtime_ns, suffix)`` for every book beneath *root*.

    Uses an explicit stack rather than recursion so a pathological tree cannot
    exhaust the interpreter stack, and reads size/mtime from the ``scandir``
    entry, which on Windows comes free with the directory listing.
    """
    skipped = SKIP_DIRECTORIES if skip is None else skip
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in skipped and not entry.name.startswith("$"):
                        stack.append(Path(entry.path))
                    continue
                suffix = os.path.splitext(entry.name)[1].casefold()
                if suffix not in BOOK_SUFFIXES:
                    continue
                stat = entry.stat(follow_symlinks=False)
                yield entry.path, stat.st_size, stat.st_mtime_ns, suffix
            except OSError:
                continue


# ─────────────────────────────── the index ────────────────────────────────


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY,
    root        TEXT    NOT NULL,
    path        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    ext         TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    author      TEXT    NOT NULL DEFAULT '',
    publisher   TEXT    NOT NULL DEFAULT '',
    language    TEXT    NOT NULL DEFAULT '',
    subjects    TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    pages       INTEGER NOT NULL DEFAULT 0,
    has_text    INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT    NOT NULL DEFAULT '',
    seen_gen    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS books_root  ON books(root);
CREATE INDEX IF NOT EXISTS books_ext   ON books(root, ext);
CREATE INDEX IF NOT EXISTS books_title ON books(root, title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS books_gen   ON books(root, seen_gen);

-- One row per completed sweep, so the settings window can say what actually
-- happened last time instead of leaving the reader to guess.
CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY,
    root        TEXT    NOT NULL,
    generation  INTEGER NOT NULL,
    finished_at REAL    NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    found       INTEGER NOT NULL DEFAULT 0,
    indexed     INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    cancelled   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS scan_runs_root ON scan_runs(root, finished_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, author, name, subjects, publisher,
    book_id UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    body,
    book_id UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);

-- The index FTS5 refuses to give us.
--
-- ``book_id`` has to be UNINDEXED - indexing it would tokenise integers into
-- the same vocabulary as the prose and poison every search - but that leaves
-- ``DELETE FROM content_fts WHERE book_id = ?`` with no way to find its row.
-- SQLite plans it as ``SCAN content_fts VIRTUAL TABLE INDEX 0:``: a full pass
-- over the entire full-text index, which on a real library IS the database.
-- Re-indexing one book therefore cost a scan of all of them.  Measured on a
-- 235 MB index that is 218 ms per book against 2.1 ms by rowid - 105x - and
-- the gap grows with the index, because one side is O(n) and the other is not.
-- On a 10.4 GB index it worked out at ten seconds a book, which is how a sweep
-- of 304 changed books came to commit seventeen of them and then appear to
-- stop forever.
--
-- So we keep the rowids ourselves.  One row per book, holding where that book
-- sits in each FTS table, and every delete becomes a rowid lookup.
CREATE TABLE IF NOT EXISTS fts_rowid (
    book_id     INTEGER PRIMARY KEY,
    meta_row    INTEGER,
    content_row INTEGER
);

-- Small durable facts about the index itself - currently just whether the map
-- above has been built, which cannot be inferred from its contents (an empty
-- map is also what a library with no books looks like).
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: ``index_meta`` key recording that :func:`build_fts_map` has run.
FTS_MAP_KEY = "fts_map_built"


# ── the FTS rowid map ──────────────────────────────────────────────────────
#
# These are module functions rather than LibraryIndex methods because the sweep
# writer owns its own raw connection to the same file - it is the one stage
# allowed to write - and it needs exactly this behaviour without opening a
# second LibraryIndex around the same database.


def meta_get(connection: sqlite3.Connection, key: str) -> str:
    try:
        row = connection.execute(
            "SELECT value FROM index_meta WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row is not None else ""


def meta_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO index_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def fts_map_ready(connection: sqlite3.Connection) -> bool:
    """Whether every existing FTS row can be found by rowid."""
    return meta_get(connection, FTS_MAP_KEY) == "1"


def build_fts_map(connection: sqlite3.Connection,
                  progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Populate the rowid map for rows written before it existed.

    One pass over each FTS table, once in the life of an index.  Everything
    written afterwards records its rowids as it goes, so this never runs again -
    which is the whole point of paying for it deliberately, here, instead of
    accidentally, one full scan per book, forever.
    """
    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    cursor = connection.cursor()
    cursor.execute("DELETE FROM fts_rowid")

    say("Mapping the title index…")
    cursor.execute(
        "INSERT INTO fts_rowid (book_id, meta_row)"
        " SELECT book_id, rowid FROM books_fts WHERE book_id IS NOT NULL"
        " ON CONFLICT(book_id) DO UPDATE SET meta_row = excluded.meta_row"
    )

    say("Mapping the full-text index — one pass, only ever once…")
    cursor.execute(
        "INSERT INTO fts_rowid (book_id, content_row)"
        " SELECT book_id, rowid FROM content_fts WHERE book_id IS NOT NULL"
        " ON CONFLICT(book_id) DO UPDATE SET content_row = excluded.content_row"
    )
    content_rows = int(cursor.execute("SELECT count(*) FROM content_fts").fetchone()[0] or 0)

    mapped = int(cursor.execute("SELECT count(*) FROM fts_rowid").fetchone()[0] or 0)
    pointed_at = int(cursor.execute(
        "SELECT count(*) FROM fts_rowid WHERE content_row IS NOT NULL").fetchone()[0] or 0)
    # More full-text rows than books means an earlier sweep left duplicates
    # behind.  The map can only point at one of them, so say so rather than let
    # a stale body quietly go on answering searches.
    orphans = max(0, content_rows - pointed_at)

    meta_set(connection, FTS_MAP_KEY, "1")
    connection.commit()
    say(f"Full-text map built: {mapped:,} books"
        + (f", {orphans:,} duplicate rows found" if orphans else ""))
    return {"mapped": mapped, "content_rows": content_rows, "orphans": orphans}


def drop_fts_rows(cursor: sqlite3.Cursor, book_ids: Sequence[int]) -> None:
    """Remove the FTS entries for *book_ids*, by rowid wherever we can.

    Falls back to the scanning delete only for books the map has never seen,
    which after :func:`build_fts_map` means none.  The fallback stays because a
    fast-but-wrong delete would leave stale text answering searches, and that is
    a worse failure than a slow one.
    """
    if not book_ids:
        return
    for chunk in _chunked(list(book_ids), 400):
        marks = ",".join("?" * len(chunk))
        mapped: dict[int, tuple[Any, Any]] = {
            int(row[0]): (row[1], row[2])
            for row in cursor.execute(
                f"SELECT book_id, meta_row, content_row FROM fts_rowid"
                f" WHERE book_id IN ({marks})", chunk
            ).fetchall()
        }
        unmapped = [book_id for book_id in chunk if book_id not in mapped]
        meta_rows = [value[0] for value in mapped.values() if value[0] is not None]
        content_rows = [value[1] for value in mapped.values() if value[1] is not None]

        for table, rows in (("books_fts", meta_rows), ("content_fts", content_rows)):
            for row_chunk in _chunked(rows, 400):
                row_marks = ",".join("?" * len(row_chunk))
                cursor.execute(f"DELETE FROM {table} WHERE rowid IN ({row_marks})", row_chunk)

        if unmapped:
            unmapped_marks = ",".join("?" * len(unmapped))
            cursor.execute(
                f"DELETE FROM books_fts   WHERE book_id IN ({unmapped_marks})", unmapped)
            cursor.execute(
                f"DELETE FROM content_fts WHERE book_id IN ({unmapped_marks})", unmapped)
        cursor.execute(f"DELETE FROM fts_rowid WHERE book_id IN ({marks})", chunk)


def default_index_path() -> Path:
    """Where the index lives: beside the other Lumen state, never in the library.

    The index is a rebuildable cache, so it belongs with application state - not
    scattered through a library folder that may well be read-only or synced.
    """
    from PySide6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / "library-index.db"


def normalize_root(root: str | Path) -> str:
    return os.path.normcase(str(Path(root).expanduser().resolve()))


def build_match_expression(query: str) -> tuple[str, list[str]]:
    """Turn a human query into an FTS5 MATCH expression plus extension filters.

    Supports quoted phrases, ``ext:pdf`` / ``pdf:`` style filters, and treats the
    final bare word as a prefix so results narrow while the reader is still
    typing.  Every token is scrubbed of FTS5 operators, so no input can produce
    a syntax error - the worst case is a query that matches nothing.
    """
    extensions: list[str] = []
    phrases: list[str] = []
    words: list[str] = []

    for raw in re.findall(r'"[^"]*"|\S+', query.strip()):
        token = raw.strip()
        if not token:
            continue
        if token.startswith('"') and token.endswith('"') and len(token) > 2:
            phrase = _FTS_SAFE_RE.sub(" ", token[1:-1]).strip()
            if phrase:
                phrases.append(f'"{phrase}"')
            continue
        lowered = token.casefold()
        if lowered in {"ext:epub", "epub:", "type:epub", ".epub"}:
            extensions.append(".epub")
            continue
        if lowered in {"ext:pdf", "pdf:", "type:pdf", ".pdf"}:
            extensions.append(".pdf")
            continue
        cleaned = _FTS_SAFE_RE.sub(" ", token).strip()
        if cleaned:
            words.extend(cleaned.split())

    terms = list(phrases)
    for index, word in enumerate(words):
        is_last = index == len(words) - 1
        terms.append(f"{word}*" if is_last and len(word) >= 2 else word)

    return " AND ".join(terms), extensions


class LibraryIndex:
    """SQLite-backed catalogue of a book datalake."""

    def __init__(self, database: str | Path | None = None, text_budget: int = DEFAULT_TEXT_BUDGET):
        self.path = Path(database) if database is not None else default_index_path()
        self.text_budget = max(0, int(text_budget))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # WAL lets the shelf keep searching while a scan is still writing.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA cache_size=-131072")  # 128 MB page cache
        # Migration comes first, and must: SCHEMA builds an index over
        # ``seen_gen``, and ``CREATE INDEX`` on a column that an older database
        # does not have is a hard error - one that stopped Lumen from starting
        # at all against a real 7.79 GB index written by the previous version.
        self._migrate()
        self.connection.executescript(SCHEMA)
        self._adopt_fresh_database()
        self.connection.commit()

    def _migrate(self) -> None:
        """Bring an index written by an older Lumen up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone,
        so a database from before generation-marking keeps its old shape however
        many times the schema script is run over it.  The column has to be added
        by hand, before anything that depends on it.
        """
        existing = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'books'"
        ).fetchone()
        if existing is None:
            return                      # a fresh database: SCHEMA builds it correctly
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(books)")}
        if "seen_gen" not in columns:
            self.connection.execute(
                "ALTER TABLE books ADD COLUMN seen_gen INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.commit()

    def _adopt_fresh_database(self) -> None:
        """A brand-new index is born with its rowid map already correct.

        Building the map costs one pass over the whole full-text index, so it
        must never be paid by a reader who has nothing to migrate.  An index
        with no books has nothing to map, and everything written from here on
        records its own rowids.
        """
        if self.fts_map_ready():
            return
        row = self.connection.execute("SELECT 1 FROM books LIMIT 1").fetchone()
        if row is None:
            self._meta_set(FTS_MAP_KEY, "1")

    # ── the FTS rowid map ──────────────────────────────────────────────────

    def fts_map_ready(self) -> bool:
        """Whether every existing FTS row can be found by rowid."""
        return fts_map_ready(self.connection)

    def build_fts_map(self, progress: Callable[[str], None] | None = None) -> dict[str, int]:
        """Map every existing FTS row to its book.  Once per index, ever."""
        return build_fts_map(self.connection, progress)

    def drop_fts_rows(self, cursor: sqlite3.Cursor, book_ids: Sequence[int]) -> None:
        """Remove the FTS entries for *book_ids*, by rowid wherever we can."""
        drop_fts_rows(cursor, book_ids)

    def _meta_set(self, key: str, value: str) -> None:
        meta_set(self.connection, key, value)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "LibraryIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── scanning ───────────────────────────────────────────────────────────

    def scan(
        self,
        root: str | Path,
        *,
        progress: Callable[[ScanProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        workers: int | None = None,
        with_text: bool = True,
        config: Any = None,
    ) -> LibraryCounts:
        """Index every book under *root*, reusing rows that have not changed.

        This is the blocking, callback-shaped face of the sweep, kept for the
        headless callers and the tests.  The work itself is done by
        :class:`~lumen_reader.turbo_scan.TurboScanner`, whose stages all run at
        once; the ``walk`` / ``extract`` / ``done`` phases reported here are a
        translation of its live telemetry, not separate passes.
        """
        from .turbo_scan import ScanConfig, TurboScanner

        settings = config if config is not None else ScanConfig()
        settings.with_text = with_text
        settings.text_budget = self.text_budget
        if workers is not None:
            settings.processes = max(1, int(workers))

        stop = cancelled or (lambda: False)
        root_key = normalize_root(root)
        scanner = TurboScanner(self.path, root, settings)
        scanner.start()

        emitted_extract = False
        while True:
            finished = scanner.wait(0.1)
            if stop():
                scanner.cancel()
            state = scanner.snapshot()
            if progress is not None:
                stale = state.books_found - state.books_unchanged
                if not state.walk_complete:
                    progress(ScanProgress("walk", state.books_found, 0,
                                          f"{state.books_found:,} books found"))
                elif stale > 0:
                    emitted_extract = True
                    done = state.books_indexed + state.books_failed
                    progress(ScanProgress("extract", done, stale, f"{done:,} / {stale:,}"))
            if finished:
                break

        state = scanner.snapshot()
        if progress is not None:
            if state.error:
                progress(ScanProgress("error", detail=state.error))
            else:
                done = state.books_indexed + state.books_failed
                detail = f"{done:,} indexed" if emitted_extract else "index already current"
                progress(ScanProgress("done", done, max(done, state.books_found), detail))
        return state.counts if state.counts is not None else self.counts(root_key)

    # ── generation marking ─────────────────────────────────────────────────
    #
    # Finding the books that have left the shelf used to need the whole indexed
    # path set and the whole found path set in memory at the same time.  Each
    # sweep now stamps the rows it saw with its own number, and anything still
    # carrying an older number is gone from disk - which costs one indexed
    # DELETE instead of two sets the size of the library.

    def next_generation(self, root: str | Path) -> int:
        """Reserve a generation number for a sweep about to start."""
        root_key = root if isinstance(root, str) and os.path.normcase(root) == root else normalize_root(root)
        row = self.connection.execute(
            "SELECT COALESCE(MAX(seen_gen), 0) FROM books WHERE root = ?", (root_key,)
        ).fetchone()
        return int(row[0] or 0) + 1

    def prune_generation(self, root: str | Path, generation: int) -> int:
        """Drop every row under *root* that the sweep numbered *generation* missed."""
        root_key = root if isinstance(root, str) and os.path.normcase(root) == root else normalize_root(root)
        stale = [row[0] for row in self.connection.execute(
            "SELECT id FROM books WHERE root = ? AND seen_gen <> ?", (root_key, generation)
        )]
        if not stale:
            return 0
        cursor = self.connection.cursor()
        for chunk in _chunked(stale, 400):
            marks = ",".join("?" * len(chunk))
            self.drop_fts_rows(cursor, chunk)
            cursor.execute(f"DELETE FROM books       WHERE id      IN ({marks})", chunk)
        self.connection.commit()
        return len(stale)

    def record_scan(
        self,
        root: str | Path,
        *,
        generation: int,
        seconds: float,
        found: int,
        indexed: int,
        skipped: int,
        failed: int,
        cancelled: bool = False,
    ) -> None:
        """Remember how the sweep went, for the configuration window to show."""
        root_key = root if isinstance(root, str) and os.path.normcase(root) == root else normalize_root(root)
        self.connection.execute(
            "INSERT INTO scan_runs (root, generation, finished_at, seconds, found, indexed,"
            " skipped, failed, cancelled) VALUES (?,?,?,?,?,?,?,?,?)",
            (root_key, generation, time.time(), seconds, found, indexed, skipped, failed,
             1 if cancelled else 0),
        )
        self.connection.execute(
            "DELETE FROM scan_runs WHERE root = ? AND id NOT IN ("
            "  SELECT id FROM scan_runs WHERE root = ? ORDER BY finished_at DESC LIMIT 40)",
            (root_key, root_key),
        )
        self.connection.commit()

    def last_scan(self, root: str | Path) -> dict[str, Any] | None:
        root_key = normalize_root(root)
        row = self.connection.execute(
            "SELECT * FROM scan_runs WHERE root = ? ORDER BY finished_at DESC LIMIT 1", (root_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    # ── maintenance ────────────────────────────────────────────────────────

    def roots(self) -> list[tuple[str, int]]:
        """Every library this index knows about, with its book count."""
        return [(row[0], row[1]) for row in self.connection.execute(
            "SELECT root, COUNT(*) FROM books GROUP BY root ORDER BY COUNT(*) DESC"
        )]

    def clear_root(self, root: str | Path) -> int:
        """Forget one library entirely, so the next sweep starts from nothing."""
        root_key = normalize_root(root)
        paths = [row[0] for row in self.connection.execute(
            "SELECT path FROM books WHERE root = ?", (root_key,)
        )]
        self._forget(paths)
        self.connection.execute("DELETE FROM scan_runs WHERE root = ?", (root_key,))
        self.connection.commit()
        return len(paths)

    def optimize(self) -> None:
        """Compact the FTS indexes and the database file itself.

        ``VACUUM`` cannot run inside a transaction, and sqlite3 opens one
        implicitly for anything that writes, so the isolation level is dropped
        for the duration rather than letting the call fail.
        """
        self.connection.execute("INSERT INTO books_fts(books_fts) VALUES('optimize')")
        self.connection.execute("INSERT INTO content_fts(content_fts) VALUES('optimize')")
        self.connection.commit()
        previous = self.connection.isolation_level
        try:
            self.connection.isolation_level = None
            self.connection.execute("VACUUM")
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.connection.isolation_level = previous

    def database_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += (self.path.parent / f"{self.path.name}{suffix}").stat().st_size
            except OSError:
                continue
        return total

    def _forget(self, paths: Sequence[str]) -> None:
        cursor = self.connection.cursor()
        for chunk in _chunked(paths, 400):
            marks = ",".join("?" * len(chunk))
            ids = [row[0] for row in cursor.execute(
                f"SELECT id FROM books WHERE path IN ({marks})", chunk
            )]
            if not ids:
                continue
            id_marks = ",".join("?" * len(ids))
            self.drop_fts_rows(cursor, ids)
            cursor.execute(f"DELETE FROM books       WHERE id      IN ({id_marks})", ids)
        self.connection.commit()

    # ── reading ────────────────────────────────────────────────────────────

    def counts(self, root: str | Path) -> LibraryCounts:
        root_key = root if isinstance(root, str) and os.path.normcase(root) == root else normalize_root(root)
        counts = LibraryCounts()
        for row in self.connection.execute(
            "SELECT ext, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes,"
            " COALESCE(SUM(has_text),0) AS texts"
            " FROM books WHERE root = ? GROUP BY ext",
            (root_key,),
        ):
            counts.by_ext[row["ext"]] = row["n"]
            counts.total += row["n"]
            counts.bytes_total += row["bytes"]
            counts.with_text += row["texts"]
        counts.indexed = counts.total
        return counts

    def search(
        self,
        root: str | Path,
        query: str = "",
        *,
        mode: str = "meta",
        limit: int = 200,
        offset: int = 0,
        extensions: Sequence[str] | None = None,
    ) -> list[BookRow]:
        """Page through the shelf.  *mode* is ``meta``, ``content``, or ``all``."""
        root_key = normalize_root(root)
        expression, parsed_extensions = build_match_expression(query)
        wanted = list(extensions or []) + parsed_extensions

        if not expression:
            sql = "SELECT * FROM books WHERE root = ?"
            parameters: list[Any] = [root_key]
            if wanted:
                sql += f" AND ext IN ({','.join('?' * len(wanted))})"
                parameters.extend(wanted)
            sql += " ORDER BY title COLLATE NOCASE, name COLLATE NOCASE LIMIT ? OFFSET ?"
            parameters.extend([limit, offset])
            return [self._row(row) for row in self.connection.execute(sql, parameters)]

        sql, parameters = self._match_sql(root_key, expression, mode, wanted)
        sql += " LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
        try:
            rows = list(self.connection.execute(sql, parameters))
        except sqlite3.OperationalError:
            return []
        return [self._row(row) for row in rows]

    def count_matching(
        self,
        root: str | Path,
        query: str = "",
        *,
        mode: str = "meta",
        extensions: Sequence[str] | None = None,
    ) -> int:
        root_key = normalize_root(root)
        expression, parsed_extensions = build_match_expression(query)
        wanted = list(extensions or []) + parsed_extensions

        if not expression:
            sql = "SELECT COUNT(*) FROM books WHERE root = ?"
            parameters: list[Any] = [root_key]
            if wanted:
                sql += f" AND ext IN ({','.join('?' * len(wanted))})"
                parameters.extend(wanted)
            return int(self.connection.execute(sql, parameters).fetchone()[0])

        sql, parameters = self._match_sql(root_key, expression, mode, wanted, counting=True)
        try:
            return int(self.connection.execute(sql, parameters).fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    def _match_sql(
        self,
        root_key: str,
        expression: str,
        mode: str,
        extensions: Sequence[str],
        counting: bool = False,
    ) -> tuple[str, list[Any]]:
        """Build the FTS-joined query for *mode*.

        ``meta`` ranks on title/author/filename, ``content`` on the indexed text
        and carries a snippet back for display, and ``all`` unions the two so a
        topic hit and a title hit surface together.
        """
        parameters: list[Any] = []
        selection = "COUNT(*)" if counting else "b.*, '' AS snippet"

        if mode == "content":
            selection = "COUNT(*)" if counting else (
                "b.*, snippet(content_fts, 0, '', '', ' … ', 18) AS snippet"
            )
            sql = (
                f"SELECT {selection} FROM content_fts"
                " JOIN books b ON b.id = content_fts.book_id"
                " WHERE content_fts MATCH ? AND b.root = ?"
            )
            parameters.extend([expression, root_key])
        elif mode == "all":
            sql = (
                f"SELECT {selection} FROM books b WHERE b.root = ? AND (b.id IN ("
                "  SELECT book_id FROM books_fts WHERE books_fts MATCH ?"
                ") OR b.id IN ("
                "  SELECT book_id FROM content_fts WHERE content_fts MATCH ?"
                "))"
            )
            parameters.extend([root_key, expression, expression])
        else:
            sql = (
                f"SELECT {selection} FROM books_fts"
                " JOIN books b ON b.id = books_fts.book_id"
                " WHERE books_fts MATCH ? AND b.root = ?"
            )
            parameters.extend([expression, root_key])

        if extensions:
            sql += f" AND b.ext IN ({','.join('?' * len(extensions))})"
            parameters.extend(extensions)
        if not counting:
            sql += " ORDER BY bm25(books_fts)" if mode == "meta" else (
                " ORDER BY bm25(content_fts)" if mode == "content"
                else " ORDER BY b.title COLLATE NOCASE"
            )
        return sql, parameters

    @staticmethod
    def _row(row: sqlite3.Row) -> BookRow:
        keys = row.keys()
        return BookRow(
            path=row["path"],
            name=row["name"],
            ext=row["ext"],
            size=row["size"],
            title=row["title"] or Path(row["path"]).stem,
            author=row["author"] or "Unknown author",
            publisher=row["publisher"],
            language=row["language"],
            subjects=row["subjects"],
            pages=row["pages"],
            snippet=(row["snippet"] if "snippet" in keys else "") or "",
        )


def _chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
