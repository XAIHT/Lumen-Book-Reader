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
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from xml.etree import ElementTree as ET

BOOK_SUFFIXES = {".epub", ".pdf"}

#: How much extracted text is indexed per book for topic search.  Topic and
#: subject matter are overwhelmingly decided in the front matter, so a bounded
#: head keeps the index proportionate to the shelf instead of to the corpus.
DEFAULT_TEXT_BUDGET = 250_000

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


def _pdf_record(path: Path, text_budget: int) -> dict[str, Any]:
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
            for page in document:
                if budget <= 0:
                    break
                try:
                    text = _SPACE_RE.sub(" ", page.get_text()).strip()
                except Exception:
                    continue
                if not text:
                    continue
                chunks.append(text[:budget])
                budget -= len(text)
            record["body"] = " ".join(chunks)[:text_budget]

    return record


def extract_book(job: tuple[str, str, int]) -> dict[str, Any]:
    """Worker entry point: pull metadata and indexable text from one book."""
    path_text, suffix, text_budget = job
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
        record = _pdf_record(path, text_budget) if suffix == ".pdf" else _epub_record(path, text_budget)
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
    error       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS books_root  ON books(root);
CREATE INDEX IF NOT EXISTS books_ext   ON books(root, ext);
CREATE INDEX IF NOT EXISTS books_title ON books(root, title COLLATE NOCASE);

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
"""


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
        self.connection.executescript(SCHEMA)
        self.connection.commit()

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
    ) -> LibraryCounts:
        """Index every book under *root*, reusing rows that have not changed."""
        root_key = normalize_root(root)
        root_path = Path(root).expanduser().resolve()
        stop = cancelled or (lambda: False)

        def report(phase: str, done: int = 0, total: int = 0, detail: str = "") -> None:
            if progress is not None:
                progress(ScanProgress(phase, done, total, detail))

        # Phase 1 - walk.  Cheap even on a very large tree.
        report("walk", detail=str(root_path))
        found: dict[str, tuple[int, int, str]] = {}
        for path_text, size, mtime_ns, suffix in walk_library(root_path):
            if stop():
                return self.counts(root_key)
            found[path_text] = (size, mtime_ns, suffix)
            if len(found) % 500 == 0:
                report("walk", len(found), 0, f"{len(found):,} books found")
        report("walk", len(found), len(found), f"{len(found):,} books found")

        # Phase 2 - diff against what is already indexed.
        known: dict[str, tuple[int, int, int]] = {}
        for row in self.connection.execute(
            "SELECT path, size, mtime_ns, has_text FROM books WHERE root = ?", (root_key,)
        ):
            known[row["path"]] = (row["size"], row["mtime_ns"], row["has_text"])

        want_text = with_text and self.text_budget > 0
        stale: list[tuple[str, str, int]] = []
        for path_text, (size, mtime_ns, suffix) in found.items():
            previous = known.get(path_text)
            if previous is not None:
                same_file = previous[0] == size and previous[1] == mtime_ns
                text_ok = previous[2] == 1 or not want_text
                if same_file and text_ok:
                    continue
            stale.append((path_text, suffix, self.text_budget if want_text else 0))

        vanished = [path for path in known if path not in found]
        if vanished:
            self._forget(vanished)

        if not stale:
            report("done", len(found), len(found), "index already current")
            return self.counts(root_key)

        # Phase 3 - parallel extraction across every core.
        total = len(stale)
        report("extract", 0, total, f"0 / {total:,}")
        max_workers = workers if workers is not None else max(1, (os.cpu_count() or 4))
        max_workers = min(max_workers, 61, total)  # Windows caps wait handles at 61

        done = 0
        batch: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(extract_book, job): job[0] for job in stale}
            for future in as_completed(futures):
                if stop():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    record = future.result()
                except Exception as exception:
                    record = {
                        "path": futures[future], "ok": False,
                        "error": f"{type(exception).__name__}: {exception}"[:400],
                        "title": Path(futures[future]).stem, "author": "", "publisher": "",
                        "language": "", "subjects": "", "description": "", "pages": 0, "body": "",
                    }
                size, mtime_ns, suffix = found[record["path"]]
                record["root"] = root_key
                record["size"] = size
                record["mtime_ns"] = mtime_ns
                record["ext"] = suffix
                batch.append(record)
                done += 1
                if len(batch) >= 400:
                    self._write(batch)
                    batch.clear()
                    report("extract", done, total, f"{done:,} / {total:,}")
        if batch:
            self._write(batch)
        report("done", done, total, f"{done:,} indexed")
        return self.counts(root_key)

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
            cursor.execute(f"DELETE FROM books_fts   WHERE book_id IN ({id_marks})", ids)
            cursor.execute(f"DELETE FROM content_fts WHERE book_id IN ({id_marks})", ids)
            cursor.execute(f"DELETE FROM books       WHERE id      IN ({id_marks})", ids)
        self.connection.commit()

    def _write(self, records: Iterable[dict[str, Any]]) -> None:
        cursor = self.connection.cursor()
        for record in records:
            path_text = record["path"]
            name = os.path.basename(path_text)
            has_text = 1 if record.get("body") else 0
            existing = cursor.execute("SELECT id FROM books WHERE path = ?", (path_text,)).fetchone()
            if existing is not None:
                book_id = existing[0]
                cursor.execute("DELETE FROM books_fts   WHERE book_id = ?", (book_id,))
                cursor.execute("DELETE FROM content_fts WHERE book_id = ?", (book_id,))
                cursor.execute(
                    """UPDATE books SET root=?, name=?, ext=?, size=?, mtime_ns=?, title=?,
                       author=?, publisher=?, language=?, subjects=?, description=?, pages=?,
                       has_text=?, ok=?, error=? WHERE id=?""",
                    (record["root"], name, record["ext"], record["size"], record["mtime_ns"],
                     record["title"], record["author"], record["publisher"], record["language"],
                     record["subjects"], record["description"], record["pages"], has_text,
                     1 if record.get("ok", True) else 0, record.get("error", ""), book_id),
                )
            else:
                cursor.execute(
                    """INSERT INTO books (root, path, name, ext, size, mtime_ns, title, author,
                       publisher, language, subjects, description, pages, has_text, ok, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record["root"], path_text, name, record["ext"], record["size"],
                     record["mtime_ns"], record["title"], record["author"], record["publisher"],
                     record["language"], record["subjects"], record["description"],
                     record["pages"], has_text, 1 if record.get("ok", True) else 0,
                     record.get("error", "")),
                )
                book_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO books_fts (title, author, name, subjects, publisher, book_id)"
                " VALUES (?,?,?,?,?,?)",
                (record["title"], record["author"], name, record["subjects"],
                 record["publisher"], book_id),
            )
            if has_text:
                cursor.execute(
                    "INSERT INTO content_fts (body, book_id) VALUES (?,?)",
                    (record["body"], book_id),
                )
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
