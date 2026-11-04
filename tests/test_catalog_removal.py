"""Catalog-only removal: source safety, sweep persistence, reader checks and real Qt actions."""
import os
import time
import zipfile
from concurrent.futures import CancelledError
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from lumen_reader.catalog_check import check_catalog
from lumen_reader.library_index import LibraryIndex
from lumen_reader.passage_builder import PassageBuilder
from lumen_reader.passage_index import corpus_revision
from lumen_reader.shelf import LibraryShelf
from test_library_index import make_epub


def identity(row):
    return row.catalog_id, row.path, row.size, row.mtime_ns


def test_removal_is_root_scoped_atomic_and_cleans_search_data(tmp_path):
    root = tmp_path / "collection"
    book = make_epub(root / "a.epub", "Primer", "Alice", "rareword " * 100)
    other = make_epub(tmp_path / "other" / "b.epub", "Other", "Bob", "ordinary")
    original = book.read_bytes()
    notes = root / "lumen-reading-marks.json"
    notes.write_bytes(b"user fixture")
    db = tmp_path / "catalog.db"
    with LibraryIndex(db) as index:
        index.scan(root, workers=1)
        index.scan(other.parent, workers=1)
        row = index.search(root)[0]
        assert PassageBuilder(db).build(book_ids=[row.catalog_id]).built == 1
        revision = corpus_revision(index.connection)
        assert index.remove_catalog_entries(other.parent, [identity(row)]) == 0
        assert index.remove_catalog_entries(root, [identity(row)]) == 1
        assert corpus_revision(index.connection) > revision
        assert not index.search(root, "rareword", mode="content")
        assert index.counts(other.parent).total == 1
        for table in ("books_fts", "content_fts", "fts_rowid", "rag_documents", "rag_passages"):
            assert not index.connection.execute(f"SELECT 1 FROM {table} WHERE book_id=?", (row.catalog_id,)).fetchone()
        assert not index.connection.execute("SELECT 1 FROM rag_passages_fts").fetchone()
    with LibraryIndex(db) as index:
        assert index.scan(root, workers=1).total == 0
        assert book.read_bytes() == original and notes.read_bytes() == b"user fixture"
        index.reset_exclusions(root)
        assert index.scan(root, workers=1).total == 1


def test_rollback_and_replacement(tmp_path, monkeypatch):
    root = tmp_path / "books"
    book = make_epub(root / "a.epub")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        row = index.search(root)[0]
        with monkeypatch.context() as patch:
            def interrupt(*_):
                raise CancelledError()
            patch.setattr(index, "drop_fts_rows", interrupt)
            with pytest.raises(CancelledError):
                index.remove_catalog_entries(root, [identity(row)])
        assert index.counts(root).total == 1 and not index.catalog_exclusions(root)
        assert index.remove_catalog_entries(root, [identity(row)]) == 1
        make_epub(book, "Replacement", "Author", "different content")
        assert index.scan(root, workers=1).total == 1
        assert index.search(root)[0].title == "Replacement"


def test_removal_from_another_reader_during_extraction_is_not_republished(tmp_path, monkeypatch):
    from lumen_reader import turbo_scan
    root, db = tmp_path / "books", tmp_path / "catalog.db"
    make_epub(root / "queued.epub")
    with LibraryIndex(db) as index:
        index.scan(root, workers=1, with_text=False)
        entry = identity(index.search(root)[0])
        extract = turbo_scan.extract_book
        removed = []
        def remove_while_queued(job):
            record = extract(job)
            with LibraryIndex(db) as second:
                removed.append(second.remove_catalog_entries(root, [entry]))
            return record
        monkeypatch.setattr(turbo_scan, "extract_book", remove_while_queued)
        assert index.scan(root, workers=1, with_text=True).total == 0
        assert removed == [1]
        assert index.last_scan(root)["status"] == "done"


