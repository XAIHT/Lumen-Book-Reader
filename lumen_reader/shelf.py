# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The library shelf: a virtualized view over an arbitrarily large datalake.

The old shelf held one ``QListWidgetItem`` per book and rebuilt every one of
them on each keystroke.  At ten thousand books that is ten thousand widgets per
character typed, on the UI thread.

This shelf never materialises more than one page.  ``LibraryModel`` asks
``LibraryIndex`` for four hundred rows at a time and fetches the next page only
when the reader actually scrolls to it, so the cost of painting the shelf is set
by the size of the *viewport*, not the size of the library.  Searching is an
FTS5 lookup rather than a Python scan, and it runs debounced so a fast typist
issues one query instead of one per key.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QProgressBar,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .library_index import BookRow, LibraryCounts, LibraryIndex, ScanProgress

PAGE_SIZE = 400
SEARCH_DEBOUNCE_MS = 140
ROW_HEIGHT = 74
ROW_HEIGHT_SNIPPET = 96


#: 1 typographic point is 4/3 of a pixel at Qt's 96-DPI logical resolution.
PIXELS_PER_POINT = 4 / 3


def scaled_font(base: QFont, delta_points: float, floor_points: float = 7.0) -> QFont:
    """A copy of *base* resized by *delta_points*, however *base* is measured.

    Lumen's theme sizes text in *pixels* (``QWidget { font-size: 13px }``), and
    a font sized that way reports ``pointSizeF() == -1``.  Adding a point to
    that asked Qt for a point size of zero: Qt refused it, so the shelf titles
    silently stayed at body size - and every repaint of every row printed
    ``QFont::setPointSizeF: Point size <= 0``.  One screenful of a real library
    wrote 1,458 of those to the console.  Scaling in whichever unit the widget
    actually uses fixes both the warning and the typography.
    """
    font = QFont(base)
    points = base.pointSizeF()
    if points > 0:
        font.setPointSizeF(max(floor_points, points + delta_points))
        return font
    pixels = base.pixelSize()
    if pixels <= 0:
        pixels = 13                       # neither unit set: assume the theme's body size
    font.setPixelSize(max(
        round(floor_points * PIXELS_PER_POINT),
        round(pixels + delta_points * PIXELS_PER_POINT),
    ))
    return font


def blend(first: str, second: str, weight: float) -> QColor:
    """A colour ``weight`` of the way from ``first`` towards ``second``.

    Lumen ships three palettes, so a hard-coded shade that reads well on the
    dark theme goes muddy on sepia.  Derived colours travel with the palette.
    """
    start, end = QColor(first), QColor(second)
    return QColor(
        round(start.red() + (end.red() - start.red()) * weight),
        round(start.green() + (end.green() - start.green()) * weight),
        round(start.blue() + (end.blue() - start.blue()) * weight),
    )


def row_border_color(colors: dict[str, str]) -> QColor:
    """The outline around one book card.

    The rows were drawn in the theme's ``line`` colour, which is meant for
    hairline dividers on the window background - on the slightly lighter row
    background it came out at roughly 1.2:1 against its own fill, so the cards
    had no visible edge at all and the shelf read as one undifferentiated
    column of text.  Lifting the same colour towards the foreground keeps it a
    quiet outline while actually drawing the card.

    0.36 is not arbitrary: it is the weight that puts all three of Lumen's
    palettes at roughly 3:1 against the row fill, which is the contrast a
    non-text element needs to be seen.
    """
    return blend(colors["line"], colors["fg"], 0.36)


def folder_color(colors: dict[str, str]) -> QColor:
    """The shade of the "in <sub-folder>" line under a book.

    It used to be painted in the same muted grey as the author and file facts
    directly above it, one point smaller and pinned to the bottom edge of the
    row - on a real shelf it was effectively invisible.  It carries the one
    thing that tells two identically-named books apart, so it is lifted most of
    the way to the foreground while staying quieter than the title.
    """
    return blend(colors["muted"], colors["fg"], 0.55)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:,.0f} {unit}" if unit in {"B", "KB"} else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} PB"


# ───────────────────────────────── model ──────────────────────────────────


