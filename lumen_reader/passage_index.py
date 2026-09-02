"""Versioned passage storage layered additively onto the library index."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .passage_chunker import PassageChunker
from .passage_models import (
    CHUNKER_VERSION,
    EXTRACTOR_VERSION,
    COVERAGE_CAPPED,
    PassageBuildReport,
    SourceSection,
)
from .text_safety import clean_unicode_text


PASSAGE_SCHEMA_VERSION = 1
PASSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_documents (
    book_id              INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    active_revision      INTEGER,
    staging_revision     INTEGER,
    source_size          INTEGER NOT NULL,
    source_mtime_ns      INTEGER NOT NULL,
    source_fingerprint   TEXT NOT NULL,
    extractor_version    TEXT NOT NULL,
    chunker_version      TEXT NOT NULL,
    coverage             TEXT NOT NULL,
    coverage_reason      TEXT NOT NULL DEFAULT '',
    section_count        INTEGER NOT NULL DEFAULT 0,
    passage_count        INTEGER NOT NULL DEFAULT 0,
    char_count           INTEGER NOT NULL DEFAULT 0,
    token_count          INTEGER NOT NULL DEFAULT 0,
    indexed_at           REAL,
    status               TEXT NOT NULL,
    error_code           TEXT NOT NULL DEFAULT '',
    error_detail         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rag_revisions (
    book_id              INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    revision             INTEGER NOT NULL,
    state                TEXT NOT NULL,
    content_sha256       TEXT NOT NULL DEFAULT '',
    section_count        INTEGER NOT NULL DEFAULT 0,
    passage_count        INTEGER NOT NULL DEFAULT 0,
    char_count           INTEGER NOT NULL DEFAULT 0,
    token_count          INTEGER NOT NULL DEFAULT 0,
    created_at           REAL NOT NULL,
    completed_at         REAL,
    PRIMARY KEY (book_id, revision)
);
CREATE TABLE IF NOT EXISTS rag_sections (
    id                   INTEGER PRIMARY KEY,
    book_id              INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    ordinal              INTEGER NOT NULL,
    section_kind         TEXT NOT NULL,
    title                TEXT NOT NULL DEFAULT '',
    href                 TEXT NOT NULL DEFAULT '',
    fragment             TEXT NOT NULL DEFAULT '',
    page_start           INTEGER,
    page_end             INTEGER,
    char_count           INTEGER NOT NULL DEFAULT 0,
    content_sha256       TEXT NOT NULL,
    UNIQUE (book_id, revision, ordinal),
    FOREIGN KEY (book_id, revision)
      REFERENCES rag_revisions(book_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rag_passages (
    id                   INTEGER PRIMARY KEY,
    book_id              INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    section_id           INTEGER NOT NULL REFERENCES rag_sections(id) ON DELETE CASCADE,
    ordinal              INTEGER NOT NULL,
    section_ordinal      INTEGER NOT NULL,
    char_start           INTEGER NOT NULL,
    char_end             INTEGER NOT NULL,
    token_start          INTEGER NOT NULL DEFAULT 0,
    token_end            INTEGER NOT NULL DEFAULT 0,
    page_start           INTEGER,
    page_end             INTEGER,
    word_count           INTEGER NOT NULL,
    text_bytes           INTEGER NOT NULL,
    content_sha256       TEXT NOT NULL,
    UNIQUE (book_id, revision, ordinal),
    FOREIGN KEY (book_id, revision)
      REFERENCES rag_revisions(book_id, revision) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS rag_passages_book_revision
    ON rag_passages(book_id, revision, ordinal);
CREATE INDEX IF NOT EXISTS rag_passages_section
    ON rag_passages(section_id, ordinal);
CREATE INDEX IF NOT EXISTS rag_sections_book_revision
    ON rag_sections(book_id, revision, ordinal);
CREATE INDEX IF NOT EXISTS rag_revisions_state
    ON rag_revisions(state, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_passages_fts USING fts5(
    body,
    heading,
    book_title,
    author,
    subjects,
    language,
    passage_id UNINDEXED,
    book_id UNINDEXED,
    revision UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2",
    prefix = "2 3 4"
);
CREATE TABLE IF NOT EXISTS rag_fts_rowid (
    passage_id          INTEGER PRIMARY KEY REFERENCES rag_passages(id) ON DELETE CASCADE,
    fts_row             INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS rag_corpus_revisions (
    revision            INTEGER PRIMARY KEY,
    activated_at        REAL NOT NULL,
    root_set_hash       TEXT NOT NULL,
    document_count      INTEGER NOT NULL,
    passage_count       INTEGER NOT NULL,
    reason              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_vector_manifest (
    backend             TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    model_sha256        TEXT NOT NULL,
    dimensions          INTEGER NOT NULL,
    distance            TEXT NOT NULL,
    corpus_revision     INTEGER NOT NULL,
    vector_count        INTEGER NOT NULL,
    state               TEXT NOT NULL,
    path                 TEXT NOT NULL,
    built_at             REAL NOT NULL,
    PRIMARY KEY (backend, model_id)
);
"""


