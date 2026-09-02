"""Memory-bounded complete EPUB-spine/PDF-page passage index builder."""

from __future__ import annotations

import os
import posixpath
import sqlite3
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .passage_index import StreamingDocumentWriter, ensure_passage_schema, source_fingerprint
from .passage_models import (
    CHUNKER_VERSION,
    EXTRACTOR_VERSION,
    COVERAGE_COMPLETE,
    COVERAGE_FAILED,
    COVERAGE_LOCKED,
    COVERAGE_METADATA_ONLY,
    COVERAGE_NO_TEXT,
    PassageBuildReport,
    SourceSection,
)
from .text_safety import clean_unicode_text


_MAX_EPUB_UNCOMPRESSED = 512 * 1024 * 1024
_MAX_SECTION_BYTES = 64 * 1024 * 1024


class SourceChangedError(RuntimeError):
    pass


class LockedDocumentError(RuntimeError):
    pass


@dataclass(slots=True)
class BuildSummary:
    examined: int = 0
    built: int = 0
    skipped: int = 0
    failed: int = 0
    sections: int = 0
    passages: int = 0
    characters: int = 0
    seconds: float = 0.0


class PassageBuilder:
    def __init__(self, database: str | Path):
        self.database = Path(database)

    def build(
        self,
        *,
        roots: Sequence[str] = (),
        book_ids: Sequence[int] = (),
        force: bool = False,
        limit: int = 0,
        progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> BuildSummary:
        started = time.monotonic()
        summary = BuildSummary()
        stop = cancelled or (lambda: False)
        connection = sqlite3.connect(str(self.database), timeout=60.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        ensure_passage_schema(connection)
        connection.commit()
        try:
            rows = self._books(connection, roots=roots, book_ids=book_ids, limit=limit)
            for row in rows:
                if stop():
                    break
                summary.examined += 1
                book = dict(row)
                if not force and self._is_current_complete(connection, book):
                    summary.skipped += 1
                    continue
                say = progress or (lambda _message: None)
                say(f"Building passages: {book['name']}")
                writer = StreamingDocumentWriter(
                    connection,
                    book=book,
                    coverage=COVERAGE_COMPLETE,
                )
                try:
                    report = self._build_one(writer, book, stop)
                except LockedDocumentError as exception:
                    report = writer.activate(
                        coverage=COVERAGE_LOCKED,
                        coverage_reason=str(exception),
                        reason="locked document coverage activation",
                    )
                except BaseException as exception:
                    writer.fail(type(exception).__name__.upper(), str(exception))
                    summary.failed += 1
                    say(f"Passage build failed: {book['name']} — {type(exception).__name__}: {exception}")
                    continue
                summary.built += 1
                summary.sections += report.sections
                summary.passages += report.passages
                summary.characters += report.characters
        finally:
            connection.close()
        summary.seconds = time.monotonic() - started
        return summary

    @staticmethod
    def _books(
        connection: sqlite3.Connection,
        *,
        roots: Sequence[str],
        book_ids: Sequence[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM books WHERE 1=1"
        parameters: list[object] = []
        if roots:
            marks = ",".join("?" for _ in roots)
            sql += f" AND root IN ({marks})"
            parameters.extend(roots)
        if book_ids:
            marks = ",".join("?" for _ in book_ids)
            sql += f" AND id IN ({marks})"
            parameters.extend(int(value) for value in book_ids)
        sql += " ORDER BY root,path,id"
        if limit > 0:
            sql += " LIMIT ?"
            parameters.append(int(limit))
        return list(connection.execute(sql, parameters))

    @staticmethod
    def _is_current_complete(connection: sqlite3.Connection, book: dict[str, object]) -> bool:
        row = connection.execute(
            "SELECT source_fingerprint,extractor_version,chunker_version,coverage,status"
            " FROM rag_documents WHERE book_id=?",
            (int(book["id"]),),
        ).fetchone()
        if row is None:
            return False
        expected = source_fingerprint(
            str(book.get("path", "")), int(book.get("size") or 0), int(book.get("mtime_ns") or 0)
        )
        return (
            row[0] == expected
            and row[1] == EXTRACTOR_VERSION
            and row[2] == CHUNKER_VERSION
            and row[3] in {COVERAGE_COMPLETE, COVERAGE_NO_TEXT, COVERAGE_LOCKED}
            and row[4] == "active"
        )

    @staticmethod
    def _build_one(
        writer: StreamingDocumentWriter,
        book: dict[str, object],
        cancelled: Callable[[], bool],
    ) -> PassageBuildReport:
        path = Path(str(book["path"]))
        before = path.stat()
        if before.st_size != int(book["size"]) or before.st_mtime_ns != int(book["mtime_ns"]):
            raise SourceChangedError("source changed after the catalog sweep; sweep again first")
        suffix = path.suffix.casefold()
        sections = _epub_sections(path) if suffix == ".epub" else _pdf_sections(path)
        any_text = False
        for section in sections:
            if cancelled():
                raise InterruptedError("passage build cancelled")
            if section.text.strip():
                any_text = True
                writer.add_section(section)
        after = path.stat()
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise SourceChangedError("source changed while passages were being extracted")
        if not any_text:
            return writer.activate(
                coverage=COVERAGE_NO_TEXT,
                coverage_reason="No extractable text layer was found; OCR is not run implicitly.",
                reason="no-text coverage activation",
            )
        return writer.activate(reason="complete source passage activation")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _normalize_href(base: str, href: str) -> str:
    path = unquote(urlsplit(href).path).replace("\\", "/")
    normalized = posixpath.normpath(posixpath.join(base, path)).lstrip("/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or any(":" in part for part in pure.parts):
        raise ValueError("unsafe EPUB spine path")
    return pure.as_posix()


def _epub_sections(path: Path) -> Iterator[SourceSection]:
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > _MAX_EPUB_UNCOMPRESSED:
            raise ValueError("EPUB exceeds the 512 MiB safe expansion limit")
        names = {PurePosixPath(item.filename.replace("\\", "/")).as_posix(): item
                 for item in archive.infolist() if not item.is_dir()}
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((node.get("full-path", "") for node in container.iter()
                         if _local(node.tag) == "rootfile" and node.get("full-path")), "")
        if not rootfile:
            raise ValueError("EPUB container has no package rootfile")
        opf_name = _normalize_href("", rootfile)
        package = ET.fromstring(archive.read(opf_name))
        opf_dir = posixpath.dirname(opf_name)
        manifest: dict[str, tuple[str, str]] = {}
        spine: list[str] = []
        for node in package.iter():
            if _local(node.tag) == "item" and node.get("id") and node.get("href"):
                manifest[str(node.get("id"))] = (
                    _normalize_href(opf_dir, str(node.get("href"))),
                    str(node.get("media-type") or "").lower(),
                )
            elif _local(node.tag) == "itemref" and node.get("idref"):
                spine.append(str(node.get("idref")))
        ordinal = 0
        for item_id in spine:
            href, media = manifest.get(item_id, ("", ""))
            if not href or ("html" not in media and "xml" not in media):
                continue
            item = names.get(href)
            if item is None:
                continue
            if item.file_size > _MAX_SECTION_BYTES:
                raise ValueError(f"EPUB section exceeds 64 MiB: {href}")
            raw = archive.read(item)
            soup = BeautifulSoup(raw, "html.parser")
            for blocked in soup(["script", "style", "iframe", "object", "form"]):
                blocked.decompose()
            body = soup.body or soup
            text = clean_unicode_text(body.get_text(" ", strip=True))
            heading_node = body.find(["h1", "h2", "h3", "title"])
            title = clean_unicode_text(heading_node.get_text(" ", strip=True) if heading_node else item_id)
            yield SourceSection(
                ordinal=ordinal,
                kind="epub_spine",
                title=title or f"Section {ordinal + 1}",
                text=text,
                href=href,
            )
            ordinal += 1


def _pdf_sections(path: Path) -> Iterator[SourceSection]:
    import pymupdf

    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise LockedDocumentError("PDF is password protected; credentials are never requested by MCP.")
        page_titles: dict[int, str] = {}
        try:
            for level, title, page in document.get_toc(simple=True):
                del level
                if 1 <= int(page) <= document.page_count:
                    page_titles.setdefault(int(page), clean_unicode_text(str(title)))
        except Exception:
            pass
        for index, page in enumerate(document):
            try:
                text = clean_unicode_text(page.get_text("text", sort=True))
            except Exception:
                text = ""
            page_number = index + 1
            yield SourceSection(
                ordinal=index,
                kind="pdf_page",
                title=page_titles.get(page_number) or f"Page {page_number}",
                text=text,
                href=f"page-{page_number}.html",
                page_start=page_number,
                page_end=page_number,
            )