class LibraryModel(QAbstractListModel):
    """A paged window onto the indexed library."""

    PathRole = Qt.ItemDataRole.UserRole + 1
    RowRole = Qt.ItemDataRole.UserRole + 2

    countsChanged = Signal(int, int)  # shown, total

    def __init__(self, index: LibraryIndex, root: str, parent: QWidget | None = None):
        super().__init__(parent)
        # NEVER name this attribute `index`: QAbstractListModel.index() is part
        # of the model API and Qt calls it from C++ on every paint.  Shadowing
        # it with a LibraryIndex kills the view with 'object is not callable'.
        self.library = index
        self.root = root
        self._rows: list[BookRow] = []
        self._total = 0
        self._query = ""
        self._mode = "meta"
        self._extensions: list[str] = []
        self._fallback: list[BookRow] = []

    # ── query state ────────────────────────────────────────────────────────

    def set_root(self, root: str) -> None:
        self.root = root
        self.refresh()

    def set_fallback(self, rows: list[BookRow]) -> None:
        """Rows to show before the index has ever been built."""
        self._fallback = rows
        if not self._total:
            self.refresh()

    def set_query(self, query: str, mode: str | None = None) -> None:
        self._query = query
        if mode is not None:
            self._mode = mode
        self.refresh()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.refresh()

    def set_extensions(self, extensions: list[str]) -> None:
        self._extensions = extensions
        self.refresh()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def total(self) -> int:
        return self._total

    def refresh(self) -> None:
        try:
            self._total = self.library.count_matching(
                self.root, self._query, mode=self._mode, extensions=self._extensions
            )
            page = self.library.search(
                self.root, self._query, mode=self._mode,
                limit=PAGE_SIZE, offset=0, extensions=self._extensions,
            )
        except Exception:
            self._total, page = 0, []

        if not self._total and self._fallback and not self._query:
            page = list(self._fallback)
            self._total = len(page)

        self.beginResetModel()
        self._rows = page
        self.endResetModel()
        self.countsChanged.emit(len(self._rows), self._total)

    # ── Qt model plumbing ──────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        return not parent.isValid() and len(self._rows) < self._total

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:
        if parent.isValid():
            return
        try:
            more = self.library.search(
                self.root, self._query, mode=self._mode,
                limit=PAGE_SIZE, offset=len(self._rows), extensions=self._extensions,
            )
        except Exception:
            return
        if not more:
            self._total = len(self._rows)
            return
        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(more) - 1)
        self._rows.extend(more)
        self.endInsertRows()
        self.countsChanged.emit(len(self._rows), self._total)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.title
        if role == self.PathRole:
            return row.path
        if role == self.RowRole:
            return row
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [row.title, row.author, row.path]
            if row.subjects:
                lines.append(f"Topics: {row.subjects}")
            if row.pages:
                lines.append(f"{row.pages:,} {'pages' if row.ext == '.pdf' else 'sections'}")
            return "\n".join(lines)
        return None

    def row_at(self, index: QModelIndex) -> BookRow | None:
        if index.isValid() and 0 <= index.row() < len(self._rows):
            return self._rows[index.row()]
        return None


# ─────────────────────────────── delegate ─────────────────────────────────


