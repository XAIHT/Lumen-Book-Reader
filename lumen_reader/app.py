"""Application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .marks import MARKS_FILENAME, MarksStore
from .storage import ReaderStore
from .ui import ReaderWindow, is_supported_book, library_books


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    QApplication.setOrganizationName("Lumen Reader")
    QApplication.setApplicationName("Lumen")
    QApplication.setApplicationVersion("1.1.0")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication(arguments)
    app.setStyle("Fusion")
    # The non-variable face is more reliable under Qt's offscreen renderer and
    # older Windows font backends while preserving the native Windows look.
    app.setFont(QFont("Segoe UI", 10))
    icon_path = Path(__file__).resolve().parent / "assets" / "lumen.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    store = ReaderStore(data_dir / "reader-state.json")
    marks_store = MarksStore(Path.cwd() / MARKS_FILENAME)
    local_books = library_books(Path.cwd())
    window = ReaderWindow(store, local_books, marks_store)
    if icon_path.is_file():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()

    if len(arguments) > 1 and is_supported_book(arguments[1]):
        window.open_book(arguments[1])
    return app.exec()