def test_bulk_diagnosis_does_not_confuse_index_or_access_failures_with_damage(tmp_path):
    root = tmp_path / "books"
    good = make_epub(root / "readable.epub")
    fixed = root / "bad.epub"
    fixed.write_bytes(b"not a ZIP")
    gone = make_epub(root / "offline.epub")
    (root / "broken.pdf").write_bytes(b"not a PDF")
    (root / "empty.pdf").write_bytes(b"")
    with zipfile.ZipFile(root / "incomplete.epub", "w") as archive:
        archive.writestr("META-INF/container.xml", "<broken")
    crc = make_epub(root / "late-crc.epub", "Catalogued normally")
    with zipfile.ZipFile(crc, "a") as archive:
        archive.writestr("OEBPS/picture.bin", b"UNIQUE-ASSET-MARK")
    crc.write_bytes(crc.read_bytes().replace(b"UNIQUE-ASSET-MARK", b"BROKEN-ASSET-MARK"))
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(root / "image-only.pdf")
        pdf.save(root / "password.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw="owner", user_pw="password")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        gone.unlink()  # synthetic file, deliberately simulate disconnected storage
        with index.connection:
            index.connection.execute("UPDATE books SET ok=0,error='transient failure' WHERE name=?", (good.name,))
        report = check_catalog(index.path, root, lambda: False, lambda *_: None)
        assert {Path(i[1]).name for i, _ in report.damaged} == {
            "bad.epub", "broken.pdf", "empty.pdf", "incomplete.epub", "late-crc.epub"}
        assert report.skipped == 2  # missing + password
        make_epub(fixed, "Repaired before confirmation")
        assert index.remove_catalog_entries(root, [i for i, _ in report.damaged], verify_files=True) == 4
        assert index.connection.execute("SELECT 1 FROM books WHERE name='bad.epub'").fetchone()
        with pytest.raises(CancelledError):
            check_catalog(index.path, root, lambda: True, lambda *_: None)


def test_visible_shelf_right_click_and_bulk_preview(tmp_path):
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "books"
    a = make_epub(root / "a.epub", "Alpha")
    b = make_epub(root / "b.epub", "Beta")
    bad = root / "corrupt.epub"
    bad.write_bytes(b"bad zip")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        shelf = LibraryShelf(index, str(root))
        shelf.resize(1200, 780)
        shelf.show()
        assert QTest.qWaitForWindowExposed(shelf)
        try:
            view = shelf.view
            view.setCurrentIndex(shelf.model.index(0, 0))
            target = shelf.model.index(1, 0)
            assert shelf.model.row_at(target).title == "Beta"
            point = view.visualRect(target).center()
            def right_click():
                QApplication.sendEvent(view.viewport(), QContextMenuEvent(
                    QContextMenuEvent.Reason.Mouse, point, view.viewport().mapToGlobal(point)))

            def exercise(trigger, confirm, expected_title):
                seen, errors = [], []
                photographed = set()
                def photo(name):
                    if confirm and name not in photographed and os.environ.get("LUMEN_SHOTER_LAUNCHER"):
                        from tools.validate_mcp_install import capture
                        photographed.add(name)
                        capture(name)
                deadline = time.monotonic() + 20
                active = [True]
                def drive():
                    if not active[0]:
                        return
                    w = QApplication.activePopupWidget() or QApplication.activeModalWidget()
                    if time.monotonic() > deadline:
                        errors.append("dialog timed out")
                        if w:
                            w.close()
                        return
                    QTimer.singleShot(60, drive)
                    if isinstance(w, QMenu):
                        if expected_title == "Remove from catalog?":
                            photo("shelf-right-click-menu")
                        QTest.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.actionGeometry(w.actions()[0]).center())
                    elif isinstance(w, QMessageBox):
                        seen.append(w.windowTitle())
                        if w.windowTitle() == expected_title:
                            if expected_title == "Review damaged books":
                                if str(bad) not in w.detailedText():
                                    errors.append("preview did not identify the damaged file")
                                photo("shelf-review-damaged")
                            button = next(x for x in w.buttons() if w.buttonRole(x) ==
                                          (QMessageBox.ButtonRole.DestructiveRole if confirm else QMessageBox.ButtonRole.RejectRole))
                            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                        else:
                            w.accept()
                QTimer.singleShot(60, drive)
                try:
                    trigger()
                finally:
                    active[0] = False
                assert not errors and expected_title in seen, (errors, seen)

            exercise(right_click, False, "Remove from catalog?")
            assert index.counts(root).total == 3
            exercise(right_click, True, "Remove from catalog?")
            assert {r.title for r in index.search(root)} == {"Alpha", "corrupt"}
            shelf.search.setText("Alpha")
            shelf._run_search()
            assert shelf.model.rowCount() == 1
            exercise(shelf.catalog_button.showMenu, False, "Review damaged books")
            assert index.counts(root).total == 2
            exercise(shelf.catalog_button.showMenu, True, "Review damaged books")
            assert index.counts(root).total == 1 and shelf._stat_total.text() == "1"
            assert shelf.search.text() == "Alpha" and a.is_file() and b.is_file() and bad.is_file()
            # Removing the last row must not resurrect it through recents.
            shelf.search.clear()
            shelf.set_books([{"path": str(a)}, {"path": str(b)}])
            index.remove_catalog_entries(root, [identity(index.search(root)[0])])
            shelf.refresh_counts()
            assert shelf.model.rowCount() == 0
        finally:
            shelf.close()


def test_context_menu_uses_the_clicked_row_after_scrolling(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "books"
    for i in range(35):
        make_epub(root / f"{i:02d}.epub", f"Book {i:02d}")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        shelf = LibraryShelf(index, str(root))
        shelf.resize(1200, 720)
        shelf.show()
        try:
            assert QTest.qWaitForWindowExposed(shelf)
            view = shelf.view
            view.setCurrentIndex(shelf.model.index(0, 0))
            item = shelf.model.index(32, 0)
            view.scrollTo(item)
            app.processEvents()
            assert view.verticalScrollBar().value() > 0
            point = view.visualRect(item).center()
            assert view.indexAt(point).row() == 32
            confirmed = []
            monkeypatch.setattr(shelf, "_confirm", lambda _title, text: confirmed.append(text) or False)
            def choose():
                menu = QApplication.activePopupWidget()
                if isinstance(menu, QMenu):
                    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(menu.actions()[0]).center())
            QTimer.singleShot(100, choose)
            QApplication.sendEvent(view.viewport(), QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, point, view.viewport().mapToGlobal(point)))
            assert len(confirmed) == 1 and str(root / "32.epub") in confirmed[0]
            assert index.counts(root).total == 35
        finally:
            shelf.close()
