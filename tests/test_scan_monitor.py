# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The sweep monitor must never hide a core it is supposed to be showing.

The monitor's whole purpose is proof: one tile per extractor process, so a
stalled fleet looks stalled.  The first version painted its tiles from the top
of a widget that the layout had squeezed to the leftover height, so on a
22-process fleet the fourth row was sliced through the middle and the fifth was
not drawn at all - the reader was shown three and a half rows and no hint that
there was more.  A monitor that hides cores is worse than no monitor, because
it looks authoritative.

These tests pin the invariant: whatever the fleet size and whatever the window
height, every tile is either drawn whole or reachable by scrolling.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from lumen_reader.scan_monitor import CoreGrid, ScanMonitorDialog
from lumen_reader.turbo_scan import ScanConfig, ScanSnapshot, TurboScanner, WorkerSnapshot


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workers(count: int) -> list[WorkerSnapshot]:
    return [
        WorkerSnapshot(
            index=index,
            pid=9000 + index,
            state="busy" if index % 3 else "idle",
            done=index * 2,
            failed=0,
            bytes_done=index * 1024,
            current=f"Encyclopedia of Explosives Vol {index}.pdf",
            busy_seconds=float(index),
        )
        for index in range(count)
    ]


# ── the grid claims the height its tiles actually need ────────────────────


@pytest.mark.parametrize("fleet", [1, 4, 12, 22, 64, 128])
def test_no_tile_is_ever_painted_outside_the_grid(fleet: int) -> None:
    """Every tile's bottom edge must land inside the widget that draws it."""
    _application()
    grid = CoreGrid()
    grid.resize(1090, 200)          # a deliberately too-short box
    grid.set_workers(_workers(fleet))

    columns = grid._columns_for(grid.width())
    rows = grid.rows_for_width(grid.width())
    assert rows * columns >= fleet, "the grid must have a row for every process"

    last_row_bottom = (rows - 1) * (CoreGrid.TILE_HEIGHT + CoreGrid.GAP) + CoreGrid.TILE_HEIGHT
    assert grid.minimumHeight() >= last_row_bottom, (
        f"{fleet} tiles need {last_row_bottom}px but the grid only claims "
        f"{grid.minimumHeight()}px - the last row would be clipped"
    )


def test_the_grid_grows_when_the_fleet_does() -> None:
    """A machine with more cores gets more rows, not smaller ones."""
    _application()
    grid = CoreGrid()
    grid.resize(1090, 400)

    grid.set_workers(_workers(5))
    small = grid.minimumHeight()
    grid.set_workers(_workers(40))
    large = grid.minimumHeight()

    assert large > small
    assert (small + CoreGrid.GAP) % (CoreGrid.TILE_HEIGHT + CoreGrid.GAP) == 0
    assert (large + CoreGrid.GAP) % (CoreGrid.TILE_HEIGHT + CoreGrid.GAP) == 0


def test_a_narrower_window_means_more_rows_not_lost_tiles() -> None:
    """Squeezing the window rewraps the fleet; it never drops a core."""
    _application()
    grid = CoreGrid()
    grid.set_workers(_workers(22))

    wide_rows = grid.rows_for_width(1300)
    narrow_rows = grid.rows_for_width(460)

    assert narrow_rows > wide_rows
    assert grid._columns_for(460) * narrow_rows >= 22
    assert grid.height_for_width(460) > grid.height_for_width(1300)


# ── the dialog scrolls rather than clips ──────────────────────────────────


def _monitor() -> ScanMonitorDialog:
    config = ScanConfig()
    scanner = TurboScanner("unused.db", os.getcwd(), config)
    return ScanMonitorDialog(scanner)


def test_the_fleet_lives_in_a_scroll_area() -> None:
    """However short the window gets, the cores stay reachable."""
    _application()
    dialog = _monitor()
    try:
        assert isinstance(dialog.fleet_area, QScrollArea)
        assert dialog.fleet_area.widget() is dialog.grid
        assert dialog.fleet_area.widgetResizable() is True
        # Horizontal scrolling would mean tiles hidden off to the side, which is
        # the same failure turned ninety degrees.  The grid rewraps instead.
        assert (dialog.fleet_area.horizontalScrollBarPolicy()
                is Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    finally:
        dialog.deleteLater()


def test_a_short_window_scrolls_the_fleet_instead_of_hiding_it() -> None:
    """The exact regression: a 22-process fleet in a window too short for it."""
    _application()
    dialog = _monitor()
    try:
        dialog.resize(1120, 640)
        dialog.show()
        QApplication.processEvents()

        dialog.grid.set_workers(_workers(22))
        QApplication.processEvents()

        needed = dialog.grid.height_for_width(dialog.grid.width())
        viewport = dialog.fleet_area.viewport().height()
        assert dialog.grid.height() >= needed, "the grid was squeezed and would clip"
        if needed > viewport:
            bar = dialog.fleet_area.verticalScrollBar()
            assert bar.maximum() > 0, "tiles overflow the viewport with no way to scroll to them"
    finally:
        dialog.hide()
        dialog.deleteLater()


def test_the_monitor_opens_big_enough_for_a_full_fleet() -> None:
    """The default size shows a machine-sized fleet without any scrolling."""
    _application()
    dialog = _monitor()
    try:
        assert dialog.width() >= 1200
        assert dialog.height() >= 900
        assert dialog.minimumHeight() <= 700, "must still fit a small laptop screen"
    finally:
        dialog.deleteLater()


def test_the_monitor_survives_a_snapshot_with_no_workers() -> None:
    """An arming fleet has no tiles yet, and must not divide by zero."""
    _application()
    dialog = _monitor()
    try:
        dialog._paint_banner(ScanSnapshot())
        dialog.grid.set_workers([])
        assert dialog.grid.minimumHeight() >= CoreGrid.TILE_HEIGHT
    finally:
        dialog.deleteLater()
