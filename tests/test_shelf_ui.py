from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

from lumen_reader.ui import WelcomePage


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_shelf_search_filters_titles_authors_and_filenames(tmp_path: Path) -> None:
    app = _application()
    paths = [tmp_path / name for name in ("alpha.epub", "beta.epub", "cosmos-file.epub")]
    for path in paths:
        path.touch()
    page = WelcomePage()
    page.set_books(
        [
            {"path": str(paths[0]), "title": "Alpha World", "author": "Ada Writer"},
            {"path": str(paths[1]), "title": "Beta Days", "author": "Ben Author"},
            {"path": str(paths[2]), "title": "The Universe", "author": "Carl Sagan"},
        ]
    )
    assert page.books.count() == 3

    page.shelf_search.setText("alpha ada")
    app.processEvents()
    assert page.books.count() == 1
    assert "Alpha World" in page.books.item(0).text()

    page.shelf_search.setText("cosmos-file")
    app.processEvents()
    assert page.books.count() == 1
    assert "The Universe" in page.books.item(0).text()

    page.shelf_search.setText("missing title")
    app.processEvents()
    assert page.books.count() == 1
    assert page.books.item(0).flags() == Qt.ItemFlag.NoItemFlags


def test_shelf_accepts_and_labels_pdf_documents(tmp_path: Path) -> None:
    app = _application()
    pdf = tmp_path / "field-guide.pdf"
    pdf.touch()
    page = WelcomePage()
    page.set_books([{"path": str(pdf), "title": "Field Guide", "author": ""}])
    app.processEvents()
    assert page.books.count() == 1
    assert "Field Guide" in page.books.item(0).text()
    assert "PDF document" in page.books.item(0).text()


def test_shelf_uses_per_pixel_precision_scrolling() -> None:
    _application()
    page = WelcomePage()
    assert page.books.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    assert page.books.horizontalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    assert page.books.viewport().testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
