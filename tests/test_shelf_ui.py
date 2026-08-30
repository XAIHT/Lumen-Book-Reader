"""Tests for the virtualized datalake shelf."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QModelIndex, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

from lumen_reader import shelf as shelf_module  # noqa: E402
from lumen_reader.library_index import LibraryIndex  # noqa: E402
from lumen_reader.shelf import (  # noqa: E402
    BookDelegate,
    BookListView,
    LibraryShelf,
    blend,
    folder_color,
    human_bytes,
    row_border_color,
    scaled_font,
    source_path_text,
)
from lumen_reader.turbo_scan import ScanSnapshot  # noqa: E402

from test_library_index import make_epub, make_pdf  # noqa: E402


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def shelf(tmp_path: Path):
    """A shelf over a small, fully indexed library."""
    _application()
    root = tmp_path / "datalake"
    make_epub(root / "alpha.epub", "Alpha World", "Ada Writer",
              "the alpha book discusses hydrology", subjects=["Science"])
    make_epub(root / "deep" / "beta.epub", "Beta Days", "Ben Author",
              "beta explores medieval falconry")
    make_pdf(root / "gamma.pdf", "Gamma Report", "detonation velocity")
    index = LibraryIndex(tmp_path / "i.db")
    index.scan(root, workers=1)
    widget = LibraryShelf(index, str(root))
    yield widget
    widget.deleteLater()
    index.close()


# ─────────────────────────────── the banner ───────────────────────────────


def test_counts_banner_shows_totals_and_each_type(shelf: LibraryShelf) -> None:
    assert shelf._stat_total.text() == "3"
    assert shelf._stat_epub.text() == "2"
    assert shelf._stat_pdf.text() == "1"
    assert shelf._stat_size.text() != "—"
    assert shelf._stat_text.text() == "3"


def test_status_line_reports_shown_and_total(shelf: LibraryShelf) -> None:
    assert "3" in shelf.status.text()


def test_a_partial_sweep_is_never_presented_as_complete(shelf: LibraryShelf) -> None:
    state = ScanSnapshot(
        phase="partial",
        root=shelf.root,
        walk_complete=True,
        books_found=3,
        books_indexed=2,
        books_rejected=1,
    )
    shelf.show_sweep(state)
    assert "incomplete" in shelf.progress.text().casefold()
    assert "1 book(s) not committed" in shelf.progress.text()


@pytest.mark.parametrize(
    "value, expected",
    [(0, "0 B"), (2048, "2 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB")],
)
def test_human_bytes(value: int, expected: str) -> None:
    assert human_bytes(value) == expected


# ─────────────────────────── the whole library ────────────────────────────


def test_every_book_in_the_datalake_is_listed_including_subfolders(shelf: LibraryShelf) -> None:
    titles = {shelf.model.row_at(shelf.model.index(row, 0)).title
              for row in range(shelf.model.rowCount())}
    assert titles == {"Alpha World", "Beta Days", "Gamma Report"}


def test_the_view_is_virtualized_not_one_widget_per_book(shelf: LibraryShelf) -> None:
    """The shelf must be a QListView over a model, never a QListWidget.

    This is the whole reason a ten-thousand-book library stays responsive, so
    it is asserted rather than assumed.
    """
    from PySide6.QtWidgets import QListView, QListWidget

    assert isinstance(shelf.view, QListView)
    assert not isinstance(shelf.view, QListWidget)
    assert shelf.view.model() is shelf.model


def test_only_one_page_is_materialised_at_a_time(tmp_path: Path, monkeypatch) -> None:
    _application()
    monkeypatch.setattr(shelf_module, "PAGE_SIZE", 4)
    root = tmp_path / "big"
    for number in range(11):
        make_epub(root / f"book{number:03}.epub", f"Book {number:03}", "An Author", "text")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        widget = LibraryShelf(index, str(root))
        assert widget.model.total == 11
        assert widget.model.rowCount() == 4          # one page, not eleven
        assert widget.model.canFetchMore()
        widget.model.fetchMore()
        assert widget.model.rowCount() == 8
        widget.model.fetchMore()
        assert widget.model.rowCount() == 11
        assert not widget.model.canFetchMore()       # and it stops cleanly
        widget.deleteLater()


def test_paging_never_repeats_or_drops_a_book(tmp_path: Path, monkeypatch) -> None:
    _application()
    monkeypatch.setattr(shelf_module, "PAGE_SIZE", 3)
    root = tmp_path / "big"
    for number in range(10):
        make_epub(root / f"b{number}.epub", f"Title {number}", "A", "t")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(root, workers=1)
        widget = LibraryShelf(index, str(root))
        while widget.model.canFetchMore():
            widget.model.fetchMore()
        paths = [widget.model.row_at(widget.model.index(row, 0)).path
                 for row in range(widget.model.rowCount())]
        assert len(paths) == len(set(paths)) == 10
        widget.deleteLater()


# ──────────────────────────────── searching ───────────────────────────────


def test_search_narrows_by_title_author_and_filename(shelf: LibraryShelf) -> None:
    shelf.search.setText("Alpha")
    shelf._run_search()
    assert shelf.model.rowCount() == 1
    assert shelf.model.row_at(shelf.model.index(0, 0)).title == "Alpha World"

    shelf.search.setText("Ben")
    shelf._run_search()
    assert shelf.model.row_at(shelf.model.index(0, 0)).title == "Beta Days"

    shelf.search.setText("gamma")
    shelf._run_search()
    assert shelf.model.row_at(shelf.model.index(0, 0)).title == "Gamma Report"


def test_search_is_debounced_not_run_per_keystroke(shelf: LibraryShelf) -> None:
    """Typing must not fire a query per character."""
    shelf.search.setText("Alph")
    assert shelf._debounce.isActive()          # queued, not executed
    assert shelf.model.rowCount() == 3         # still showing everything
    shelf._run_search()
    assert shelf.model.rowCount() == 1


def test_search_finds_nothing_gracefully(shelf: LibraryShelf) -> None:
    shelf.search.setText("zzzz-no-such-book")
    shelf._run_search()
    assert shelf.model.rowCount() == 0
    assert "0" in shelf.status.text()


def test_topic_mode_searches_inside_the_books(shelf: LibraryShelf) -> None:
    shelf._set_mode("content")
    assert shelf.model.mode == "content"
    shelf.search.setText("falconry")
    shelf._run_search()
    assert shelf.model.rowCount() == 1
    row = shelf.model.row_at(shelf.model.index(0, 0))
    assert row.title == "Beta Days"
    assert row.snippet                          # an excerpt comes back

    shelf._set_mode("meta")
    shelf._run_search()
    assert shelf.model.rowCount() == 0          # not in any title or author


def test_mode_toggles_are_mutually_exclusive(shelf: LibraryShelf) -> None:
    shelf._set_mode("content")
    assert shelf.mode_content.isChecked()
    assert not shelf.mode_meta.isChecked()
    shelf._set_mode("meta")
    assert shelf.mode_meta.isChecked()
    assert not shelf.mode_content.isChecked()


def test_type_chips_filter_the_shelf(shelf: LibraryShelf) -> None:
    shelf._set_extensions([".pdf"])
    assert shelf.model.rowCount() == 1
    assert shelf.model.row_at(shelf.model.index(0, 0)).ext == ".pdf"
    assert shelf.chip_pdf.isChecked() and not shelf.chip_all.isChecked()

    shelf._set_extensions([".epub"])
    assert shelf.model.rowCount() == 2

    shelf._set_extensions([])
    assert shelf.model.rowCount() == 3
    assert shelf.chip_all.isChecked()


def test_a_hostile_query_does_not_crash_the_shelf(shelf: LibraryShelf) -> None:
    for hostile in ('a OR b', '"unclosed', 'NEAR(x y)', '((('):
        shelf.search.setText(hostile)
        shelf._run_search()                     # must not raise
        assert shelf.model.rowCount() >= 0


# ──────────────────────────────── opening ─────────────────────────────────


def test_activating_a_row_requests_that_book(shelf: LibraryShelf) -> None:
    received: list[str] = []
    shelf.book_requested.connect(received.append)
    shelf._activate(shelf.model.index(0, 0))
    assert len(received) == 1
    assert Path(received[0]).is_file()


def test_activating_an_invalid_index_is_ignored(shelf: LibraryShelf) -> None:
    received: list[str] = []
    shelf.book_requested.connect(received.append)
    shelf._activate(QModelIndex())
    assert received == []


# ──────────────────────────── fallback and chrome ─────────────────────────


def test_recents_show_before_the_index_exists(tmp_path: Path) -> None:
    _application()
    root = tmp_path / "empty-index"
    book = make_epub(root / "solo.epub", "Solo Book", "Only Author", "words")
    with LibraryIndex(tmp_path / "i.db") as index:
        widget = LibraryShelf(index, str(root))     # never scanned
        assert widget.model.rowCount() == 0
        widget.set_books([{"path": str(book), "title": "Solo Book", "author": "Only Author"}])
        assert widget.model.rowCount() == 1
        assert widget.model.row_at(widget.model.index(0, 0)).title == "Solo Book"
        widget.deleteLater()


def test_a_recent_that_no_longer_exists_is_dropped(tmp_path: Path) -> None:
    _application()
    with LibraryIndex(tmp_path / "i.db") as index:
        widget = LibraryShelf(index, str(tmp_path))
        widget.set_books([{"path": str(tmp_path / "ghost.epub"), "title": "Ghost", "author": ""}])
        assert widget.model.rowCount() == 0
        widget.deleteLater()


def test_shelf_uses_per_pixel_precision_scrolling(shelf: LibraryShelf) -> None:
    assert shelf.view.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    assert shelf.view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_palette_reaches_both_the_stylesheet_and_the_delegate(shelf: LibraryShelf) -> None:
    colors = {
        "bg": "#000000", "panel": "#111111", "panel2": "#222222", "fg": "#ffffff",
        "muted": "#888888", "line": "#333333", "accent": "#00ff00", "accent2": "#003300",
        "hover": "#444444", "reading": "#050505",
    }
    shelf.apply_palette(colors)
    assert shelf.delegate.colors["accent"] == "#00ff00"
    assert "#00ff00" in shelf.styleSheet()


# ─────────────────────────── typing goes to search ────────────────────────
#
# The live defect this covers: on a real shelf, typing "sitchin" went nowhere.
# The characters were being eaten by QListView's own jump-to-item search, and
# on first show nothing had claimed the keyboard at all.


def _press(widget, text: str, key: Qt.Key | None = None) -> None:
    """Deliver one real key press to `widget`, the way Qt would."""
    if key is None:
        key = getattr(Qt.Key, f"Key_{text.upper()}")
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text))


def test_the_search_box_has_the_keyboard_as_soon_as_the_shelf_appears(
    shelf: LibraryShelf,
) -> None:
    shelf.show()
    _application().processEvents()
    assert shelf.focusWidget() is shelf.search
    shelf.hide()


def test_the_list_hands_typing_to_the_search_box(shelf: LibraryShelf) -> None:
    """The exact bug: characters typed at the list must land in the box."""
    for character in "sitchin":
        _press(shelf.view, character)
    assert shelf.search.text() == "sitchin"


def test_the_list_never_runs_its_own_jump_to_item_search(shelf: LibraryShelf) -> None:
    """QListView.keyboardSearch is what silently swallowed the typing."""
    assert isinstance(shelf.view, BookListView)
    shelf.view.keyboardSearch("alpha")          # must be a no-op, not a jump
    assert not shelf.view.selectionModel().selectedIndexes()


def test_backspace_at_the_list_edits_the_search_box(shelf: LibraryShelf) -> None:
    shelf.search.setText("alphax")
    _press(shelf.view, "", Qt.Key.Key_Backspace)
    assert shelf.search.text() == "alpha"


def test_typing_anywhere_on_the_shelf_reaches_the_search_box(shelf: LibraryShelf) -> None:
    """Focus may sit on a chip after a Tab; the next word is still a search."""
    shelf.chip_pdf.setFocus()
    for character in "gamma":
        _press(shelf, character)
    assert shelf.search.text() == "gamma"


def test_typing_appends_rather_than_replacing_the_query(shelf: LibraryShelf) -> None:
    shelf.search.setText("alpha")
    shelf.view.setFocus()
    _press(shelf.view, "x")
    assert shelf.search.text() == "alphax"


def test_a_keyboard_chord_still_belongs_to_the_list(shelf: LibraryShelf) -> None:
    """Ctrl+A is not a letter the reader wants in the search box."""
    shelf.view.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier, "a")
    )
    assert shelf.search.text() == ""


def test_arrow_keys_still_move_through_the_shelf(shelf: LibraryShelf) -> None:
    shelf.view.setCurrentIndex(shelf.model.index(0, 0))
    _press(shelf.view, "", Qt.Key.Key_Down)
    assert shelf.view.currentIndex().row() == 1
    assert shelf.search.text() == ""            # navigation is not typing


# ───────────────────────────── the folder line ────────────────────────────


def test_the_sub_folder_line_is_brighter_than_the_muted_facts() -> None:
    """It used to be painted in the same grey as the line above it."""
    colors = {"muted": "#929bad", "fg": "#ecedea"}
    folder = folder_color(colors)
    from PySide6.QtGui import QColor

    muted = QColor(colors["muted"])
    assert folder.lightness() > muted.lightness()
    assert folder != QColor(colors["fg"])       # still quieter than the title


@pytest.mark.parametrize(
    "muted, foreground",
    [("#929bad", "#ecedea"), ("#6b7280", "#22252a"), ("#766650", "#3d3328")],
)
def test_the_folder_line_follows_every_palette(muted: str, foreground: str) -> None:
    """Dark, light and sepia: derived, never a hard-coded shade."""
    from PySide6.QtGui import QColor

    folder = folder_color({"muted": muted, "fg": foreground})
    distance = abs(folder.lightness() - QColor(muted).lightness())
    assert distance >= 10                       # visibly lifted off the facts


def test_blend_walks_between_two_colours() -> None:
    assert blend("#000000", "#ffffff", 0.0).name() == "#000000"
    assert blend("#000000", "#ffffff", 1.0).name() == "#ffffff"
    assert blend("#000000", "#ffffff", 0.5).name() == "#808080"


# ──────────────────────── row typography (pixel themes) ───────────────────
#
# Lumen's theme sizes text in pixels.  The delegate used to add a *point* to
# that, which asked Qt for point size zero: the titles quietly stayed at body
# size and every repaint printed a warning - 1,458 of them on one screenful.


def test_fonts_scale_when_the_theme_measures_in_pixels() -> None:
    from PySide6.QtGui import QFont

    base = QFont("Segoe UI")
    base.setPixelSize(13)
    assert base.pointSizeF() == -1               # this is the trap

    bigger = scaled_font(base, 1.0)
    smaller = scaled_font(base, -1.0)
    assert bigger.pixelSize() > 13               # actually larger, not ignored
    assert smaller.pixelSize() < 13
    assert bigger.pointSizeF() == -1             # still measured in pixels


def test_fonts_scale_when_the_theme_measures_in_points() -> None:
    from PySide6.QtGui import QFont

    base = QFont("Segoe UI")
    base.setPointSizeF(10.0)
    assert scaled_font(base, 1.0).pointSizeF() == pytest.approx(11.0)
    assert scaled_font(base, -1.0).pointSizeF() == pytest.approx(9.0)


def test_a_font_never_shrinks_below_its_floor() -> None:
    from PySide6.QtGui import QFont

    base = QFont("Segoe UI")
    base.setPointSizeF(8.0)
    assert scaled_font(base, -5.0, floor_points=7.0).pointSizeF() == pytest.approx(7.0)


def test_painting_a_row_never_asks_qt_for_an_impossible_font(shelf: LibraryShelf) -> None:
    """The regression itself: paint a real row and listen for Qt's complaint."""
    from PySide6.QtCore import QRect, qInstallMessageHandler
    from PySide6.QtGui import QFont, QPainter, QPixmap
    from PySide6.QtWidgets import QStyleOptionViewItem

    shelf.delegate.set_root(str(Path(shelf.root)))
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 700, 96)
    option.font = QFont("Segoe UI")
    option.font.setPixelSize(13)                 # exactly what the theme does

    complaints: list[str] = []
    previous = qInstallMessageHandler(lambda mode, context, text: complaints.append(text))
    try:
        canvas = QPixmap(700, 96)
        painter = QPainter(canvas)
        for row in range(shelf.model.rowCount()):
            shelf.delegate.paint(painter, option, shelf.model.index(row, 0))
        painter.end()
    finally:
        qInstallMessageHandler(previous)

    assert not [text for text in complaints if "Point size" in text], complaints


