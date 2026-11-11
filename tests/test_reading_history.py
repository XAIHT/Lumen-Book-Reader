"""Reading-session durability and real native shelf/reader interactions."""

import json
import os
import time
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from lumen_reader.library_index import LibraryIndex
from lumen_reader.marks import MarksStore
from lumen_reader.reading_history import HISTORY_FILENAME, ReadingHistoryStore, path_key
from lumen_reader.shelf import DATE_STACK_THRESHOLD, LibraryShelf
from lumen_reader.storage import ReaderStore
from test_library_index import make_epub, make_pdf


def session(path, closed, opened=None):
    return {"id": uuid4().hex, "path": str(path.resolve()), "title": path.stem,
            "author": "Test Author", "opened_ns": opened or closed - 1_000_000_000,
            "closed_ns": closed}


def test_history_is_lazy_merges_windows_and_retains_every_session(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    first, second = ReadingHistoryStore(path), ReadingHistoryStore(path)
    assert first.last_reads() == {} and not path.exists()
    book = tmp_path / "book.pdf"
    older = session(book, 1_700_000_000_000_000_000)
    newer = session(book, older["closed_ns"] + 3_000_000_000)
    first.record(newer)
    second.record(older)
    first.record(newer)  # retried close must be idempotent
    reopened = ReadingHistoryStore(path)
    assert reopened.last_reads() == {path_key(book): newer["closed_ns"]}
    assert len(reopened.read()["sessions"]) == 2


@pytest.mark.parametrize("original", ['{broken', '{"version":99,"sessions":[]}', '[]'])
def test_damaged_or_future_history_is_never_overwritten(tmp_path, original):
    path = tmp_path / HISTORY_FILENAME
    path.write_text(original, encoding="utf-8")
    with pytest.raises(OSError):
        ReadingHistoryStore(path).record(session(tmp_path / "book.pdf", time.time_ns()))
    assert path.read_text(encoding="utf-8") == original


def test_failed_atomic_save_preserves_existing_history(tmp_path, monkeypatch):
    store = ReadingHistoryStore(tmp_path / HISTORY_FILENAME)
    store.record(session(tmp_path / "a.pdf", time.time_ns()))
    original = store.path.read_bytes()
    def denied(*args, **kwargs):
        raise PermissionError("read-only")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        store.record(session(tmp_path / "b.pdf", time.time_ns()))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob("*.pending"))


def test_two_processes_merge_completed_sessions(tmp_path):
    script = """
import sys
from pathlib import Path
from lumen_reader.reading_history import ReadingHistoryStore
store = ReadingHistoryStore(sys.argv[1])
for n in range(8):
    store.record({'id': sys.argv[2] + str(n), 'path': str(Path(sys.argv[1]).with_suffix('.pdf')),
                  'opened_ns': 1700000000000000000 + n, 'closed_ns': 1700000001000000000 + n})
"""
    path = tmp_path / HISTORY_FILENAME
    children = [subprocess.Popen([sys.executable, "-c", script, str(path), prefix])
                for prefix in ("first-", "second-")]
    try:
        assert [child.wait(timeout=20) for child in children] == [0, 0]
        assert len(ReadingHistoryStore(path).read()["sessions"]) == 16
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def test_history_refreshes_after_another_window_writes_and_skips_invalid_entries(tmp_path):
    history = ReadingHistoryStore(tmp_path / HISTORY_FILENAME)
    first = session(tmp_path / "first.pdf", time.time_ns())
    history.record(first)
    assert history.last_reads() == history.last_reads() == {path_key(first["path"]): first["closed_ns"]}
    second = session(tmp_path / "second.pdf", time.time_ns())
    ReadingHistoryStore(history.path).record(second)
    assert len(history.last_reads()) == 2
    data = history.read()
    data["sessions"].extend([None, {}, {"path": "relative.pdf", "closed_ns": 123},
                             {"path": "C:\\bad\u0000name.pdf", "closed_ns": 123},
                             {"path": first["path"], "closed_ns": True}])
    history.path.write_text(json.dumps(data), encoding="utf-8")
    assert len(history.last_reads()) == 2


