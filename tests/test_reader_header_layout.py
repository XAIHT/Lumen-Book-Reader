"""Regression coverage for the responsive reader header."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import pytest
from PySide6.QtWidgets import QApplication

from lumen_reader import ui as ui_module
from lumen_reader.library_index import LibraryIndex
from lumen_reader.marks import MarksStore
from lumen_reader.storage import ReaderStore
from lumen_reader.ui import ReaderWindow


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def reader_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _application()
    monkeypatch.setattr(ui_module, "lookup_offline_wordnet_entries", lambda *_args: [])
    index = LibraryIndex(tmp_path / "library.db")
    store = ReaderStore(tmp_path / "reader.json")
    store.data["theme"] = "sepia"
    window = ReaderWindow(
        store,
        marks_store=MarksStore(tmp_path / "marks.json"),
        library_root=tmp_path,
        library_index=index,
    )
    window.main_stack.setCurrentIndex(1)
    window.library_button.show()
    window.reader_search_cluster.show()
    window.speed_reader_button.show()
    window.chapter_heading.setText(
        "1.1 Contents of Modern Radio Frequency Technologies and Their Applications"
    )
    window.show()
    app.processEvents()
    yield window
    window.close()
    window.deleteLater()
    app.processEvents()
    index.close()


def _visible_header_widgets(window: ReaderWindow):
    widgets = []
    for index in range(window.header_layout.count()):
        widget = window.header_layout.itemAt(index).widget()
        if widget is not None and not widget.isHidden():
            widgets.append(widget)
    return widgets


@pytest.mark.parametrize("width", [940, 1100, 1420, 1640])
def test_reader_header_controls_never_overlap(
    reader_window: ReaderWindow, width: int
) -> None:
    reader_window.resize(width, 700)
    reader_window._update_header_responsiveness()
    QApplication.processEvents()

    widgets = _visible_header_widgets(reader_window)
    for left, right in zip(widgets, widgets[1:]):
        assert left.geometry().right() < right.geometry().left(), (
            left.objectName(),
            left.geometry(),
            right.objectName(),
            right.geometry(),
        )
    assert widgets[-1].geometry().right() <= reader_window.header.contentsRect().right()
    assert (
        reader_window.reader_search_cluster.geometry().right()
        < reader_window.smaller_button.geometry().left()
    )


def test_reader_header_restores_optional_actions_when_space_returns(
    reader_window: ReaderWindow,
) -> None:
    reader_window.resize(940, 700)
    reader_window._update_header_responsiveness()
    QApplication.processEvents()
    assert any(widget.isHidden() for widget in reader_window._optional_header_widgets)

    reader_window.resize(2400, 700)
    reader_window._update_header_responsiveness()
    QApplication.processEvents()
    assert all(not widget.isHidden() for widget in reader_window._optional_header_widgets)