def test_the_title_is_painted_larger_than_the_facts_beneath_it(shelf: LibraryShelf) -> None:
    """Typography, not just silence: the fix must restore the size hierarchy."""
    from PySide6.QtGui import QFont

    base = QFont("Segoe UI")
    base.setPixelSize(13)
    assert scaled_font(base, 1.0).pixelSize() > scaled_font(base, -1.0, 7.5).pixelSize()


def test_a_snippet_row_is_given_the_extra_height_it_needs(shelf: LibraryShelf) -> None:
    """Four stacked lines do not fit in a three-line row."""
    from PySide6.QtWidgets import QStyleOptionViewItem

    option = QStyleOptionViewItem()
    plain = shelf.delegate.sizeHint(option, shelf.model.index(0, 0))
    assert plain.height() == shelf_module.ROW_HEIGHT

    shelf._set_mode("content")
    shelf.search.setText("falconry")
    shelf._run_search()
    assert shelf.model.row_at(shelf.model.index(0, 0)).snippet
    tall = shelf.delegate.sizeHint(option, shelf.model.index(0, 0))
    assert tall.height() == shelf_module.ROW_HEIGHT_SNIPPET > plain.height()


def test_source_path_line_is_visible_for_root_and_nested_books(shelf: LibraryShelf) -> None:
    """A root-level file is identity, not redundant tooltip-only information."""
    from PySide6.QtGui import QFont, QFontMetrics

    font = QFont("Segoe UI")
    font.setPixelSize(12)
    metrics = QFontMetrics(font)
    rows = {
        shelf.model.row_at(shelf.model.index(index, 0)).title:
        shelf.model.row_at(shelf.model.index(index, 0))
        for index in range(shelf.model.rowCount())
    }

    for title in ("Alpha World", "Beta Days"):
        text = source_path_text(rows[title].path, metrics, 1200)
        assert text.startswith("FILE")
        assert text.endswith(Path(rows[title].path).name)
        assert os.path.normpath(rows[title].path) in text