def ensure_passage_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(PASSAGE_SCHEMA)
    connection.execute(
        "INSERT INTO rag_meta(key,value) VALUES('schema_version',?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(PASSAGE_SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO rag_meta(key,value) VALUES('corpus_revision','0')"
    )


def passage_schema_available(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value FROM rag_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None and str(row[0]) == str(PASSAGE_SCHEMA_VERSION)


def corpus_revision(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM rag_meta WHERE key='corpus_revision'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def delete_book_passages(cursor: sqlite3.Cursor, book_ids: Sequence[int]) -> None:
    for book_id in book_ids:
        passage_ids = [int(row[0]) for row in cursor.execute(
            "SELECT id FROM rag_passages WHERE book_id=?", (int(book_id),)
        )]
        for start in range(0, len(passage_ids), 400):
            chunk = passage_ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cursor.execute(f"DELETE FROM rag_passages_fts WHERE rowid IN ({marks})", chunk)
            cursor.execute(f"DELETE FROM rag_fts_rowid WHERE passage_id IN ({marks})", chunk)
        cursor.execute("DELETE FROM rag_passages WHERE book_id=?", (int(book_id),))
        cursor.execute("DELETE FROM rag_sections WHERE book_id=?", (int(book_id),))
        cursor.execute("DELETE FROM rag_revisions WHERE book_id=?", (int(book_id),))
        cursor.execute("DELETE FROM rag_documents WHERE book_id=?", (int(book_id),))


def source_fingerprint(path: str, size: int, mtime_ns: int) -> str:
    raw = f"{path}\0{int(size)}\0{int(mtime_ns)}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def replace_bootstrap_passages(
    cursor: sqlite3.Cursor,
    *,
    book_id: int,
    record: dict[str, Any],
    chunker: PassageChunker | None = None,
) -> PassageBuildReport:
    body = clean_unicode_text(record.get("body", ""))
    coverage = COVERAGE_CAPPED if body else "metadata_only"
    reason = ("Existing sweep text budget; run `lumen-mcp index build` for complete coverage."
              if body else "No body text was extracted by the library sweep.")
    section = SourceSection(
        ordinal=0,
        kind="book_head",
        title=clean_unicode_text(record.get("title", "")),
        text=body,
    )
    writer = _CursorDocumentWriter(cursor, chunker or PassageChunker())
    return writer.replace(
        book_id=book_id,
        path=str(record.get("path", "")),
        size=int(record.get("size") or 0),
        mtime_ns=int(record.get("mtime_ns") or 0),
        metadata=record,
        sections=[section] if body else [],
        coverage=coverage,
        coverage_reason=reason,
        reason="bootstrap sweep activation",
    )


@dataclass(slots=True)
class _Totals:
    sections: int = 0
    passages: int = 0
    characters: int = 0
    words: int = 0
    tokens: int = 0


class _CursorDocumentWriter:
    def __init__(self, cursor: sqlite3.Cursor, chunker: PassageChunker):
        self.cursor = cursor
        self.chunker = chunker

    def replace(
        self,
        *,
        book_id: int,
        path: str,
        size: int,
        mtime_ns: int,
        metadata: dict[str, Any],
        sections: Iterable[SourceSection],
        coverage: str,
        coverage_reason: str,
        reason: str,
    ) -> PassageBuildReport:
        cursor = self.cursor
        row = cursor.execute(
            "SELECT COALESCE(MAX(revision),0) FROM rag_revisions WHERE book_id=?",
            (book_id,),
        ).fetchone()
        revision = int(row[0] or 0) + 1
        now = time.time()
        fingerprint = source_fingerprint(path, size, mtime_ns)
        cursor.execute(
            "INSERT INTO rag_revisions(book_id,revision,state,created_at) VALUES(?,?,?,?)",
            (book_id, revision, "staging", now),
        )
        cursor.execute(
            "INSERT INTO rag_documents(book_id,active_revision,staging_revision,source_size,"
            "source_mtime_ns,source_fingerprint,extractor_version,chunker_version,coverage,"
            "coverage_reason,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(book_id) DO UPDATE SET staging_revision=excluded.staging_revision,"
            " source_size=excluded.source_size,source_mtime_ns=excluded.source_mtime_ns,"
            " source_fingerprint=excluded.source_fingerprint,"
            " extractor_version=excluded.extractor_version,chunker_version=excluded.chunker_version,"
            " status='staging',error_code='',error_detail=''",
            (book_id, None, revision, size, mtime_ns, fingerprint, EXTRACTOR_VERSION,
             self.chunker.version, coverage, clean_unicode_text(coverage_reason)[:1000], "staging"),
        )

        totals = _Totals()
        digest = hashlib.sha256()
        passage_ordinal = 0
        for section in sections:
            clean_text = clean_unicode_text(section.text)
            if not clean_text.strip():
                continue
            section_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT INTO rag_sections(book_id,revision,ordinal,section_kind,title,href,"
                "fragment,page_start,page_end,char_count,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (book_id, revision, totals.sections, clean_unicode_text(section.kind)[:40],
                 clean_unicode_text(section.title)[:1000], clean_unicode_text(section.href)[:2000],
                 clean_unicode_text(section.fragment)[:500], section.page_start, section.page_end,
                 len(clean_text), section_hash),
            )
            section_id = int(cursor.lastrowid)
            for chunk in self.chunker.chunks(clean_text):
                body_bytes = chunk.text.encode("utf-8")
                cursor.execute(
                    "INSERT INTO rag_passages(book_id,revision,section_id,ordinal,section_ordinal,"
                    "char_start,char_end,token_start,token_end,page_start,page_end,word_count,"
                    "text_bytes,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, revision, section_id, passage_ordinal, totals.sections,
                     chunk.char_start, chunk.char_end, chunk.token_start, chunk.token_end,
                     section.page_start, section.page_end, chunk.word_count, len(body_bytes),
                     chunk.content_sha256),
                )
                passage_id = int(cursor.lastrowid)
                cursor.execute(
                    "INSERT INTO rag_passages_fts(rowid,body,heading,book_title,author,subjects,"
                    "language,passage_id,book_id,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (passage_id, chunk.text, clean_unicode_text(section.title),
                     clean_unicode_text(metadata.get("title", "")),
                     clean_unicode_text(metadata.get("author", "")),
                     clean_unicode_text(metadata.get("subjects", "")),
                     clean_unicode_text(metadata.get("language", "")),
                     passage_id, book_id, revision),
                )
                cursor.execute(
                    "INSERT INTO rag_fts_rowid(passage_id,fts_row) VALUES(?,?)",
                    (passage_id, passage_id),
                )
                digest.update(body_bytes)
                digest.update(b"\0")
                totals.passages += 1
                totals.characters += len(chunk.text)
                totals.words += chunk.word_count
                totals.tokens += max(0, chunk.token_end - chunk.token_start)
                passage_ordinal += 1
            totals.sections += 1

        content_hash = digest.hexdigest()
        current = cursor.execute(
            "SELECT active_revision FROM rag_documents WHERE book_id=?", (book_id,)
        ).fetchone()
        old_revision = int(current[0]) if current and current[0] is not None else None
        if old_revision is not None:
            cursor.execute(
                "UPDATE rag_revisions SET state='superseded' WHERE book_id=? AND revision=?",
                (book_id, old_revision),
            )
        cursor.execute(
            "UPDATE rag_revisions SET state='active',content_sha256=?,section_count=?,"
            "passage_count=?,char_count=?,token_count=?,completed_at=?"
            " WHERE book_id=? AND revision=? AND state='staging'",
            (content_hash, totals.sections, totals.passages, totals.characters, totals.tokens,
             now, book_id, revision),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("passage revision activation conflict")
        cursor.execute(
            "UPDATE rag_documents SET active_revision=?,staging_revision=NULL,status='active',"
            "coverage=?,coverage_reason=?,section_count=?,passage_count=?,char_count=?,"
            "token_count=?,indexed_at=?,error_code='',error_detail=''"
            " WHERE book_id=? AND staging_revision=?",
            (revision, coverage, clean_unicode_text(coverage_reason)[:1000], totals.sections,
             totals.passages, totals.characters, totals.tokens, now, book_id, revision),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("passage document activation conflict")
        _bump_corpus_revision(cursor, reason)
        return PassageBuildReport(
            book_id=book_id,
            revision=revision,
            coverage=coverage,
            sections=totals.sections,
            passages=totals.passages,
            characters=totals.characters,
            words=totals.words,
            content_sha256=content_hash,
        )


class StreamingDocumentWriter:
    """Build one inactive revision in section-sized transactions, then activate.

    The previous active revision remains queryable throughout.  A crash can
    leave staging rows, but cannot expose them; the next attempt marks that
    revision abandoned before starting a new one.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        book: dict[str, Any],
        coverage: str,
        coverage_reason: str = "",
        chunker: PassageChunker | None = None,
    ):
        self.connection = connection
        self.book = book
        self.book_id = int(book["id"])
        self.chunker = chunker or PassageChunker()
        self.coverage = coverage
        self.coverage_reason = clean_unicode_text(coverage_reason)[:1000]
        self.revision = 0
        self.totals = _Totals()
        self._digest = hashlib.sha256()
        self._passage_ordinal = 0
        self._closed = False
        self._begin()

    def _begin(self) -> None:
        ensure_passage_schema(self.connection)
        cursor = self.connection.cursor()
        cursor.execute(
            "UPDATE rag_revisions SET state='abandoned' WHERE book_id=? AND state='staging'",
            (self.book_id,),
        )
        row = cursor.execute(
            "SELECT COALESCE(MAX(revision),0) FROM rag_revisions WHERE book_id=?",
            (self.book_id,),
        ).fetchone()
        self.revision = int(row[0] or 0) + 1
        now = time.time()
        path = str(self.book.get("path", ""))
        size = int(self.book.get("size") or 0)
        mtime_ns = int(self.book.get("mtime_ns") or 0)
        cursor.execute(
            "INSERT INTO rag_revisions(book_id,revision,state,created_at) VALUES(?,?,?,?)",
            (self.book_id, self.revision, "staging", now),
        )
        cursor.execute(
            "INSERT INTO rag_documents(book_id,active_revision,staging_revision,source_size,"
            "source_mtime_ns,source_fingerprint,extractor_version,chunker_version,coverage,"
            "coverage_reason,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(book_id) DO UPDATE SET staging_revision=excluded.staging_revision,"
            " source_size=excluded.source_size,source_mtime_ns=excluded.source_mtime_ns,"
            " source_fingerprint=excluded.source_fingerprint,extractor_version=excluded.extractor_version,"
            " chunker_version=excluded.chunker_version,status='staging',error_code='',error_detail=''",
            (self.book_id, None, self.revision, size, mtime_ns,
             source_fingerprint(path, size, mtime_ns), EXTRACTOR_VERSION,
             self.chunker.version, self.coverage, self.coverage_reason, "staging"),
        )
        self.connection.commit()

    def add_section(self, section: SourceSection) -> int:
        if self._closed:
            raise RuntimeError("passage writer is closed")
        text = clean_unicode_text(section.text)
        if not text.strip():
            return 0
        cursor = self.connection.cursor()
        section_ordinal = self.totals.sections
        section_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                "INSERT INTO rag_sections(book_id,revision,ordinal,section_kind,title,href,"
                "fragment,page_start,page_end,char_count,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.book_id, self.revision, section_ordinal,
                 clean_unicode_text(section.kind)[:40], clean_unicode_text(section.title)[:1000],
                 clean_unicode_text(section.href)[:2000], clean_unicode_text(section.fragment)[:500],
                 section.page_start, section.page_end, len(text), section_hash),
            )
            section_id = int(cursor.lastrowid)
            section_passages = 0
            section_characters = section_words = section_tokens = 0
            digest_parts: list[bytes] = []
            for chunk in self.chunker.chunks(text):
                body_bytes = chunk.text.encode("utf-8")
                cursor.execute(
                    "INSERT INTO rag_passages(book_id,revision,section_id,ordinal,section_ordinal,"
                    "char_start,char_end,token_start,token_end,page_start,page_end,word_count,"
                    "text_bytes,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.book_id, self.revision, section_id, self._passage_ordinal,
                     section_ordinal, chunk.char_start, chunk.char_end, chunk.token_start,
                     chunk.token_end, section.page_start, section.page_end, chunk.word_count,
                     len(body_bytes), chunk.content_sha256),
                )
                passage_id = int(cursor.lastrowid)
                cursor.execute(
                    "INSERT INTO rag_passages_fts(rowid,body,heading,book_title,author,subjects,"
                    "language,passage_id,book_id,revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (passage_id, chunk.text, clean_unicode_text(section.title),
                     clean_unicode_text(self.book.get("title", "")),
                     clean_unicode_text(self.book.get("author", "")),
                     clean_unicode_text(self.book.get("subjects", "")),
                     clean_unicode_text(self.book.get("language", "")),
                     passage_id, self.book_id, self.revision),
                )
                cursor.execute(
                    "INSERT INTO rag_fts_rowid(passage_id,fts_row) VALUES(?,?)",
                    (passage_id, passage_id),
                )
                digest_parts.extend((body_bytes, b"\0"))
                section_passages += 1
                section_characters += len(chunk.text)
                section_words += chunk.word_count
                section_tokens += max(0, chunk.token_end - chunk.token_start)
                self._passage_ordinal += 1
            cursor.execute(
                "UPDATE rag_revisions SET section_count=section_count+1,"
                "passage_count=passage_count+?,char_count=char_count+?,token_count=token_count+?"
                " WHERE book_id=? AND revision=? AND state='staging'",
                (section_passages, section_characters, section_tokens,
                 self.book_id, self.revision),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("staging passage revision disappeared")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        for part in digest_parts:
            self._digest.update(part)
        self.totals.sections += 1
        self.totals.passages += section_passages
        self.totals.characters += section_characters
        self.totals.words += section_words
        self.totals.tokens += section_tokens
        return section_passages

    def activate(
        self,
        *,
        coverage: str | None = None,
        coverage_reason: str | None = None,
        reason: str = "complete passage activation",
    ) -> PassageBuildReport:
        if self._closed:
            raise RuntimeError("passage writer is closed")
        final_coverage = clean_unicode_text(coverage or self.coverage)
        final_reason = clean_unicode_text(
            self.coverage_reason if coverage_reason is None else coverage_reason
        )[:1000]
        cursor = self.connection.cursor()
        now = time.time()
        content_hash = self._digest.hexdigest()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT active_revision,staging_revision FROM rag_documents WHERE book_id=?",
                (self.book_id,),
            ).fetchone()
            if row is None or int(row[1] or 0) != self.revision:
                raise sqlite3.IntegrityError("passage activation lost its staging lease")
            old_revision = int(row[0]) if row[0] is not None else None
            if old_revision is not None:
                cursor.execute(
                    "UPDATE rag_revisions SET state='superseded' WHERE book_id=? AND revision=?",
                    (self.book_id, old_revision),
                )
            cursor.execute(
                "UPDATE rag_revisions SET state='active',content_sha256=?,completed_at=?"
                " WHERE book_id=? AND revision=? AND state='staging'",
                (content_hash, now, self.book_id, self.revision),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("passage revision activation conflict")
            cursor.execute(
                "UPDATE rag_documents SET active_revision=?,staging_revision=NULL,status='active',"
                "coverage=?,coverage_reason=?,section_count=?,passage_count=?,char_count=?,"
                "token_count=?,indexed_at=?,error_code='',error_detail=''"
                " WHERE book_id=? AND staging_revision=?",
                (self.revision, final_coverage, final_reason, self.totals.sections,
                 self.totals.passages, self.totals.characters, self.totals.tokens, now,
                 self.book_id, self.revision),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("passage document activation conflict")
            _bump_corpus_revision(cursor, reason)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        self._closed = True
        return PassageBuildReport(
            book_id=self.book_id,
            revision=self.revision,
            coverage=final_coverage,
            sections=self.totals.sections,
            passages=self.totals.passages,
            characters=self.totals.characters,
            words=self.totals.words,
            content_sha256=content_hash,
        )

    def fail(self, code: str, detail: str) -> None:
        if self._closed:
            return
        cursor = self.connection.cursor()
        cursor.execute(
            "UPDATE rag_revisions SET state='failed',completed_at=?"
            " WHERE book_id=? AND revision=? AND state='staging'",
            (time.time(), self.book_id, self.revision),
        )
        cursor.execute(
            "UPDATE rag_documents SET staging_revision=NULL,"
            "status=CASE WHEN active_revision IS NULL THEN 'failed' ELSE 'active' END,"
            "error_code=?,error_detail=? WHERE book_id=? AND staging_revision=?",
            (clean_unicode_text(code)[:80], clean_unicode_text(detail)[:1000],
             self.book_id, self.revision),
        )
        self.connection.commit()
        self._closed = True


def _bump_corpus_revision(cursor: sqlite3.Cursor, reason: str) -> int:
    row = cursor.execute(
        "SELECT value FROM rag_meta WHERE key='corpus_revision'"
    ).fetchone()
    revision = int(row[0] or 0) + 1 if row else 1
    cursor.execute(
        "INSERT INTO rag_meta(key,value) VALUES('corpus_revision',?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(revision),),
    )
    counts = cursor.execute(
        "SELECT COUNT(*),COALESCE(SUM(passage_count),0) FROM rag_documents"
        " WHERE active_revision IS NOT NULL"
    ).fetchone()
    roots = [str(row[0]) for row in cursor.execute(
        "SELECT DISTINCT root FROM books ORDER BY root"
    )]
    root_hash = hashlib.sha256(json.dumps(roots).encode("utf-8")).hexdigest()
    cursor.execute(
        "INSERT INTO rag_corpus_revisions(revision,activated_at,root_set_hash,document_count,"
        "passage_count,reason) VALUES(?,?,?,?,?,?)",
        (revision, time.time(), root_hash, int(counts[0] or 0), int(counts[1] or 0),
         clean_unicode_text(reason)[:200]),
    )
    return revision