class BookDelegate(QStyledItemDelegate):
    """Paints one book row: kind badge, title, author, and any topic snippet."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.colors = {
            "fg": "#ecedea", "muted": "#929bad", "line": "#252d3b",
            "accent": "#63d1ad", "accent2": "#112d28", "panel2": "#171d29",
            "hover": "#202837",
        }
        self.root = ""

    def set_palette(self, colors: dict[str, str]) -> None:
        self.colors = dict(colors)

    def set_root(self, root: str) -> None:
        self.root = os.path.normcase(str(root))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        row = index.data(LibraryModel.RowRole)
        tall = bool(row and row.snippet)
        return QSize(0, ROW_HEIGHT_SNIPPET if tall else ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row: BookRow | None = index.data(LibraryModel.RowRole)
        if row is None:
            return
        colors = self.colors
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(3, 2, -3, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        background = QColor(colors["accent2"] if selected else
                            colors["hover"] if hovered else colors["panel2"])
        border = QColor(colors["accent"]) if selected else row_border_color(colors)
        painter.setBrush(background)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, 9, 9)

        # Kind badge
        badge = QRect(rect.left() + 12, rect.top() + 14, 52, 22)
        is_pdf = row.ext == ".pdf"
        badge_color = QColor("#e2725b") if is_pdf else QColor(colors["accent"])
        painter.setBrush(badge_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge, 6, 6)
        badge_font = scaled_font(option.font, -2.0)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        painter.setPen(QColor("#09130f"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, row.kind)

        text_left = badge.right() + 14
        text_width = rect.right() - text_left - 12

        # Title
        title_font = scaled_font(option.font, 1.0)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(colors["fg"]))
        title_rect = QRect(text_left, rect.top() + 8, text_width, 22)
        metrics = QFontMetrics(title_font)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(row.title, Qt.TextElideMode.ElideRight, text_width),
        )

        # Author + facts
        detail_font = scaled_font(option.font, -1.0, floor_points=7.5)
        painter.setFont(detail_font)
        painter.setPen(QColor(colors["muted"]))
        facts = [row.author]
        if row.pages:
            facts.append(f"{row.pages:,} {'pages' if is_pdf else 'sections'}")
        if row.size:
            facts.append(human_bytes(row.size))
        if row.subjects:
            facts.append(row.subjects)
        detail_metrics = QFontMetrics(detail_font)
        detail_rect = QRect(text_left, rect.top() + 30, text_width, 17)
        painter.drawText(
            detail_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            detail_metrics.elidedText("  ·  ".join(facts), Qt.TextElideMode.ElideRight, text_width),
        )

        # Sub-folder line.  Only drawn when the book is *not* sitting directly
        # in the library root - in a flat library it would be the same string on
        # every row, which is noise rather than information.
        parent = Path(row.path).parent
        if self.root and os.path.normcase(str(parent)) != self.root:
            try:
                relative = parent.relative_to(Path(self.root))
                folder_text = str(relative)
            except ValueError:
                folder_text = parent.name
            folder_font = QFont(detail_font)
            folder_font.setBold(True)
            painter.setFont(folder_font)
            painter.setPen(folder_color(colors))
            folder_rect = QRect(text_left, rect.top() + 48, text_width, 17)
            folder_metrics = QFontMetrics(folder_font)
            painter.drawText(
                folder_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                folder_metrics.elidedText(
                    "in  " + folder_text, Qt.TextElideMode.ElideMiddle, text_width
                ),
            )

        # Topic snippet from inside the book
        if row.snippet:
            snippet_font = QFont(detail_font)
            snippet_font.setItalic(True)
            painter.setFont(snippet_font)
            painter.setPen(QColor(colors["accent"]))
            snippet_rect = QRect(text_left, rect.top() + 66, text_width, 18)
            painter.drawText(
                snippet_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                QFontMetrics(snippet_font).elidedText(
                    row.snippet.strip(), Qt.TextElideMode.ElideRight, text_width
                ),
            )

        painter.restore()


# ────────────────────────────── the list view ─────────────────────────────


class BookListView(QListView):
    """The shelf list, which never keeps the reader's typing for itself.

    ``QListView`` answers a printable key with its own incremental jump-to-item
    search.  On a shelf that already has a search box that is exactly wrong:
    click a book, type "sitchin", and every character disappears into a hidden
    jump buffer instead of the box the reader is looking at.  Printable keys are
    handed straight back to the shelf; navigation keys still belong to the list.
    """

    typed = Signal(str)   # the character to append; "" means backspace

    _CHORDS = (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.MetaModifier
    )

    def keyboardSearch(self, search: str) -> None:  # noqa: N802 - Qt's name
        """Disabled: the search box is the only place typing goes."""
        return

    def keyPressEvent(self, event) -> None:
        if not (event.modifiers() & self._CHORDS):
            text = event.text()
            if text and text.isprintable():
                self.typed.emit(text)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Backspace:
                self.typed.emit("")
                event.accept()
                return
        super().keyPressEvent(event)


# ──────────────────────────── background indexer ──────────────────────────


class IndexWorker(QThread):
    """Runs a full parallel scan off the UI thread."""

    progressed = Signal(object)   # ScanProgress
    finished_scan = Signal(object)  # LibraryCounts

    def __init__(self, database: Path, root: str, text_budget: int, with_text: bool = True):
        super().__init__()
        self.database = database
        self.root = root
        self.text_budget = text_budget
        self.with_text = with_text
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        # A private connection: WAL lets this write while the shelf keeps reading.
        counts = LibraryCounts()
        try:
            with LibraryIndex(self.database, text_budget=self.text_budget) as index:
                counts = index.scan(
                    self.root,
                    progress=self.progressed.emit,
                    cancelled=lambda: self._cancel,
                    with_text=self.with_text,
                )
        except Exception as exception:
            self.progressed.emit(ScanProgress("error", detail=str(exception)[:300]))
        self.finished_scan.emit(counts)


# ────────────────────────────────  shelf  ─────────────────────────────────


class LibraryShelf(QWidget):
    """The middle-of-the-window list of every book in the datalake."""

    browse_requested = Signal()
    book_requested = Signal(str)
    rescan_requested = Signal()

    def __init__(self, index: LibraryIndex, root: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = index
        self.root = root
        self.setObjectName("welcomePage")

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(54, 30, 54, 26)
        root_layout.setSpacing(14)

        # ── hero ───────────────────────────────────────────────────────────
        heading = QLabel("Your library, all of it.")
        heading.setObjectName("heroHeading")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root_layout.addWidget(heading)

        # ── the count banner ───────────────────────────────────────────────
        self.counter_bar = QWidget()
        self.counter_bar.setObjectName("counterBar")
        counter_layout = QHBoxLayout(self.counter_bar)
        counter_layout.setContentsMargins(20, 14, 20, 14)
        counter_layout.setSpacing(10)
        self._stat_total = self._stat(counter_layout, "0", "BOOKS", "statBig")
        self._stat_epub = self._stat(counter_layout, "0", "EPUB", "statEpub")
        self._stat_pdf = self._stat(counter_layout, "0", "PDF", "statPdf")
        self._stat_size = self._stat(counter_layout, "—", "ON DISK", "statPlain")
        self._stat_text = self._stat(counter_layout, "0", "FULL-TEXT", "statPlain")
        root_layout.addWidget(self.counter_bar)

        # ── search row ─────────────────────────────────────────────────────
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("shelfSearch")
        self.search.setPlaceholderText(
            "Search titles, authors, filenames…   (try  ext:pdf  ·  \"exact phrase\")"
        )
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search your library")
        self.search.textChanged.connect(self._on_search_typed)
        self.search.returnPressed.connect(self._open_selected)
        search_row.addWidget(self.search, 1)

        self.mode_meta = self._toggle("Titles & authors", True)
        self.mode_content = self._toggle("Inside books", False)
        self.mode_meta.clicked.connect(lambda: self._set_mode("meta"))
        self.mode_content.clicked.connect(lambda: self._set_mode("content"))
        search_row.addWidget(self.mode_meta)
        search_row.addWidget(self.mode_content)
        root_layout.addLayout(search_row)

        # ── filter chips + status ──────────────────────────────────────────
        chip_row = QHBoxLayout()
        chip_row.setSpacing(7)
        self.chip_all = self._toggle("All", True)
        self.chip_epub = self._toggle("EPUB only", False)
        self.chip_pdf = self._toggle("PDF only", False)
        self.chip_all.clicked.connect(lambda: self._set_extensions([]))
        self.chip_epub.clicked.connect(lambda: self._set_extensions([".epub"]))
        self.chip_pdf.clicked.connect(lambda: self._set_extensions([".pdf"]))
        for chip in (self.chip_all, self.chip_epub, self.chip_pdf):
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("shelfStatus")
        chip_row.addWidget(self.status)
        root_layout.addLayout(chip_row)

        # ── the virtualized list ───────────────────────────────────────────
        self.model = LibraryModel(index, root, self)
        self.model.countsChanged.connect(self._on_counts_changed)
        self.delegate = BookDelegate(self)
        self.view = BookListView()
        self.view.setObjectName("shelfList")
        self.view.setModel(self.model)
        self.view.setItemDelegate(self.delegate)
        self.view.setUniformItemSizes(False)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.doubleClicked.connect(self._activate)
        self.view.activated.connect(self._activate)
        self.view.typed.connect(self.type_into_search)
        root_layout.addWidget(self.view, 1)

        # ── indexing footer ────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.setSpacing(9)
        self.progress = QProgressBar()
        self.progress.setObjectName("shelfProgress")
        self.progress.setTextVisible(True)
        self.progress.setMaximumHeight(18)
        self.progress.hide()
        footer.addWidget(self.progress, 1)

        self.open_button = QPushButton("Open a Book…")
        self.open_button.setObjectName("primarySmallButton")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.browse_requested)
        footer.addWidget(self.open_button)

        self.rescan_button = QPushButton("Rescan library")
        self.rescan_button.setObjectName("smallButton")
        self.rescan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rescan_button.clicked.connect(self.rescan_requested)
        footer.addWidget(self.rescan_button)
        root_layout.addLayout(footer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_search)

        self._extensions: list[str] = []
        self.delegate.set_root(root)
        self.search.setFocus()
        self.refresh_counts()

    def showEvent(self, event) -> None:
        """The reader should be able to simply start typing.

        Coming back from a book, the shelf is what has the reader's attention,
        so the search box takes the keyboard rather than making them click it
        first.  Without this the first characters of a search go nowhere.
        """
        super().showEvent(event)
        self.focus_search()

    def focus_search(self, select_all: bool = True) -> None:
        """Put the keyboard in the search box."""
        self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        if select_all:
            self.search.selectAll()

    def keyPressEvent(self, event) -> None:
        """Typing anywhere on the shelf is a search, wherever the focus sits.

        The chips and the buttons are focusable too, so setting focus once at
        show time is not enough on its own - a Tab or a stray click would send
        the next word into a button.  Any printable key that reaches the shelf
        is routed to the box instead.
        """
        if not (event.modifiers() & BookListView._CHORDS):
            text = event.text()
            if text and text.isprintable():
                self.type_into_search(text)
                event.accept()
                return
        super().keyPressEvent(event)

    def type_into_search(self, text: str) -> None:
        """Append a character the reader typed elsewhere on the shelf."""
        had_focus = self.search.hasFocus()
        self.focus_search(select_all=False)
        if not had_focus:
            self.search.deselect()
            self.search.end(False)
        if text:
            self.search.insert(text)
        else:
            self.search.backspace()

    # ── small builders ─────────────────────────────────────────────────────

    def _stat(self, layout: QHBoxLayout, value: str, caption: str, name: str) -> QLabel:
        block = QWidget()
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(6, 0, 6, 0)
        block_layout.setSpacing(0)
        number = QLabel(value)
        number.setObjectName(name)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel(caption)
        label.setObjectName("statCaption")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        block_layout.addWidget(number)
        block_layout.addWidget(label)
        layout.addWidget(block)
        return number

    @staticmethod
    def _toggle(text: str, checked: bool) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("chip")
        button.setCheckable(True)
        button.setChecked(checked)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # ── behaviour ──────────────────────────────────────────────────────────

    def set_root(self, root: str) -> None:
        self.root = root
        self.delegate.set_root(root)
        self.model.set_root(root)
        self.refresh_counts()

    def set_books(self, books: list[dict[str, str]]) -> None:
        """Compatibility path: show recents until the index has been built."""
        rows: list[BookRow] = []
        for book in books:
            path_text = str(book.get("path") or "")
            if not path_text:
                continue
            path = Path(path_text)
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError:
                continue
            if not size:
                continue
            rows.append(BookRow(
                path=str(path), name=path.name, ext=path.suffix.casefold(), size=size,
                title=str(book.get("title") or path.stem),
                author=str(book.get("author") or "Unknown author"),
            ))
        self.model.set_fallback(rows)

    def _on_search_typed(self, _text: str) -> None:
        self._debounce.start()

    def _run_search(self) -> None:
        self.model.set_query(self.search.text())

    def _set_mode(self, mode: str) -> None:
        self.mode_meta.setChecked(mode == "meta")
        self.mode_content.setChecked(mode == "content")
        self.search.setPlaceholderText(
            "Search inside every book — topics, phrases, ideas…"
            if mode == "content"
            else "Search titles, authors, filenames…   (try  ext:pdf  ·  \"exact phrase\")"
        )
        self.model.set_mode(mode)

    def _set_extensions(self, extensions: list[str]) -> None:
        self._extensions = extensions
        self.chip_all.setChecked(not extensions)
        self.chip_epub.setChecked(extensions == [".epub"])
        self.chip_pdf.setChecked(extensions == [".pdf"])
        self.model.set_extensions(extensions)

    def _activate(self, index: QModelIndex) -> None:
        row = self.model.row_at(index)
        if row is not None:
            self.book_requested.emit(row.path)

    def _open_selected(self) -> None:
        indexes = self.view.selectionModel().selectedIndexes()
        if indexes:
            self._activate(indexes[0])
        elif self.model.rowCount():
            self._activate(self.model.index(0, 0))

    def _on_counts_changed(self, shown: int, total: int) -> None:
        scope = "matching" if self.search.text().strip() else "in library"
        self.status.setText(f"showing {shown:,} of {total:,} {scope}")

    def refresh_counts(self) -> None:
        try:
            counts = self.library.counts(self.root)
        except Exception:
            counts = LibraryCounts()
        self._stat_total.setText(f"{counts.total:,}")
        self._stat_epub.setText(f"{counts.epub:,}")
        self._stat_pdf.setText(f"{counts.pdf:,}")
        self._stat_size.setText(human_bytes(counts.bytes_total) if counts.bytes_total else "—")
        self._stat_text.setText(f"{counts.with_text:,}")
        self.model.refresh()

    # ── indexing feedback ──────────────────────────────────────────────────

    def show_progress(self, update: ScanProgress) -> None:
        self.progress.show()
        self.rescan_button.setEnabled(False)
        if update.phase == "walk":
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"Scanning library — {update.detail}")
        elif update.phase == "extract":
            self.progress.setRange(0, max(1, update.total))
            self.progress.setValue(update.done)
            self.progress.setFormat(f"Indexing %v of %m books  ({update.detail})")
        elif update.phase == "error":
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self.progress.setFormat(f"Indexing problem: {update.detail}")
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self.progress.setFormat(f"Library indexed — {update.detail}")

    def finish_progress(self) -> None:
        self.rescan_button.setEnabled(True)
        self.refresh_counts()
        QTimer.singleShot(4000, self.progress.hide)

    # ── theming ────────────────────────────────────────────────────────────

    def apply_palette(self, colors: dict[str, str]) -> None:
        self.delegate.set_palette(colors)
        self.setStyleSheet(f"""
            #heroHeading {{ color: {colors['fg']}; font-size: 27px; font-weight: 800; }}
            #counterBar {{ background: {colors['panel']}; border: 1px solid {colors['line']};
                           border-radius: 14px; }}
            #statBig {{ color: {colors['accent']}; font-size: 34px; font-weight: 850; }}
            #statEpub {{ color: {colors['accent']}; font-size: 27px; font-weight: 800; }}
            #statPdf {{ color: #e2725b; font-size: 27px; font-weight: 800; }}
            #statPlain {{ color: {colors['fg']}; font-size: 27px; font-weight: 800; }}
            #statCaption {{ color: {colors['muted']}; font-size: 10px; font-weight: 750;
                            letter-spacing: 2px; }}
            #shelfStatus {{ color: {colors['muted']}; font-size: 11px; }}
            #shelfSearch {{ color: {colors['fg']}; background: {colors['panel']};
                            border: 1px solid {colors['line']}; border-radius: 10px;
                            padding: 11px 13px; font-size: 14px; }}
            #shelfSearch:focus {{ border-color: {colors['accent']}; }}
            #shelfList {{ background: {colors['panel']}; border: 1px solid {colors['line']};
                          border-radius: 13px; padding: 6px; outline: none; }}
            #chip {{ color: {colors['muted']}; background: transparent;
                     border: 1px solid {colors['line']}; border-radius: 13px;
                     padding: 5px 13px; font-size: 11px; font-weight: 700; }}
            #chip:checked {{ color: {colors['accent']}; background: {colors['accent2']};
                             border-color: {colors['accent']}; }}
            #smallButton {{ color: {colors['fg']}; background: {colors['panel2']};
                            border: 1px solid {colors['line']}; border-radius: 9px;
                            padding: 7px 13px; font-size: 11px; font-weight: 700; }}
            #smallButton:hover {{ border-color: {colors['accent']}; }}
            #primarySmallButton {{ color: #09130f; background: {colors['accent']}; border: none;
                                   border-radius: 9px; padding: 7px 15px; font-size: 11px;
                                   font-weight: 800; }}
            #shelfProgress {{ color: {colors['muted']}; background: {colors['panel2']};
                              border: 1px solid {colors['line']}; border-radius: 9px;
                              font-size: 10px; font-weight: 700; }}
            #shelfProgress::chunk {{ background: {colors['accent']}; border-radius: 8px; }}
        """)
        self.view.viewport().update()
