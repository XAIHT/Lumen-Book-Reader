# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The Sweep Monitor: proof, on screen, that the fleet is working.

The old indexer reported itself through one eighteen-pixel progress bar in the
corner of the shelf, which hid four seconds after it finished.  When it scanned
the wrong folder and found nothing, that bar was indistinguishable from a bar
that had done the job - the button looked broken because nothing on screen could
tell you otherwise.

This window is the answer to that.  Every extractor process gets a tile carrying
its own PID, its own throughput, and the title of the book it is reading *right
now*; the numbers come straight out of the shared-memory block each worker
writes, at ten frames a second, so a stalled fleet looks stalled and a busy one
visibly seethes.  Nothing here is decoration: if the sweep is not doing
anything, this window is designed to make that obvious within one second.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .dialog_layout import ScreenFittingDialog
from .turbo_scan import ScanSnapshot, TurboScanner

#: Ten frames a second.  Fast enough that a busy fleet visibly churns, slow
#: enough that painting the monitor never competes with the fleet for a core.
REFRESH_MS = 100

DEFAULT_COLORS = {
    "bg": "#0b0e14", "panel": "#111620", "panel2": "#171d29", "fg": "#ecedea",
    "muted": "#929bad", "line": "#252d3b", "accent": "#63d1ad", "accent2": "#112d28",
    "hover": "#202837", "reading": "#11141c",
}


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB", "EB"):
        if abs(value) < 1024 or unit == "EB":
            return f"{value:,.0f} {unit}" if unit in {"B", "KB"} else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} EB"


def human_seconds(value: float) -> str:
    if value < 0:
        return "—"
    value = int(value)
    if value < 60:
        return f"{value}s"
    if value < 3600:
        return f"{value // 60}m {value % 60:02d}s"
    return f"{value // 3600}h {(value % 3600) // 60:02d}m"


def blend(first: str, second: str, weight: float) -> QColor:
    start, end = QColor(first), QColor(second)
    return QColor(
        round(start.red() + (end.red() - start.red()) * weight),
        round(start.green() + (end.green() - start.green()) * weight),
        round(start.blue() + (end.blue() - start.blue()) * weight),
    )


# ────────────────────────────── the core grid ──────────────────────────────