def test_source_path_middle_elision_keeps_the_filename_visible() -> None:
    from PySide6.QtGui import QFont, QFontMetrics

    font = QFont("Segoe UI")
    font.setPixelSize(12)
    metrics = QFontMetrics(font)
    path = r"C:\Books\Research\Energetic Materials\pasteexplosivepaper.pdf"
    text = source_path_text(path, metrics, 330)

    assert text.startswith("FILE")
    assert "…" in text
    assert text.endswith("pasteexplosivepaper.pdf")


def test_source_paths_are_visible_in_the_live_shelf(shelf: LibraryShelf) -> None:
    """Keep the real card surface open for the per-test visual capture."""
    from PySide6.QtTest import QTest

    shelf.resize(1400, 760)
    shelf.show()
    shelf.raise_()
    shelf.activateWindow()
    QApplication.processEvents()
    QTest.qWait(250)

    assert shelf.view.isVisible()
    assert shelf.model.rowCount() == 3


# ───────────────────────────── the row outline ────────────────────────────
#
# The cards were outlined in the theme's hairline `line` colour, which sits at
# about 1.2:1 against the row's own fill - no visible edge at all, so the shelf
# read as one undifferentiated column.  Angela called it the near-invisible line.

PALETTES = {
    "dark":  {"line": "#252d3b", "fg": "#ecedea", "panel2": "#171d29"},
    "light": {"line": "#d7d2c8", "fg": "#22252a", "panel2": "#ffffff"},
    "sepia": {"line": "#c9b997", "fg": "#3d3328", "panel2": "#f4ead4"},
}