def test_failed_close_write_retries_original_timestamp(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lumen_reader.ui import ReaderWindow
    history = ReadingHistoryStore(tmp_path / HISTORY_FILENAME)
    target = SimpleNamespace(_reading_session=session(tmp_path / "book.pdf", time.time_ns()),
                             _pending_reading_sessions=[], history_store=history)
    target._reading_session.pop("closed_ns")
    original = history.record
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    def deny_save(_session):
        raise PermissionError("read-only")
    monkeypatch.setattr(history, "record", deny_save)
    ignored = []
    target._finish_reading_session = lambda: ReaderWindow._finish_reading_session(target)
    ReaderWindow.closeEvent(target, SimpleNamespace(ignore=lambda: ignored.append(True)))
    closed = target._pending_reading_sessions[0]["closed_ns"]
    assert warnings and ignored and not history.path.exists()
    monkeypatch.setattr(history, "record", original)
    ReaderWindow._finish_reading_session(target)
    assert not target._pending_reading_sessions
    assert history.read()["sessions"][0]["closed_ns"] == closed


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "books"
    books = [root / "A.pdf", root / "B.epub", root / "C.pdf"]
    make_pdf(books[0], "Alpha sample", "sample reading text")
    make_epub(books[1], "Beta sample", "Author", "sample reading text")
    make_pdf(books[2], "Gamma sample", "sample reading text")
    history = ReadingHistoryStore(tmp_path / "state" / HISTORY_FILENAME)
    index = LibraryIndex(tmp_path / "index.db")
    index.scan(root, workers=1)
    yield root, books, history, index
    index.close()


@pytest.mark.parametrize("mode,query", [("meta", ""), ("meta", "sample"),
                                         ("content", "sample"), ("all", "sample")])
def test_sort_precedes_pagination_and_history_cannot_create_books(library, tmp_path, mode, query):
    root, books, history, index = library
    history.record(session(books[0], 1_700_000_000_000_000_000))
    history.record(session(books[1], 1_700_000_002_000_000_000))
    history.record(session(root / "missing.pdf", 1_800_000_000_000_000_000))
    history.record(session(tmp_path / "other-library" / "outside.pdf", 1_900_000_000_000_000_000))
    index.set_reading_history(history.last_reads())
    def names(sort):
        return [Path(index.search(root, query, mode=mode, sort=sort, limit=1, offset=i)[0].path).name
                for i in range(3)]
    assert names("last_read") == ["B.epub", "A.pdf", "C.pdf"]
    assert names("last_read_asc") == ["A.pdf", "B.epub", "C.pdf"]
    assert index.count_matching(root, query, mode=mode) == 3
    index.clear_root(root)
    index.scan(root, workers=1)
    assert names("last_read") == ["B.epub", "A.pdf", "C.pdf"]
    assert len(history.read()["sessions"]) == 4


def test_reinstall_preserves_history_but_clears_settings_and_index(tmp_path):
    from install import LumenInstaller
    from uninstall import LumenUninstaller
    appdata = tmp_path / "Lumen Reader"
    data = appdata / "Lumen"
    store = ReadingHistoryStore(data / HISTORY_FILENAME)
    store.record(session(tmp_path / "read.pdf", time.time_ns()))
    before = store.path.read_bytes()
    (data / "reader-state.json").write_text('{"theme":"dark"}')
    (data / "library-index.db").write_bytes(b"disposable")
    (data / "cache").mkdir()
    (data / "cache" / "unused").write_bytes(b"cache")
    wizard = LumenUninstaller.__new__(LumenUninstaller)
    wizard._kept = []
    wizard._appdata_path = lambda: str(appdata)
    wizard.install_path = type("Value", (), {"get": lambda self: str(tmp_path / "install")})()
    wizard._find_marks_files = lambda: []
    payload, _ = wizard._collect_reading_data()
    assert payload["reading_history"] == store.read()["sessions"]
    assert wizard._keep_reading_history(str(appdata))
    assert store.path.read_bytes() == before
    assert sorted(path.name for path in data.iterdir()) == [HISTORY_FILENAME]
    assert LumenInstaller._is_preserved_member(HISTORY_FILENAME, str(data), {HISTORY_FILENAME})
    assert ReadingHistoryStore(data / HISTORY_FILENAME).last_reads()


def test_fallback_shows_only_current_files_and_sorts_history(library, tmp_path):
    app = QApplication.instance() or QApplication([])
    root, books, history, _index = library
    outside = tmp_path / "other" / "outside.pdf"
    make_pdf(outside, "Other library", "not in this catalog")
    history.record(session(books[1], time.time_ns()))
    history.record(session(outside, time.time_ns()))
    with LibraryIndex(tmp_path / "empty.db") as empty:
        shelf = LibraryShelf(empty, str(root), history_store=history)
        shelf.set_books([{"path": str(path)} for path in [*books, outside, root / "gone.pdf"]])
        shelf.model.set_sort("last_read")
        assert [Path(row.path).name for row in shelf.model._rows] == ["B.epub", "A.pdf", "C.pdf"]
        shelf.close()
        shelf.deleteLater()
        app.processEvents()


def test_uninstall_never_descends_into_a_linked_history_directory(tmp_path, monkeypatch):
    from uninstall import LumenUninstaller
    linked = tmp_path / "Lumen"
    linked.mkdir()
    (linked / HISTORY_FILENAME).write_text("keep even damaged history")
    other = linked / "unrelated-data.txt"
    other.write_text("must not traverse a junction")
    wizard = LumenUninstaller.__new__(LumenUninstaller)
    wizard._kept = []
    monkeypatch.setattr(os.path, "isjunction", lambda path: Path(path) == linked, raising=False)
    assert wizard._keep_reading_history(str(tmp_path))
    assert other.read_text() == "must not traverse a junction"


@pytest.mark.parametrize("theme", ["dark", "light", "sepia"])
def test_visible_sessions_sort_never_read_and_reopen(library, tmp_path, monkeypatch, request, theme):
    from lumen_reader.ui import ReaderWindow
    app = QApplication.instance() or QApplication([])
    assert app.platformName() not in {"offscreen", "minimal"}
    root, books, history, index = library
    state = ReaderStore(history.path.with_name("reader-state.json"))
    state.data["theme"] = theme
    window = ReaderWindow(state, library_root=root, library_index=index,
                          marks_store=MarksStore(root / "marks.json"))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    window.resize(1500, 920)
    window.show()
    try:
        QTest.qWait(300)
        model = window.welcome.model
        assert all(row.last_read_ns is None for row in model._rows)
        assert "Never read" in model.data(model.index(0, 0), Qt.ItemDataRole.AccessibleTextRole)
        window.open_book(str(books[0]))
        QTest.qWait(400)
        assert history.last_reads() == {}
        window.open_book(str(root / "does-not-exist.pdf"))
        assert history.last_reads() == {}  # failed open never ends the current session
        before_close = time.time_ns()
        window.return_to_library()
        for _ in range(40):
            if window.main_stack.currentIndex() == 0:
                break
            QTest.qWait(50)
        assert window.main_stack.currentIndex() == 0
        recorded = history.last_reads()[path_key(books[0])]
        assert before_close <= recorded <= time.time_ns()
        window.return_to_library()
        assert len(history.read()["sessions"]) == 1
        window.open_book(str(books[1]))
        QTest.qWait(250)
        window.open_book(str(books[0]))  # closes the EPUB session
        assert path_key(books[1]) in history.last_reads()
        QTest.qWait(250)
        window.return_to_library()
        QTest.qWait(300)
        button = next(button for button, _, key in window.welcome.date_headers if key == "last_read")
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert [Path(row.path).name for row in model._rows] == ["A.pdf", "B.epub", "C.pdf"]
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert [Path(row.path).name for row in model._rows] == ["B.epub", "A.pdf", "C.pdf"]
        for width in (940, 1500):
            window.resize(width, 920)
            QTest.qWait(200)
            assert window.welcome.delegate.stacked_dates == (window.welcome.view.viewport().width() - 6 < DATE_STACK_THRESHOLD)
            assert all(button.isVisible() for button, _, _ in window.welcome.date_headers)
        window.open_book(str(books[1]))
        QTest.qWait(200)
        window.close()  # closing the app while reading records once
        assert len(history.read()["sessions"]) == 4
        window.close()
        assert len(history.read()["sessions"]) == 4
        with LibraryIndex(tmp_path / "fresh-install-index.db") as rebuilt:
            rebuilt.scan(root, workers=1)
            shelf = LibraryShelf(rebuilt, str(root), history_store=ReadingHistoryStore(history.path))
            shelf.apply_palette(window.theme_colors)
            shelf.resize(1400, 820)
            shelf.show()
            shelf.model.set_sort("last_read")
            QTest.qWait(350)
            assert [Path(row.path).name for row in shelf.model._rows] == ["B.epub", "A.pdf", "C.pdf"]
            assert "Never read" in shelf.model.data(shelf.model.index(2, 0), Qt.ItemDataRole.ToolTipRole)
            request.addfinalizer(shelf.close)
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