class CoreGrid(QWidget):
    """One tile per extractor process, painted from its shared-memory vitals.

    Custom-painted rather than built from widgets: a fleet is one tile per
    logical processor, the tiles change ten times a second, and sixty-odd
    live QWidgets updating at that rate would cost more than the sweep does.
    """

    TILE_WIDTH = 208
    TILE_HEIGHT = 68
    GAP = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.colors = dict(DEFAULT_COLORS)
        self.workers: list = []
        self.busiest = 1
        self.setMinimumHeight(self.TILE_HEIGHT)
        # Vertically the grid asks for exactly the room its rows need - never
        # more, never less.  Expanding here let the layout hand it a height that
        # fell *between* two whole rows, and since the tiles are painted from
        # the top down, the leftover row was simply sliced off at the widget
        # edge: on a 22-process fleet the reader saw three and a half rows of
        # cores and no indication that the rest existed.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_palette(self, colors: dict[str, str]) -> None:
        self.colors = dict(colors)
        self.update()

    def set_workers(self, workers: list) -> None:
        self.workers = workers
        self.busiest = max([worker.done for worker in workers] or [1]) or 1
        self._claim_height()
        self.update()

    def _columns_for(self, width: int) -> int:
        usable = max(1, width)
        return max(1, min(len(self.workers) or 1,
                          (usable + self.GAP) // (self.TILE_WIDTH + self.GAP)))

    def _columns(self) -> int:
        return self._columns_for(self.width())

    def rows_for_width(self, width: int) -> int:
        """How many tile rows the current fleet needs at *width* pixels."""
        columns = self._columns_for(width)
        return max(1, (len(self.workers) + columns - 1) // columns)

    def height_for_width(self, width: int) -> int:
        """The exact height those rows occupy, inner gaps included."""
        return self.rows_for_width(width) * (self.TILE_HEIGHT + self.GAP) - self.GAP

    def _claim_height(self) -> None:
        """Take the full height of the fleet, so no tile is ever half-drawn.

        The scroll area around this widget sizes its scrollbar from the minimum
        height, so claiming the honest number here is what turns an invisible
        clip into a visible scroll.
        """
        needed = self.height_for_width(self.width())
        if self.minimumHeight() != needed:
            self.setMinimumHeight(needed)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        return QSize(self.TILE_WIDTH, self.height_for_width(self.width()))

    def minimumSizeHint(self) -> QSize:
        return QSize(self.TILE_WIDTH, self.height_for_width(self.width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._claim_height()

    def paintEvent(self, event) -> None:
        if not self.workers:
            return
        colors = self.colors
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        columns = self._columns()
        span = (self.width() - (columns - 1) * self.GAP) / columns

        label_font = QFont(self.font())
        label_font.setPixelSize(10)
        label_font.setBold(True)
        path_font = QFont(self.font())
        path_font.setPixelSize(10)
        count_font = QFont(self.font())
        count_font.setPixelSize(14)
        count_font.setBold(True)

        for position, worker in enumerate(self.workers):
            row, column = divmod(position, columns)
            left = round(column * (span + self.GAP))
            top = row * (self.TILE_HEIGHT + self.GAP)
            tile = QRect(left, top, round(span), self.TILE_HEIGHT)
            self._paint_tile(painter, tile, worker, colors, label_font, path_font, count_font)
        painter.end()

    def _paint_tile(self, painter, tile, worker, colors, label_font, path_font, count_font) -> None:
        busy = worker.state == "busy"
        publishing = worker.state == "publishing"
        active = busy or publishing
        stopped = worker.state == "stopped"

        painter.setBrush(QColor(colors["accent2"] if active else colors["panel2"]))
        painter.setPen(QPen(QColor(colors["accent"]) if active
                            else blend(colors["line"], colors["fg"], 0.30), 1))
        painter.drawRoundedRect(tile, 8, 8)

        # The activity rail down the left edge: the fastest read of the whole
        # grid.  A fleet doing nothing is a column of grey; a fleet at work is
        # a column of accent.
        rail = QRect(tile.left() + 5, tile.top() + 6, 4, tile.height() - 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors["accent"]) if active else
                         blend(colors["line"], colors["muted"], 0.5 if stopped else 0.2))
        painter.drawRoundedRect(rail, 2, 2)

        text_left = tile.left() + 16
        text_width = tile.width() - 22

        painter.setFont(label_font)
        painter.setPen(QColor(colors["accent"] if active else colors["muted"]))
        heading = f"CORE {worker.index:02d}"
        if worker.pid:
            heading += f"   ·   PID {worker.pid}"
        if stopped:
            heading += "   ·   DONE"
        painter.drawText(QRect(text_left, tile.top() + 5, text_width, 13),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), heading)

        # Share bar: this worker's books against the busiest worker's, so an
        # unevenly loaded fleet is visible at a glance.
        bar = QRect(text_left, tile.top() + 22, text_width - 58, 7)
        painter.setBrush(blend(colors["panel2"], colors["line"], 0.9))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(bar, 3, 3)
        fraction = min(1.0, worker.done / self.busiest) if self.busiest else 0.0
        if fraction > 0:
            filled = QRect(bar.left(), bar.top(), max(3, round(bar.width() * fraction)), bar.height())
            painter.setBrush(QColor(colors["accent"]))
            painter.drawRoundedRect(filled, 3, 3)

        painter.setFont(count_font)
        painter.setPen(QColor(colors["fg"]))
        painter.drawText(QRect(bar.right() + 6, tile.top() + 16, 52, 18),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         f"{worker.done:,}")

        painter.setFont(path_font)
        painter.setPen(QColor(colors["accent"]) if active else QColor(colors["muted"]))
        if busy and worker.current:
            label = os.path.basename(worker.current)
            if worker.busy_seconds > 3:
                label = f"{label}   ({worker.busy_seconds:,.0f}s)"
        elif publishing:
            label = "publishing to the index…"
        elif stopped:
            label = f"finished · {worker.failed:,} unreadable" if worker.failed else "finished"
        else:
            label = "waiting for a book…"
        metrics = QFontMetrics(path_font)
        painter.drawText(
            QRect(text_left, tile.top() + 46, text_width, 14),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(label, Qt.TextElideMode.ElideMiddle, text_width),
        )


# ─────────────────────────────── the sparkline ─────────────────────────────


class Sparkline(QWidget):
    """Throughput over the last minute, as a filled curve."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.colors = dict(DEFAULT_COLORS)
        self.samples: list[float] = []
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_palette(self, colors: dict[str, str]) -> None:
        self.colors = dict(colors)
        self.update()

    def set_samples(self, samples: list[float]) -> None:
        self.samples = samples
        self.update()

    def paintEvent(self, event) -> None:
        colors = self.colors
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frame = self.rect().adjusted(0, 0, -1, -1)
        painter.setBrush(QColor(colors["panel2"]))
        painter.setPen(QPen(blend(colors["line"], colors["fg"], 0.25), 1))
        painter.drawRoundedRect(frame, 8, 8)

        samples = self.samples
        if len(samples) < 2:
            painter.setPen(QColor(colors["muted"]))
            font = QFont(self.font())
            font.setPixelSize(10)
            painter.setFont(font)
            painter.drawText(frame, Qt.AlignmentFlag.AlignCenter, "measuring throughput…")
            painter.end()
            return

        peak = max(samples) or 1.0
        inner = frame.adjusted(8, 8, -8, -8)
        step = inner.width() / (len(samples) - 1)
        points = [
            (inner.left() + index * step,
             inner.bottom() - (value / peak) * inner.height())
            for index, value in enumerate(samples)
        ]

        curve = QPolygonF([QPointF(x, y) for x, y in points])
        area = QPolygonF(curve)
        area.append(QPointF(points[-1][0], inner.bottom()))
        area.append(QPointF(points[0][0], inner.bottom()))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(blend(colors["panel2"], colors["accent"], 0.30))
        painter.drawPolygon(area)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(colors["accent"]), 2))
        painter.drawPolyline(curve)

        font = QFont(self.font())
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(colors["muted"]))
        painter.drawText(inner, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
                         f"peak {peak:,.0f} books/s")
        painter.end()


# ──────────────────────────────── the window ───────────────────────────────


class ScanMonitorDialog(ScreenFittingDialog):
    """The live face of one sweep."""

    finished_scan = Signal(object)   # ScanSnapshot

    def __init__(self, scanner: TurboScanner, parent: QWidget | None = None,
                 colors: dict[str, str] | None = None):
        super().__init__(parent)
        self.scanner = scanner
        self.colors = dict(colors or DEFAULT_COLORS)
        self._announced = False
        self._log_lines = 0

        self.setWindowTitle("Lumen — Sweeping the library")
        self.setObjectName("sweepMonitor")
        # Opened big enough that a machine-sized fleet fits without scrolling at
        # all - five columns of tiles need about 1,090 pixels of grid, and the
        # rest of the window needs the height.  ScreenFittingDialog shrinks this
        # to whatever the monitor can actually show, and the fleet area scrolls
        # from there, so asking generously here costs a small screen nothing.
        self.setMinimumSize(880, 620)
        self.resize(1260, 980)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        # ── banner ─────────────────────────────────────────────────────────
        self.phase_label = QLabel("ARMING THE FLEET")
        self.phase_label.setObjectName("sweepPhase")
        layout.addWidget(self.phase_label)

        self.root_label = QLabel(str(scanner.root))
        self.root_label.setObjectName("sweepRoot")
        self.root_label.setWordWrap(True)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.root_label)

        self.fleet_label = QLabel("")
        self.fleet_label.setObjectName("sweepFleet")
        self.fleet_label.setWordWrap(True)
        layout.addWidget(self.fleet_label)

        # ── headline counters ──────────────────────────────────────────────
        self.stats_frame = QFrame()
        self.stats_frame.setObjectName("sweepStats")
        stats = QGridLayout(self.stats_frame)
        stats.setContentsMargins(16, 12, 16, 12)
        stats.setHorizontalSpacing(10)
        stats.setVerticalSpacing(2)
        self._stat_widgets: dict[str, QLabel] = {}
        for column, (key, caption) in enumerate((
            ("found", "BOOKS FOUND"), ("indexed", "INDEXED OK"), ("skipped", "ALREADY CURRENT"),
            ("failed", "UNREADABLE"), ("dirs", "FOLDERS SWEPT"), ("bytes", "BYTES SEEN"),
        )):
            value = QLabel("0")
            value.setObjectName("sweepStatValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption_label = QLabel(caption)
            caption_label.setObjectName("sweepStatCaption")
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stats.addWidget(value, 0, column)
            stats.addWidget(caption_label, 1, column)
            self._stat_widgets[key] = value
        layout.addWidget(self.stats_frame)

        # ── progress + rates ───────────────────────────────────────────────
        self.progress = QProgressBar()
        self.progress.setObjectName("sweepProgress")
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(24)
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)

        rate_row = QHBoxLayout()
        rate_row.setSpacing(10)
        self.rate_label = QLabel("—")
        self.rate_label.setObjectName("sweepRate")
        rate_row.addWidget(self.rate_label)
        rate_row.addStretch(1)
        self.eta_label = QLabel("")
        self.eta_label.setObjectName("sweepEta")
        rate_row.addWidget(self.eta_label)
        layout.addLayout(rate_row)

        self.sparkline = Sparkline()
        layout.addWidget(self.sparkline)

        # ── the fleet ──────────────────────────────────────────────────────
        # Not "one process per core": that is what this fleet is on a machine
        # with cores to spare, and a promise Lumen deliberately breaks on a
        # four-core laptop or a spinning disk.  The tile count says what the
        # fleet actually is; the heading must not contradict it.
        fleet_heading = QLabel("THE FLEET  —  one tile per extractor process")
        fleet_heading.setObjectName("sweepSection")
        layout.addWidget(fleet_heading)
        self.grid = CoreGrid()
        # The fleet is as big as the machine gives it: twenty-two processes on
        # this one, a hundred and twenty-eight on a workstation.  However many
        # tiles that is, each one is either drawn whole or scrolled to.  The
        # monitor exists to prove the fleet is working, and a row cut in half at
        # the bottom of a fixed box proves the opposite - it hides exactly the
        # cores the reader came here to look at.
        self.fleet_area = QScrollArea()
        self.fleet_area.setObjectName("sweepFleetArea")
        self.fleet_area.setWidget(self.grid)
        self.fleet_area.setWidgetResizable(True)
        self.fleet_area.setFrameShape(QFrame.Shape.NoFrame)
        self.fleet_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.fleet_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.fleet_area.setMinimumHeight(CoreGrid.TILE_HEIGHT * 2 + CoreGrid.GAP)
        layout.addWidget(self.fleet_area, 1)

        # ── log ────────────────────────────────────────────────────────────
        log_heading = QLabel("SWEEP LOG")
        log_heading.setObjectName("sweepSection")
        layout.addWidget(log_heading)
        self.log = QPlainTextEdit()
        self.log.setObjectName("sweepLog")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(112)
        layout.addWidget(self.log)

        # ── buttons ────────────────────────────────────────────────────────
        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        self.open_button = QPushButton("Open this folder")
        self.open_button.setObjectName("sweepSubtle")
        self.open_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("sweepSubtle")
        self.pause_button.clicked.connect(self._toggle_pause)
        buttons.addWidget(self.pause_button)
        self.stop_button = QPushButton("Stop the sweep")
        self.stop_button.setObjectName("sweepStop")
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.stop_button)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("sweepPrimary")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.apply_palette(self.colors)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ── behaviour ──────────────────────────────────────────────────────────

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.scanner.root)))

    def _toggle_pause(self) -> None:
        if self.scanner.paused:
            self.scanner.resume()
        else:
            self.scanner.pause()

    def _stop(self) -> None:
        self.scanner.cancel()
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping…")

    def _refresh(self) -> None:
        state = self.scanner.snapshot()
        self._paint_banner(state)
        self._paint_stats(state)
        self._paint_progress(state)
        self.grid.set_workers(state.workers)
        self.sparkline.set_samples(state.history)
        self._paint_log(state)

        if not state.running and not self._announced:
            self._announced = True
            self._timer.setInterval(1000)
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.close_button.setText("Done — close")
            self.close_button.setDefault(True)
            self.finished_scan.emit(state)

    def _paint_banner(self, state: ScanSnapshot) -> None:
        headline = {
            "starting": "ARMING THE FLEET",
            "sweeping": "SWEEPING" + ("  ·  PAUSED" if state.paused else ""),
            "finishing": "FINISHING THE LAST BOOKS",
            "failing": "STOPPING AFTER A FAILURE",
            "done": "SWEEP COMPLETE",
            "cancelled": "SWEEP STOPPED",
            "partial": "SWEEP INCOMPLETE",
            "error": "SWEEP FAILED",
        }.get(state.phase, state.phase.upper())
        self.phase_label.setText(f"{headline}   ·   {human_seconds(state.elapsed)}")
        self.pause_button.setText("Resume" if state.paused else "Pause")

        fleet = (
            f"{state.processes} extractor process"
            f"{'' if state.processes == 1 else 'es'} at "
            f"{(state.priority or '?').upper()} priority   ·   "
            f"{state.active_workers} extracting or publishing now   ·   "
            f"{state.walkers} walker threads"
        )
        if state.backend:
            fleet += f"   ·   engine: {state.backend}"
        if state.walk_complete:
            fleet += "   ·   walk finished"
        self.fleet_label.setText(fleet)

    def _paint_stats(self, state: ScanSnapshot) -> None:
        self._stat_widgets["found"].setText(f"{state.books_found:,}")
        self._stat_widgets["indexed"].setText(f"{state.books_indexed:,}")
        self._stat_widgets["skipped"].setText(f"{state.books_unchanged:,}")
        self._stat_widgets["failed"].setText(f"{state.books_failed:,}")
        self._stat_widgets["dirs"].setText(f"{state.dirs_swept:,}")
        self._stat_widgets["bytes"].setText(human_bytes(state.bytes_found))

    def _paint_progress(self, state: ScanSnapshot) -> None:
        stale = state.books_found - state.books_unchanged
        settled = state.books_indexed + state.books_failed
        if state.walk_complete and stale > 0:
            self.progress.setRange(0, stale)
            self.progress.setValue(settled)
            self.progress.setFormat(f"%v of %m books committed   ({settled * 100 // max(1, stale)}%)")
        elif state.walk_complete:
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self.progress.setFormat("Every book was already current")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(
                f"Listing the tree — {state.dirs_swept:,} folders, {state.books_found:,} books so far"
            )

        self.rate_label.setText(
            f"{state.books_per_second:,.1f} books/s   ·   "
            f"{human_bytes(state.bytes_per_second)}/s"
        )
        if state.eta_seconds >= 0 and state.running:
            self.eta_label.setText(f"about {human_seconds(state.eta_seconds)} left")
        elif state.running and not state.walk_complete:
            self.eta_label.setText("estimating once the walk finishes…")
        else:
            self.eta_label.setText("")

    def _paint_log(self, state: ScanSnapshot) -> None:
        if len(state.messages) <= self._log_lines:
            return
        for line in state.messages[self._log_lines:]:
            self.log.appendPlainText(line)
        self._log_lines = len(state.messages)

    def closeEvent(self, event) -> None:
        """Closing the window watches less; it never abandons the sweep.

        The scan is a background job with a real cost, so a stray click on the
        title bar must not silently throw away half an hour of reading.  The
        sweep keeps running and the shelf still refreshes when it lands.
        """
        self._timer.stop()
        super().closeEvent(event)

    # ── theming ────────────────────────────────────────────────────────────

    def apply_palette(self, colors: dict[str, str]) -> None:
        self.colors = dict(colors)
        self.grid.set_palette(colors)
        self.sparkline.set_palette(colors)
        c = colors
        self.setStyleSheet(f"""
            #sweepMonitor {{ background: {c['bg']}; }}
            QLabel {{ color: {c['fg']}; }}
            #sweepPhase {{ color: {c['accent']}; font-size: 21px; font-weight: 850;
                           letter-spacing: 2px; }}
            #sweepRoot {{ color: {c['fg']}; font-size: 13px; font-weight: 700; }}
            #sweepFleet {{ color: {c['muted']}; font-size: 11px; }}
            #sweepSection {{ color: {c['muted']}; font-size: 10px; font-weight: 800;
                             letter-spacing: 2px; padding-top: 3px; }}
            #sweepStats {{ background: {c['panel']}; border: 1px solid {c['line']};
                           border-radius: 12px; }}
            #sweepStatValue {{ color: {c['accent']}; font-size: 24px; font-weight: 850; }}
            #sweepStatCaption {{ color: {c['muted']}; font-size: 9px; font-weight: 750;
                                 letter-spacing: 1.4px; }}
            #sweepRate {{ color: {c['fg']}; font-size: 13px; font-weight: 800; }}
            #sweepEta {{ color: {c['muted']}; font-size: 12px; }}
            #sweepProgress {{ color: {c['fg']}; background: {c['panel2']};
                              border: 1px solid {c['line']}; border-radius: 11px;
                              font-size: 11px; font-weight: 750; }}
            #sweepProgress::chunk {{ background: {c['accent']}; border-radius: 10px; }}
            #sweepFleetArea, #sweepFleetArea > QWidget > QWidget {{
                background: transparent; border: none; }}
            #sweepFleetArea QScrollBar:vertical {{ background: transparent; width: 10px;
                                                   margin: 0; border: none; }}
            #sweepFleetArea QScrollBar::handle:vertical {{ background: {c['line']};
                                                           border-radius: 5px;
                                                           min-height: 28px; }}
            #sweepFleetArea QScrollBar::handle:vertical:hover {{ background: {c['accent']}; }}
            #sweepFleetArea QScrollBar::add-line:vertical,
            #sweepFleetArea QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
            #sweepFleetArea QScrollBar::add-page:vertical,
            #sweepFleetArea QScrollBar::sub-page:vertical {{ background: transparent; }}
            #sweepLog {{ color: {c['muted']}; background: {c['panel2']};
                         border: 1px solid {c['line']}; border-radius: 9px;
                         font-family: Consolas, 'Courier New', monospace; font-size: 11px; }}
            QPushButton {{ color: {c['fg']}; background: {c['panel2']};
                           border: 1px solid {c['line']}; border-radius: 9px;
                           padding: 8px 15px; font-size: 12px; font-weight: 700; }}
            QPushButton:hover {{ border-color: {c['accent']}; }}
            QPushButton:disabled {{ color: {c['muted']}; border-color: {c['line']}; }}
            #sweepPrimary {{ color: #09130f; background: {c['accent']}; border: none;
                             font-weight: 800; }}
            #sweepStop {{ color: #e2725b; border-color: #e2725b; }}
            #sweepStop:hover {{ color: #09130f; background: #e2725b; }}
        """)
        self.update()