def _contrast(first: str, second: str) -> float:
    """WCAG relative-luminance contrast ratio between two colours."""
    from PySide6.QtGui import QColor

    def luminance(value: str) -> float:
        color = QColor(value)
        channels = []
        for raw in (color.redF(), color.greenF(), color.blueF()):
            channels.append(raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("theme", sorted(PALETTES))
def test_the_row_outline_is_actually_visible_against_the_row(theme: str) -> None:
    colors = PALETTES[theme]
    border = row_border_color(colors).name()
    assert _contrast(border, colors["panel2"]) >= 2.8, (theme, border)


@pytest.mark.parametrize("theme", sorted(PALETTES))
def test_the_row_outline_beats_the_hairline_it_replaced(theme: str) -> None:
    colors = PALETTES[theme]
    fill = colors["panel2"]
    was = _contrast(colors["line"], fill)
    now = _contrast(row_border_color(colors).name(), fill)
    assert now > was * 1.5, (theme, was, now)


def test_the_row_outline_stays_quieter_than_the_text_on_it() -> None:
    """A card edge that shouts is as wrong as one that vanishes."""
    colors = PALETTES["dark"]
    border = row_border_color(colors).name()
    assert _contrast(border, colors["panel2"]) < _contrast(colors["fg"], colors["panel2"])


def test_a_selected_row_still_takes_the_accent_outline(shelf: LibraryShelf) -> None:
    """The lifted outline must not swallow the selection cue."""
    colors = dict(PALETTES["dark"], accent="#63d1ad")
    assert row_border_color(colors).name() != colors["accent"]
