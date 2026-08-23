"""Application bootstrap."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from . import accel
from .library_index import DEFAULT_TEXT_BUDGET, LibraryIndex, normalize_root
from .marks import MARKS_FILENAME, MarksStore
from .storage import ReaderStore
from .ui import ReaderWindow, is_supported_book, library_books
from .version import get_version

#: Where the installer records the datalake the reader was pointed at.
REGISTRY_KEY = r"Software\Lumen Reader"
REGISTRY_VALUE = "LibraryDir"


def installed_library_root() -> Path | None:
    """The library folder chosen in the installer, if this is an installed copy.

    ``CreateShortcut.ps1`` already makes that folder the shortcut's working
    directory, so ``Path.cwd()`` is usually right - but a reader who launches
    Lumen from a pinned taskbar entry, a file association, or a shell that
    overrides the working directory would otherwise land on an empty shelf.
    Reading the value back makes the datalake explicit instead of ambient.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            value, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
    except (OSError, ImportError, FileNotFoundError):
        return None
    if not value:
        return None
    candidate = Path(str(value)).expanduser()
    return candidate if candidate.is_dir() else None


def resolve_library_root(store: ReaderStore, opened_book: str | None = None) -> Path:
    """Pick the datalake: the book's own folder, the reader's choice, or the cwd.

    Opening a specific book always wins - its siblings are the shelf the reader
    is looking at.  Otherwise the folder chosen in Configuration (Ctrl+, in the
    main window) beats the installer's value, which in turn beats whatever
    directory we were started in.

    That last fallback is why this used to look broken.  ``library_root`` was
    read here from the very first version, but nothing in the application ever
    wrote it - there was no settings window at all - so the answer was always
    the installer's value or the working directory, and "Rescan library" would
    faithfully sweep a folder with no books in it and report success.  The
    Configuration window now writes the key, and the shelf shows the folder it
    is using, so a wrong answer here is visible rather than silent.
    """
    if opened_book:
        try:
            parent = Path(opened_book).expanduser().resolve().parent
            if parent.is_dir():
                return parent
        except OSError:
            pass

    chosen = str(store.data.get("library_root") or "")
    if chosen:
        candidate = Path(chosen).expanduser()
        if candidate.is_dir():
            return candidate

    installed = installed_library_root()
    if installed is not None:
        return installed

    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    # ProcessPoolExecutor re-imports this module in every worker on Windows and
    # inside a PyInstaller bundle; without this the indexer would fork GUIs.
    multiprocessing.freeze_support()

    arguments = list(sys.argv if argv is None else argv)
    QApplication.setOrganizationName("Lumen Reader")
    QApplication.setApplicationName("Lumen")
    QApplication.setApplicationVersion(get_version())
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication(arguments)
    app.setStyle("Fusion")
    # The non-variable face is more reliable under Qt's offscreen renderer and
    # older Windows font backends while preserving the native Windows look.
    app.setFont(QFont("Segoe UI", 10))
    icon_path = Path(__file__).resolve().parent / "assets" / "lumen.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Ask what this machine has while the window is still being built, so the
    # answer is ready before anything needs it and no reader ever waits on a
    # GPU probe - least of all on a machine that has no GPU to probe.
    accel.start_background_probe()

    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    store = ReaderStore(data_dir / "reader-state.json")

    opened_book = arguments[1] if len(arguments) > 1 and is_supported_book(arguments[1]) else None
    library_root = resolve_library_root(store, opened_book)

    marks_store = MarksStore(library_root / MARKS_FILENAME)
    store.relink_missing_books(library_root)

    text_budget = int(store.data.get("text_budget", DEFAULT_TEXT_BUDGET))
    library_index = LibraryIndex(data_dir / "library-index.db", text_budget=text_budget)

    # Only used until the index exists: the recents keep the shelf from being
    # blank during the very first scan.  Capped, because this list is stat-ed
    # per entry on the UI thread and a datalake can hold tens of thousands.
    local_books = library_books(library_root)[:60]

    window = ReaderWindow(
        store,
        local_books,
        marks_store,
        library_root=normalize_root(library_root),
        library_index=library_index,
    )
    if icon_path.is_file():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    window.start_library_scan_if_needed()

    if opened_book:
        window.open_book(opened_book)
    return app.exec()
