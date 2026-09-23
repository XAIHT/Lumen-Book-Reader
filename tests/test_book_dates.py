"""Date contract, migration, real sweep/MCP and visible native shelf tests."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import anyio
import pytest

from lumen_reader.book_dates import (
    DATE_COLUMNS, creation_ns, date_fields, date_predicate, epub_publication,
    pdf_publication, publication_date,
)
from lumen_reader.library_index import LibraryIndex, extract_book, normalize_root
from lumen_reader.passage_builder import PassageBuilder
from lumen_reader.retrieval.contracts import RetrievalError
from lumen_reader.retrieval.service import RetrievalService
from lumen_reader.runtime_paths import RuntimePaths
from test_library_index import make_epub, make_pdf


@pytest.mark.parametrize("value,display,first,last", [
    ("1984", "1984", "1984-01-01", "1984-12-31"),
    ("2024-02", "2024-02", "2024-02-01", "2024-02-29"),
    ("2020-05-03", "2020-05-03", "2020-05-03", "2020-05-03"),
    ("2020-05-03T23:55:10Z", "2020-05-03", "2020-05-03", "2020-05-03"),
    ("2023-02-29", "", None, None), ("0000", "", None, None),
    ("2024-00", "", None, None), ("2024-00-15", "", None, None),
    ("2024-01-00", "", None, None), ("2024-00-00", "", None, None),
    ("about 1984", "", None, None), (None, "", None, None),
])
def test_publication_precision(value, display, first, last):
    assert publication_date(value) == (display, first, last)


def test_epub_prefers_publication_and_rejects_modification():
    root = ET.fromstring('''<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:opf="http://www.idpf.org/2007/opf">
      <dc:date opf:event="modification">2026</dc:date><dc:date>2000</dc:date>
      <dc:date opf:event="publication">1999-05</dc:date></metadata>''')
    assert epub_publication(root)["published_date"] == "1999-05"
    assert pdf_publication('<x xmlns:xmp="http://ns.adobe.com/xap/1.0/"><xmp:CreateDate>2026</xmp:CreateDate></x>') == {}
    assert pdf_publication('<!DOCTYPE x [<!ENTITY a "1999">]><x/>') == {}


def test_creation_uses_birth_not_posix_inode_change(monkeypatch):
    assert creation_ns(SimpleNamespace(st_birthtime_ns=123, st_ctime_ns=456)) == 123
    monkeypatch.setattr("lumen_reader.book_dates.os.name", "posix")
    assert creation_ns(SimpleNamespace(st_ctime_ns=456)) is None


@pytest.mark.parametrize("filters", [
    {"publication_from": "2020"}, {"created_from": "2020"},
    {"modified_from": "2020-01-01T12:00:00"},
    {"published_from": "2021", "published_to": "2020"},
    {"created_from": "2021-01-01", "created_to": "2020-01-01"},
    {"published_from": "2000 OR 1=1"}, {"modified_to": "9999-12-31"},
    {"published_from": 2020}, {"modified_from": "2020-13-99"},
    {"published_from": "2024-00"}, {"published_to": "2024-01-00"},
])
def test_date_filter_validation(filters):
    with pytest.raises((ValueError, OverflowError)):
        date_predicate(filters)


def create_corpus(base: Path) -> tuple[RuntimePaths, Path]:
    root = base / "books"
    for name, published in (("Alpha", "1984"), ("Beta", "2024-02"), ("Gamma", "2025-03-14")):
        book = make_epub(root / f"{name}.epub", name, "Date Author", "chronology research " * 60, published=published)
        year = int(published[:4])
        stamp = datetime(year, 6, 15, 12, tzinfo=timezone.utc).timestamp()
        os.utime(book, (stamp, stamp))
    make_pdf(root / "Unknown.pdf", "Unknown publication", "chronology research")
    data = base / "state"
    paths = RuntimePaths(data, data / "reader-state.json", data / "library-index.db", data / "logs", data / "mcp-cache")
    with LibraryIndex(paths.index_file) as index:
        index.scan(root, workers=1)
        assert index.counts(root).total == 4
    return paths, root


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return create_corpus(tmp_path_factory.mktemp("book-dates"))


def test_real_extraction_file_dates_and_missing_publication(corpus):
    paths, root = corpus
    with LibraryIndex(paths.index_file) as index:
        rows = index.search(root, sort="published")
        assert [row.title for row in rows] == ["Gamma", "Beta", "Alpha", "Unknown publication"]
        for row in rows:
            stat = Path(row.path).stat()
            assert row.created_ns == creation_ns(stat)
            assert row.mtime_ns == stat.st_mtime_ns
            assert row.date_metadata_version == 1
        assert rows[-1].published_date == ""
        assert rows[1].published_date == "2024-02"


@pytest.mark.parametrize("mode,query", [("meta", "Date"), ("content", "chronology"), ("all", "chronology"), ("meta", "")])
def test_catalog_range_filters_sort_and_pagination(corpus, mode, query):
    paths, root = corpus
    with LibraryIndex(paths.index_file) as index:
        filters = {"published_from": "2024", "published_to": "2025"}
        assert index.count_matching(root, query, mode=mode, date_filters=filters) == 2
        rows = []
        for offset in range(2):
            rows.extend(index.search(root, query, mode=mode, date_filters=filters, sort="published_asc", limit=1, offset=offset))
        assert [row.title for row in rows] == ["Beta", "Gamma"]
        assert index.search(root, date_filters={"published_from": "1984-08", "published_to": "1984-08"})[0].title == "Alpha"
        assert index.search(root, date_filters={"modified_from": "2024-06-15", "modified_to": "2024-06-15"})[0].title == "Beta"


def test_pdf_explicit_xmp_publication_not_creation(tmp_path):
    import pymupdf
    path = make_pdf(tmp_path / "date.pdf")
    with pymupdf.open(path) as doc:
        doc.set_metadata({"creationDate": "D:20260923120000Z"})
        doc.set_xml_metadata('''<x:xmpmeta xmlns:x="adobe:ns:meta/">
          <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
          <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:date><rdf:Seq><rdf:li>1995</rdf:li></rdf:Seq></dc:date>
          </rdf:Description></rdf:RDF></x:xmpmeta>''')
        doc.saveIncr()
    assert extract_book((str(path), ".pdf", 0))["published_date"] == "1995"


def test_date_indexes_are_real_btrees(corpus):
    paths, root = corpus
    with LibraryIndex(paths.index_file) as index:
        for field, expected in (("published_start", "books_published"), ("published_end", "books_published_end"),
                                ("created_ns", "books_created"), ("mtime_ns", "books_modified")):
            plan = index.connection.execute(
                f"EXPLAIN QUERY PLAN SELECT id FROM books WHERE root=? AND {field}>=?",
                (normalize_root(root), "2020-01-01" if field.startswith("published") else 0),
            ).fetchall()
            assert any(expected in str(tuple(row)) for row in plan)


def test_legacy_date_query_fails_explicitly_without_mutating(corpus, tmp_path):
    paths, root = corpus
    data = tmp_path / "legacy"
    data.mkdir()
    target = data / "library-index.db"
    with sqlite3.connect(paths.index_file) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
        for name in ("books_published", "books_published_end", "books_created"):
            dest.execute(f"DROP INDEX {name}")
        for column in DATE_COLUMNS:
            dest.execute(f"ALTER TABLE books DROP COLUMN {column}")
    service = RetrievalService(RuntimePaths(data, data / "state.json", target, data / "logs", data / "cache"))
    assert service.glob("**/*")["hits"]
    assert service.status()["catalog"]["dates"]["pending_books"] == 4
    for operation, argument in ((service.glob, "**/*"), (service.search, "chronology"), (service.grep, "chronology")):
        with pytest.raises(RetrievalError) as error:
            operation(argument, date_filters={"published_from": "2000"})
        assert error.value.code == "DATE_INDEX_REQUIRED"
    with sqlite3.connect(target) as connection:
        assert "created_ns" not in {row[1] for row in connection.execute("PRAGMA table_info(books)")}


def test_failed_metadata_backfill_keeps_book_and_text(tmp_path, monkeypatch):
    paths, root = create_corpus(tmp_path)
    from lumen_reader import library_index
    with LibraryIndex(paths.index_file) as index:
        before = [tuple(row) for row in index.connection.execute("SELECT rowid,* FROM content_fts ORDER BY rowid")]
        index.connection.execute("UPDATE books SET date_metadata_version=0")
        index.connection.commit()
        def fail(*args):
            raise ValueError("malformed optional metadata")
        monkeypatch.setattr(library_index, "_epub_record", fail)
        index.scan(root, workers=1)
        assert [tuple(row) for row in index.connection.execute("SELECT rowid,* FROM content_fts ORDER BY rowid")] == before
        assert index.connection.execute("SELECT COUNT(*) FROM books WHERE date_metadata_version=0").fetchone()[0] == 3
        assert index.search(root, "chronology", mode="content")


def test_old_index_migration_backfills_without_replacing_fts_or_passages(tmp_path, corpus):
    paths, root = corpus
    target = tmp_path / "legacy.db"
    with sqlite3.connect(paths.index_file) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    PassageBuilder(target).build(force=True)
    with sqlite3.connect(target) as db:
        before = db.execute("SELECT rowid,* FROM content_fts ORDER BY rowid").fetchall()
        passages = db.execute("SELECT * FROM rag_passages ORDER BY id").fetchall()
        ids = db.execute("SELECT id,path FROM books ORDER BY id").fetchall()
        for name in ("books_published", "books_published_end", "books_created", "books_modified"):
            db.execute(f"DROP INDEX {name}")
        for column in DATE_COLUMNS:
            db.execute(f"ALTER TABLE books DROP COLUMN {column}")
    with LibraryIndex(target) as index:
        assert index.search(root)[0].date_metadata_version == 0
        index.scan(root, workers=1)
        assert index.connection.execute("SELECT COUNT(*) FROM books WHERE date_metadata_version=1").fetchone()[0] == 4
        assert [tuple(row) for row in index.connection.execute("SELECT rowid,* FROM content_fts ORDER BY rowid")] == before
        assert [tuple(row) for row in index.connection.execute("SELECT * FROM rag_passages ORDER BY id")] == passages
        assert [tuple(row) for row in index.connection.execute("SELECT id,path FROM books ORDER BY id")] == ids
        index.scan(root, workers=1)
        assert index.last_scan(root)["skipped"] == 4


@pytest.mark.parametrize("passages", [False, True])
def test_mcp_service_dates_filter_all_search_paths(tmp_path, passages):
    paths, root = create_corpus(tmp_path)
    if passages:
        PassageBuilder(paths.index_file).build(force=True)
    service = RetrievalService(paths)
    filters = {"published_from": "2024", "modified_to": "2024-12-31"}
    for result in (service.glob("**/*", date_filters=filters),
                   service.search("chronology", strategy="lexical", date_filters=filters),
                   service.grep("chronology", date_filters=filters)):
        assert result["hits"]
        for hit in result["hits"]:
            book = hit.get("book", hit)
            assert book["title"] == "Beta"
            assert book["published_date"] == "2024-02"
            assert book["created_at"].endswith("Z")
    if passages:
        sections = service.glob("*c1*", include_sections=True, date_filters=filters)
        assert sections["hits"] and all(hit["title"] == "Beta" for hit in sections["hits"])
    status = service.status()["catalog"]["dates"]
    assert status["indexed_books"] == 4 and status["pending_books"] == 0
    first = service.glob("**/*", sort="published", limit=1)
    second = service.glob("**/*", sort="published", limit=1, cursor=first["next_cursor"])
    assert first["hits"][0]["title"] == "Gamma"
    assert second["hits"][0]["title"] == "Beta"
    with pytest.raises(RetrievalError):
        service.glob("**/*", sort="published", date_filters=filters, cursor=first["next_cursor"])


def test_real_stdio_discovers_and_uses_date_arguments(corpus):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from lumen_reader.mcp_server.compat import field, structured_payload
    paths, _ = corpus

    async def exercise():
        frozen = os.environ.get("LUMEN_TEST_MCP_EXE")
        params = StdioServerParameters(command=frozen or sys.executable,
            args=["serve", "--stdio"] if frozen else ["-m", "lumen_reader.mcp_server", "serve", "--stdio"],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "LUMEN_INDEX_PATH": str(paths.index_file), "LUMEN_DATA_DIR": str(paths.data_dir)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.list_tools()
                for tool in listing.tools:
                    if tool.name in {"lumen_glob", "lumen_grep", "lumen_search"}:
                        assert "date_filters" in field(tool, "input_schema")["properties"]
                for tool, args in (("lumen_glob", {"pattern": "**/*", "sort": "published"}),
                                   ("lumen_search", {"query": "chronology", "strategy": "lexical"}),
                                   ("lumen_grep", {"query": "chronology"})):
                    result = await session.call_tool(tool, {**args, "date_filters": {"published_from": "2025"}})
                    assert not field(result, "is_error")
                    assert structured_payload(result)["hits"][0].get("book", structured_payload(result)["hits"][0])["title"] == "Gamma"
                for invalid_date in ("nonsense", "2024-00", "2024-01-00"):
                    result = await session.call_tool("lumen_glob", {"pattern": "*", "date_filters": {"published_from": invalid_date}})
                    assert field(result, "is_error")
                    assert structured_payload(result)["error"]["code"] == "INVALID_ARGUMENT"
                explained = await session.call_tool("lumen_explain_query", {
                    "operation": "search", "query": "chronology", "date_filters": {"published_from": "1984"}})
                assert not field(explained, "is_error")
                assert structured_payload(explained)["plan"]["date_filters"] == {"published_from": "1984"}
    anyio.run(exercise)


def test_visible_shelf_dates_click_sort_filter_and_open(corpus):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from lumen_reader.shelf import LibraryShelf
    paths, root = corpus
    app = QApplication.instance() or QApplication([])
    assert app.platformName() not in {"offscreen", "minimal"}, "This test requires a visible desktop."
    with LibraryIndex(paths.index_file) as index:
        shelf = LibraryShelf(index, str(root))
        shelf.setWindowTitle("Lumen • VISIBLE date columns regression test")
        shelf.resize(1440, 880)
        shelf.show()
        shelf.raise_()
        try:
            QTest.qWait(800)
            assert shelf.isVisible() and shelf.windowHandle().isExposed()
            for button, _, _ in shelf.date_headers:
                assert button.isVisible()
            QTest.mouseClick(shelf.date_headers[0][0], Qt.MouseButton.LeftButton)
            assert shelf.model._rows[0].title == "Gamma"
            QTest.mouseClick(shelf.date_headers[0][0], Qt.MouseButton.LeftButton)
            assert shelf.model._rows[0].title == "Alpha"
            shelf.date_from.setFocus()
            QTest.keyClicks(shelf.date_from, "2024")
            QTest.mouseClick(shelf.date_apply, Qt.MouseButton.LeftButton)
            assert shelf.model.total == 2
            QTest.qWait(700)
            QTest.mouseClick(shelf.date_clear, Qt.MouseButton.LeftButton)
            assert shelf.model.total == 4
            received = []
            shelf.book_requested.connect(received.append)
            first = shelf.model.index(0, 0)
            shelf.view.setCurrentIndex(first)
            QTest.keyClick(shelf.view, Qt.Key.Key_Return)
            assert received and Path(received[-1]).is_file()
            for width in (800, 1440):
                shelf.resize(width, 880)
                QTest.qWait(600)
                assert shelf.delegate.stacked_dates == (shelf.view.viewport().width() - 6 < 850)
            output = Path(__file__).parents[1] / ".artifacts" / "book-dates"
            output.mkdir(parents=True, exist_ok=True)
            shelf.grab().save(str(output / "visible-shelf.png"))
        finally:
            shelf.close()
            shelf.deleteLater()
            app.processEvents()


def test_visible_reader_themes_and_narrow_date_layout(corpus, tmp_path):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from lumen_reader.ui import ReaderWindow
    from lumen_reader.storage import ReaderStore
    from lumen_reader.marks import MarksStore
    paths, root = corpus
    app = QApplication.instance() or QApplication([])
    assert app.platformName() not in {"offscreen", "minimal"}
    output = Path(__file__).parents[1] / ".artifacts" / "book-dates"
    output.mkdir(parents=True, exist_ok=True)
    with LibraryIndex(paths.index_file) as index:
        window = ReaderWindow(ReaderStore(tmp_path / "reader.json"),
                              marks_store=MarksStore(tmp_path / "marks.json"),
                              library_root=root, library_index=index)
        window.setWindowTitle("Lumen — visible date feature • isolated test library")
        window.resize(1460, 960)
        window.show()
        try:
            QTest.qWait(1000)
            assert window.isVisible() and window.windowHandle().isExposed()
            for theme in ("dark", "light", "sepia"):
                window.theme_combo.setCurrentText({"dark": "Night", "light": "Paper", "sepia": "Sepia"}[theme])
                QTest.qWait(1000)
                assert window.welcome.model.rowCount() == 4
                assert all(button.isVisible() for button, _, _ in window.welcome.date_headers)
                window.grab().save(str(output / f"reader-{theme}.png"))
            window.resize(940, 900)
            QTest.qWait(900)
            window.grab().save(str(output / "reader-narrow.png"))
            assert window.welcome.delegate.stacked_dates, (window.width(), window.welcome.view.viewport().width())
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()
