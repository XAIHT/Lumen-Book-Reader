# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The Configuration centre: every parameter Lumen has, in one window.

Until now Lumen had no general settings window at all - only the definition
sources dialog.  ``resolve_library_root`` in ``app.py`` read a ``library_root``
key and its docstring called it "a folder the reader chose in Preferences", but
there were no Preferences and nothing ever wrote that key.  So the library root
silently fell through to the installer's registry value or, failing that,
whatever directory Lumen happened to be started in - and "Rescan library"
faithfully swept a folder with no books in it and reported success.

That is the bug this window closes.  The library folder is the first control on
the first tab, it is validated in place, and the count underneath it is measured
from the actual disk rather than promised.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .accel import (
    AUTO,
    EXTRACTION_BACKENDS,
    SEARCH_BACKENDS,
    accelerators,
    capacity_report,
    choose_backends,
    extraction_backend_status,
    probed,
    search_backend_status,
    start_background_probe,
)
from .dialog_layout import ScreenFittingDialog, WheelSafeComboBox, WheelSafeSpinBox
from .library_index import LibraryIndex
from .turbo_scan import (
    MAX_PROCESSES,
    PRIORITY_LABELS,
    PRIORITY_ORDER,
    ScanConfig,
    cpu_topology,
    describe_fleet,
)

#: How long the folder probe is allowed to spend proving a folder has books in
#: it.  It runs on the UI thread every time the path changes, so it is bounded
#: by the clock rather than by the tree - a mis-typed UNC path must not freeze
#: the window while Windows times out on every level of it.
PROBE_SECONDS = 0.45
PROBE_CEILING = 2_000

SEARCH_MODE_LABELS = {
    "meta": "Titles, authors and filenames",
    "content": "Inside the books",
    "all": "Both at once",
}


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            return f"{value:,.0f} {unit}" if unit in {"B", "KB"} else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} PB"


def probe_folder(root: Path, suffixes: set[str]) -> tuple[int, int, bool]:
    """A quick, time-bounded look for books under *root*.

    Returns ``(books, folders, exhausted)`` where *exhausted* means the whole
    tree was covered rather than the clock running out.  This exists so the
    settings window can say "1,284 books found here" the moment a folder is
    chosen, instead of letting a reader save a path and discover it was wrong
    only after a sweep reports nothing.
    """
    deadline = time.monotonic() + PROBE_SECONDS
    stack = [root]
    books = 0
    folders = 0
    while stack:
        if time.monotonic() > deadline or books >= PROBE_CEILING:
            return books, folders, False
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.name.startswith((".", "$")):
                                stack.append(Path(entry.path))
                            continue
                        if os.path.splitext(entry.name)[1].casefold() in suffixes:
                            books += 1
                    except OSError:
                        continue
            folders += 1
        except (OSError, PermissionError):
            continue
    return books, folders, True


# ────────────────────────────── layout helpers ─────────────────────────────


class SettingsPage(QScrollArea):
    """A scrolling tab body that lays its sections out in a column."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsBody")
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(4, 8, 14, 14)
        self.column.setSpacing(14)
        self.setWidget(body)

    def section(self, title: str, description: str = "") -> QVBoxLayout:
        frame = QFrame()
        frame.setObjectName("settingsSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)
        heading = QLabel(title)
        heading.setObjectName("settingsHeading")
        layout.addWidget(heading)
        if description:
            note = QLabel(description)
            note.setObjectName("settingsNote")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.column.addWidget(frame)
        return layout

    def finish(self) -> None:
        self.column.addStretch(1)


def labelled(layout: QVBoxLayout, caption: str, widget: QWidget, hint: str = "") -> QWidget:
    """One captioned control, with an optional line of explanation under it."""
    row = QHBoxLayout()
    row.setSpacing(10)
    label = QLabel(caption)
    label.setObjectName("settingsLabel")
    label.setMinimumWidth(210)
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    row.addWidget(label)
    row.addWidget(widget, 1)
    layout.addLayout(row)
    if hint:
        note = QLabel(hint)
        note.setObjectName("settingsHint")
        note.setWordWrap(True)
        note.setContentsMargins(220, 0, 0, 4)
        layout.addWidget(note)
    return widget


def spin(minimum: int, maximum: int, value: int, suffix: str = "",
         step: int = 1, special: str = "") -> WheelSafeSpinBox:
    box = WheelSafeSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    if special:
        box.setSpecialValueText(special)
    box.setMinimumWidth(150)
    return box


def lines_to_tuple(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.replace(",", "\n").splitlines() if part.strip())


# ──────────────────────────────── the window ───────────────────────────────


class ConfigurationDialog(ScreenFittingDialog):
    """Everything Lumen can be told, in one place."""

    sweep_requested = Signal()
    index_changed = Signal()

    def __init__(self, parent: QWidget, store, library_index: LibraryIndex, library_root: str,
                 colors: dict[str, str] | None = None):
        super().__init__(parent)
        self.store = store
        self.library_index = library_index
        self.original_root = library_root
        self.colors = dict(colors or {})

        self.setWindowTitle("Lumen — Configuration")
        self.setObjectName("configurationDialog")
        self.setMinimumSize(860, 620)
        self.resize(1040, 860)
        self.setSizeGripEnabled(True)

        self.scan_config = ScanConfig.from_mapping(store.data.get("scan"))
        self.search_settings = dict(store.data.get("search") or {})
        self.accel_settings = dict(store.data.get("accel") or {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        title = QLabel("Configuration")
        title.setObjectName("configTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Every parameter Lumen has. The library folder is the first one, "
            "because until it is right nothing else matters."
        )
        subtitle.setObjectName("configSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("configTabs")
        self.tabs.addTab(self._library_page(), "Library")
        self.tabs.addTab(self._engine_page(), "Sweep engine")
        self.tabs.addTab(self._acceleration_page(), "Acceleration && scale")
        self.tabs.addTab(self._index_page(), "Index")
        self.tabs.addTab(self._search_page(), "Search && shelf")
        self.tabs.addTab(self._reading_page(), "Reading")
        layout.addWidget(self.tabs, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save settings")
        self.sweep_button = self.buttons.addButton(
            "Save and sweep now", QDialogButtonBox.ButtonRole.ApplyRole
        )
        self.sweep_button.setObjectName("configSweep")
        self.sweep_button.clicked.connect(self._save_and_sweep)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(320)
        self._probe_timer.timeout.connect(self._probe_root)

        if self.colors:
            self.apply_palette(self.colors)
        self._refresh_fleet_summary()
        self._probe_root()

    # ── tab: library ───────────────────────────────────────────────────────

    def _library_page(self) -> QWidget:
        page = SettingsPage()
        section = page.section(
            "The library folder",
            "The folder Lumen sweeps for books. Sub-folders are included. "
            "This is the setting that had no home before: a scan can only ever "
            "be as right as this path.",
        )

        chooser = QHBoxLayout()
        chooser.setSpacing(8)
        self.root_edit = QLineEdit(self.original_root)
        self.root_edit.setObjectName("rootEdit")
        self.root_edit.setPlaceholderText(r"D:\Books   ·   \\NAS\library   ·   any folder or share")
        self.root_edit.textChanged.connect(lambda _text: self._probe_timer.start())
        chooser.addWidget(self.root_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("configSubtle")
        browse.clicked.connect(self._browse_root)
        chooser.addWidget(browse)
        section.addLayout(chooser)

        self.root_status = QLabel("Checking…")
        self.root_status.setObjectName("rootStatus")
        self.root_status.setWordWrap(True)
        section.addWidget(self.root_status)

        known = [root for root, _count in self.library_index.roots()]
        remembered = [str(item) for item in (self.store.data.get("recent_roots") or [])]
        seen: set[str] = set()
        choices: list[str] = []
        for candidate in [*remembered, *known]:
            key = os.path.normcase(candidate)
            if key not in seen and candidate:
                seen.add(key)
                choices.append(candidate)
        if choices:
            self.recent_combo = WheelSafeComboBox()
            self.recent_combo.addItem("Switch to a library Lumen already knows…")
            for candidate in choices[:24]:
                self.recent_combo.addItem(candidate)
            self.recent_combo.currentIndexChanged.connect(self._pick_recent)
            labelled(section, "Recent libraries", self.recent_combo)

        types = page.section(
            "What counts as a book",
            "Extensions the sweep collects. Lumen can open EPUB and PDF; adding "
            "anything else here will index it but not open it.",
        )
        self.epub_check = QCheckBox("EPUB  (.epub)")
        self.epub_check.setChecked(".epub" in self.scan_config.suffix_set())
        self.pdf_check = QCheckBox("PDF  (.pdf)")
        self.pdf_check.setChecked(".pdf" in self.scan_config.suffix_set())
        types.addWidget(self.epub_check)
        types.addWidget(self.pdf_check)
        extra = sorted(self.scan_config.suffix_set() - {".epub", ".pdf"})
        self.extra_types = QLineEdit(" ".join(extra))
        self.extra_types.setPlaceholderText(".mobi  .djvu  .cbz")
        labelled(types, "Also collect", self.extra_types,
                 "Space-separated extensions. Leave empty for EPUB and PDF only.")

        limits = page.section(
            "What to leave out",
            "Folders and files the walk should never spend time on.",
        )
        self.skip_edit = QPlainTextEdit("\n".join(self.scan_config.skip_directories))
        self.skip_edit.setFixedHeight(96)
        labelled(limits, "Skip these folder names", self.skip_edit,
                 "One per line. Matched by name at any depth, case-insensitively.")
        self.glob_edit = QPlainTextEdit("\n".join(self.scan_config.exclude_globs))
        self.glob_edit.setFixedHeight(66)
        labelled(limits, "Skip paths matching", self.glob_edit,
                 r"One glob per line, matched against the whole path, e.g.  *\backup\*")
        self.depth_spin = spin(0, 512, self.scan_config.max_depth, special="Unlimited")
        labelled(limits, "Maximum depth", self.depth_spin,
                 "How many folder levels below the library root to descend. 0 is unlimited.")
        self.min_size_spin = spin(0, 1_000_000, self.scan_config.min_bytes // 1024, " KB")
        labelled(limits, "Ignore files smaller than", self.min_size_spin,
                 "Useful for shelves littered with zero-byte placeholders. 0 keeps everything.")
        self.max_size_spin = spin(0, 1_000_000, self.scan_config.max_bytes // (1024 * 1024), " MB",
                                  special="No limit")
        labelled(limits, "Ignore files larger than", self.max_size_spin)
        self.symlink_check = QCheckBox(
            "Follow symbolic links and junctions  (loops are detected and skipped)"
        )
        self.symlink_check.setChecked(self.scan_config.follow_symlinks)
        limits.addWidget(self.symlink_check)

        page.finish()
        return page

    def _browse_root(self) -> None:
        start = self.root_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose your library folder", start)
        if chosen:
            self.root_edit.setText(chosen)

    def _pick_recent(self, position: int) -> None:
        if position > 0:
            self.root_edit.setText(self.recent_combo.itemText(position))

    def _probe_root(self) -> None:
        text = self.root_edit.text().strip()
        if not text:
            self.root_status.setText("⚠  No folder chosen. Lumen has nothing to sweep.")
            self.root_status.setProperty("tone", "bad")
            self._restyle(self.root_status)
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            self.root_status.setText(f"⚠  {path} is not a folder Lumen can open.")
            self.root_status.setProperty("tone", "bad")
            self._restyle(self.root_status)
            return

        suffixes = self._chosen_suffixes()
        books, folders, exhausted = probe_folder(path, suffixes)
        if books == 0 and exhausted:
            message = (f"⚠  {path} exists, but there is not a single "
                       f"{' or '.join(sorted(s.lstrip('.').upper() for s in suffixes))} "
                       f"file anywhere under it. A sweep here will find nothing.")
            tone = "bad"
        elif books == 0:
            message = (f"…  Nothing found yet in the first {folders:,} folders "
                       f"(the look-ahead is time-limited). A sweep will search all of it.")
            tone = "warn"
        else:
            counted = f"{books:,}" if exhausted else f"at least {books:,}"
            scope = "in the whole tree" if exhausted else f"in the first {folders:,} folders"
            message = f"✓  {counted} book files found {scope}. This is a real library."
            tone = "good"
        self.root_status.setText(message)
        self.root_status.setProperty("tone", tone)
        self._restyle(self.root_status)

    def _restyle(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _chosen_suffixes(self) -> set[str]:
        suffixes: set[str] = set()
        if self.epub_check.isChecked():
            suffixes.add(".epub")
        if self.pdf_check.isChecked():
            suffixes.add(".pdf")
        for token in self.extra_types.text().replace(",", " ").split():
            token = token.strip().casefold()
            if token:
                suffixes.add(token if token.startswith(".") else f".{token}")
        return suffixes or {".epub", ".pdf"}

    # ── tab: sweep engine ──────────────────────────────────────────────────

    def _engine_page(self) -> QWidget:
        page = SettingsPage()
        physical, logical = cpu_topology()

        fleet = page.section(
            "The extractor fleet",
            f"This machine has {physical} cores and {logical} logical processors. "
            f"Each extractor is a real OS process reading one book at a time; the "
            f"sweep monitor shows every one of them by PID while it works.",
        )
        self.processes_spin = spin(0, MAX_PROCESSES, self.scan_config.processes,
                                   special=f"One per processor ({logical})")
        self.processes_spin.valueChanged.connect(lambda _v: self._refresh_fleet_summary())
        labelled(fleet, "Extractor processes", self.processes_spin,
                 f"0 means one per logical processor. Windows cannot wait on more "
                 f"than {MAX_PROCESSES} at once, so that is the ceiling.")

        self.priority_combo = WheelSafeComboBox()
        for name in PRIORITY_ORDER:
            self.priority_combo.addItem(PRIORITY_LABELS[name], name)
        index = list(PRIORITY_ORDER).index(self.scan_config.priority) \
            if self.scan_config.priority in PRIORITY_ORDER else 4
        self.priority_combo.setCurrentIndex(index)
        self.priority_combo.currentIndexChanged.connect(lambda _i: self._refresh_fleet_summary())
        labelled(fleet, "Process priority", self.priority_combo,
                 "Each worker raises itself the moment it starts. Realtime is offered "
                 "because you asked for maximum, but a realtime fleet can starve the "
                 "desktop of its own input — High is the one to use.")

        self.walkers_spin = spin(0, 256, self.scan_config.walkers,
                                 special=f"Automatic ({self.scan_config.resolved_walkers()})")
        self.walkers_spin.valueChanged.connect(lambda _v: self._refresh_fleet_summary())
        labelled(fleet, "Directory walker threads", self.walkers_spin,
                 "Listing a network folder is mostly waiting, so more walkers is almost "
                 "free and is the single biggest win on a NAS.")

        self.fleet_summary = QLabel("")
        self.fleet_summary.setObjectName("fleetSummary")
        self.fleet_summary.setWordWrap(True)
        fleet.addWidget(self.fleet_summary)

        depth = page.section(
            "How deeply each book is read",
            "Metadata is always read. Indexing the text as well is what makes "
            "'search inside books' possible, and it is most of the cost of a sweep.",
        )
        self.text_check = QCheckBox("Index the text inside each book, not just its metadata")
        self.text_check.setChecked(self.scan_config.with_text)
        self.text_check.toggled.connect(lambda _on: self._refresh_capacity())
        depth.addWidget(self.text_check)
        self.budget_spin = spin(0, 100_000, self.scan_config.text_budget // 1024, " KB", step=25)
        self.budget_spin.valueChanged.connect(lambda _v: self._refresh_capacity())
        labelled(depth, "Text indexed per book", self.budget_spin,
                 "Topic and subject matter are decided in the front matter, so a bounded "
                 "head keeps the index proportional to the shelf rather than to the corpus.")
        self.page_cap_spin = spin(0, 100_000, self.scan_config.pdf_page_cap,
                                  " pages", special="Until the budget runs out")
        labelled(depth, "PDF page ceiling", self.page_cap_spin,
                 "A hard stop for shelves full of thousand-page scans.")

        plumbing = page.section(
            "Pipeline plumbing",
            "The walk, the triage, the fleet and the writer all run at once, "
            "connected by bounded queues. These are the bounds — they set how much "
            "memory a sweep may use, and nothing else in the pipeline grows with "
            "the size of the library.",
        )
        self.walk_queue_spin = spin(0, 10_000_000, self.scan_config.walk_queue_depth,
                                    special="Automatic (20,000)")
        labelled(plumbing, "Walk queue depth", self.walk_queue_spin)
        self.job_queue_spin = spin(0, 1_000_000, self.scan_config.job_queue_depth,
                                   special=f"Automatic ({self.scan_config.resolved_job_queue()})")
        labelled(plumbing, "Fleet queue depth", self.job_queue_spin)
        self.triage_spin = spin(16, 100_000, self.scan_config.triage_batch, step=64)
        labelled(plumbing, "Triage batch", self.triage_spin,
                 "How many paths are checked against the index in one query. A partly "
                 "filled batch is dispatched after 150 ms regardless, so a slow share "
                 "never leaves the fleet idle waiting for the batch to fill.")
        self.write_spin = spin(16, 100_000, self.scan_config.write_batch, step=50)
        labelled(plumbing, "Write batch", self.write_spin,
                 "Rows committed per transaction.")

        behaviour = page.section("When Lumen sweeps")
        self.startup_check = QCheckBox("Sweep automatically when the shelf is empty at startup")
        self.startup_check.setChecked(self.scan_config.scan_on_startup)
        behaviour.addWidget(self.startup_check)
        self.prune_check = QCheckBox(
            "Remove books from the index when they are no longer on disk"
        )
        self.prune_check.setChecked(self.scan_config.prune_missing)
        behaviour.addWidget(self.prune_check)

        page.finish()
        return page

    def _refresh_fleet_summary(self) -> None:
        if not hasattr(self, "fleet_summary"):
            return
        preview = ScanConfig(
            processes=self.processes_spin.value(),
            priority=self.priority_combo.currentData() or "high",
            walkers=self.walkers_spin.value(),
        )
        self.fleet_summary.setText("Pressing Sweep will launch:  " + describe_fleet(preview))

    # ── tab: acceleration ──────────────────────────────────────────────────

    def _acceleration_page(self) -> QWidget:
        page = SettingsPage()

        self.hardware_section = page.section(
            "What this machine has",
            "Detected on this machine, not assumed. If a GPU or the DirectStorage "
            "runtime is missing, it says so and why — and Lumen carries on without "
            "it. There is one build, and it adapts.",
        )
        self.hardware_placeholder = QLabel("Detecting this machine…")
        self.hardware_placeholder.setObjectName("settingsHint")
        self.hardware_section.addWidget(self.hardware_placeholder)
        self._hardware_rows: list[QWidget] = []

        backends = page.section(
            "Backends",
            "Extraction and search are replaceable stages rather than inlined "
            "code, so the GPU path switches on without a second version of Lumen. "
            "Leave both on Automatic: the same build then uses the GPU on a "
            "machine that has one and the CPU fleet on a machine that does not.",
        )
        self.extraction_combo = WheelSafeComboBox()
        for key, label in EXTRACTION_BACKENDS.items():
            self.extraction_combo.addItem(label, key)
        self._select_data(self.extraction_combo, self.accel_settings.get("extraction", AUTO))
        self.extraction_combo.currentIndexChanged.connect(lambda _i: self._refresh_backends())
        labelled(backends, "Extraction", self.extraction_combo)
        self.extraction_status = QLabel("")
        self.extraction_status.setObjectName("settingsHint")
        self.extraction_status.setWordWrap(True)
        backends.addWidget(self.extraction_status)

        self.search_combo = WheelSafeComboBox()
        for key, label in SEARCH_BACKENDS.items():
            self.search_combo.addItem(label, key)
        self._select_data(self.search_combo, self.accel_settings.get("search", AUTO))
        self.search_combo.currentIndexChanged.connect(lambda _i: self._refresh_backends())
        labelled(backends, "Search", self.search_combo)
        self.search_status = QLabel("")
        self.search_status.setObjectName("settingsHint")
        self.search_status.setWordWrap(True)
        backends.addWidget(self.search_status)

        self.resolved_label = QLabel("")
        self.resolved_label.setObjectName("fleetSummary")
        self.resolved_label.setWordWrap(True)
        backends.addWidget(self.resolved_label)

        scale = page.section(
            "Scale",
            "The index is addressed through a shard function from the start, so "
            "growing past one database file is a setting rather than a migration. "
            "Every shard is an independent file with its own full-text index: they "
            "can live on different disks, different NAS volumes or different "
            "machines, and they are swept and searched in parallel.",
        )
        self.shard_spin = spin(1, 100_000, int(self.accel_settings.get("shards", 1) or 1))
        self.shard_spin.valueChanged.connect(lambda _v: self._refresh_capacity())
        labelled(scale, "Index shards", self.shard_spin,
                 "1 is today's single database and is right for one reader's shelf.")
        self.capacity_label = QLabel("")
        self.capacity_label.setObjectName("capacityLabel")
        self.capacity_label.setWordWrap(True)
        scale.addWidget(self.capacity_label)

        # Detection shells out to nvidia-smi and to PowerShell, which costs
        # seconds.  It runs in the background and this fills in when it lands, so
        # opening the window is instant on every machine - most of all on one
        # with no GPU stack at all, where the probe is pure waiting.
        start_background_probe()
        self._hardware_timer = QTimer(self)
        self._hardware_timer.setInterval(300)
        self._hardware_timer.timeout.connect(self._fill_hardware)
        self._hardware_timer.start()
        self._fill_hardware()

        self._refresh_backends()
        self._refresh_capacity()
        page.finish()
        return page

    def _fill_hardware(self) -> None:
        """Show the detected hardware once the background probe has answered."""
        if not probed():
            return
        self._hardware_timer.stop()
        self.hardware_placeholder.hide()
        for accelerator in accelerators():
            row = QFrame()
            row.setObjectName("accelRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(11, 8, 11, 8)
            row_layout.setSpacing(11)
            badge = QLabel(accelerator.badge)
            badge.setObjectName("accelBadgeGood" if accelerator.available else "accelBadgeBad")
            badge.setFixedWidth(64)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(badge)
            text = QLabel(f"<b>{accelerator.name}</b><br>{accelerator.detail}")
            text.setObjectName("accelText")
            text.setWordWrap(True)
            row_layout.addWidget(text, 1)
            self.hardware_section.addWidget(row)
            self._hardware_rows.append(row)
        self._refresh_backends()

    @staticmethod
    def _select_data(combo: WheelSafeComboBox, value: str) -> None:
        position = combo.findData(value)
        combo.setCurrentIndex(position if position >= 0 else 0)

    def _refresh_backends(self) -> None:
        extraction = self.extraction_combo.currentData() or AUTO
        search = self.search_combo.currentData() or AUTO
        ok, why = extraction_backend_status(extraction)
        self.extraction_status.setText(("✓  " if ok else "○  ") + why)
        ok, why = search_backend_status(search)
        self.search_status.setText(("✓  " if ok else "○  ") + why)

        choice = choose_backends(extraction, search)
        self.resolved_label.setText(
            f"On this machine that resolves to:  {EXTRACTION_BACKENDS[choice.extraction]}  "
            f"and  {SEARCH_BACKENDS[choice.search]}.\n{choice.reason}"
        )

    def _refresh_capacity(self) -> None:
        # The engine tab is built before this one, and its controls drive this
        # label, so the first few signals arrive before the label exists.
        if not hasattr(self, "capacity_label"):
            return
        report = capacity_report(
            self.shard_spin.value(),
            text_budget=max(0, self.budget_spin.value() * 1024),
            with_text=self.text_check.isChecked(),
        )
        self.capacity_label.setText(report.summary())

    # ── tab: index ─────────────────────────────────────────────────────────

    def _index_page(self) -> QWidget:
        page = SettingsPage()
        section = page.section(
            "The index",
            "A rebuildable cache, kept with Lumen's own state rather than inside "
            "your library, which may well be read-only or synced.",
        )
        path_label = QLabel(str(self.library_index.path))
        path_label.setObjectName("settingsMono")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        labelled(section, "Database", path_label)

        self.index_facts = QLabel("")
        self.index_facts.setObjectName("settingsHint")
        self.index_facts.setWordWrap(True)
        section.addWidget(self.index_facts)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        optimise = QPushButton("Optimise and compact")
        optimise.setObjectName("configSubtle")
        optimise.clicked.connect(self._optimise_index)
        actions.addWidget(optimise)
        clear = QPushButton("Forget this library")
        clear.setObjectName("configDanger")
        clear.clicked.connect(self._clear_index)
        actions.addWidget(clear)
        actions.addStretch(1)
        section.addLayout(actions)

        libraries = page.section(
            "Libraries in this index",
            "Every folder Lumen has ever swept, with the number of books it holds. "
            "A library whose folder has since been moved or deleted still occupies "
            "the index — and its books still appear on the shelf, where none of "
            "them will open.",
        )
        rows = self.library_index.roots()
        self._dead_roots = [root for root, _count in rows if not Path(root).is_dir()]
        if not rows:
            empty = QLabel("Nothing indexed yet. Choose a library folder and sweep it.")
            empty.setObjectName("settingsHint")
            libraries.addWidget(empty)
        for root, count in rows[:40]:
            gone = root in self._dead_roots
            row = QLabel(("⚠  " if gone else "") + f"<b>{count:,}</b> &nbsp; {root}"
                         + ("  &nbsp;— <i>this folder no longer exists</i>" if gone else ""))
            row.setObjectName("deadRoot" if gone else "settingsMono")
            row.setWordWrap(True)
            libraries.addWidget(row)

        if self._dead_roots:
            dead_count = sum(count for root, count in rows if root in self._dead_roots)
            self.dead_button = QPushButton(
                f"Forget {len(self._dead_roots)} missing "
                f"librar{'y' if len(self._dead_roots) == 1 else 'ies'} "
                f"({dead_count:,} unreachable books)"
            )
            self.dead_button.setObjectName("configDanger")
            self.dead_button.clicked.connect(self._forget_dead_roots)
            libraries.addWidget(self.dead_button)

        self._refresh_index_facts()
        page.finish()
        return page

    def _forget_dead_roots(self) -> None:
        """Drop every library whose folder is gone.

        These rows are pure cost: they cannot be opened, they are most of the
        size of the index, and they are what makes a shelf look full while every
        book on it is a dead path.
        """
        if not self._dead_roots:
            return
        listing = "\n".join(f"  •  {root}" for root in self._dead_roots[:10])
        answer = QMessageBox.question(
            self, "Forget the missing libraries?",
            f"These folders no longer exist, so nothing indexed under them can "
            f"be opened:\n\n{listing}\n\nRemove them from the index?\n\n"
            f"No books are touched — only the catalogue entries pointing at "
            f"folders that are gone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = sum(self.library_index.clear_root(root) for root in self._dead_roots)
        self._dead_roots = []
        self.dead_button.setEnabled(False)
        self.dead_button.setText(f"{removed:,} unreachable books removed")
        self._refresh_index_facts()
        self.index_changed.emit()
        QMessageBox.information(
            self, "Missing libraries forgotten",
            f"{removed:,} unreachable entries were removed.\n\n"
            f"Use “Optimise and compact” to give the disk space back.",
        )

    def _refresh_index_facts(self) -> None:
        counts = self.library_index.counts(self.original_root)
        last = self.library_index.last_scan(self.original_root)
        facts = [
            f"{counts.total:,} books indexed for this library "
            f"({counts.epub:,} EPUB, {counts.pdf:,} PDF, {counts.with_text:,} with full text)",
            f"{human_bytes(counts.bytes_total)} of books  ·  "
            f"{human_bytes(self.library_index.database_bytes())} of index",
        ]
        if last:
            when = time.strftime("%d %b %Y at %H:%M", time.localtime(last["finished_at"]))
            facts.append(
                f"Last sweep {when}: {last['indexed']:,} read, {last['skipped']:,} already "
                f"current, {last['failed']:,} unreadable, in {last['seconds']:,.1f}s"
                + ("  (stopped early)" if last["cancelled"] else "")
            )
        else:
            facts.append("This library has never been swept.")
        self.index_facts.setText("\n".join(facts))

    def _optimise_index(self) -> None:
        try:
            self.library_index.optimize()
        except Exception as exception:
            QMessageBox.warning(self, "Could not optimise",
                                f"{type(exception).__name__}: {exception}")
            return
        self._refresh_index_facts()
        QMessageBox.information(self, "Index optimised",
                                "The full-text indexes were merged and the database compacted.")

    def _clear_index(self) -> None:
        root = self.root_edit.text().strip() or self.original_root
        answer = QMessageBox.question(
            self, "Forget this library?",
            f"Remove every indexed book under\n\n{root}\n\nfrom Lumen's index?\n\n"
            f"Your books are not touched — only the catalogue is cleared, and the "
            f"next sweep rebuilds it from scratch.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.library_index.clear_root(root)
        self._refresh_index_facts()
        self.index_changed.emit()
        QMessageBox.information(self, "Library forgotten",
                                f"{removed:,} indexed books were removed from the catalogue.")

    # ── tab: search ────────────────────────────────────────────────────────

    def _search_page(self) -> QWidget:
        page = SettingsPage()
        section = page.section(
            "Searching",
            "Search is an index lookup, not a scan over your books, so these "
            "settings change how results are shown rather than how long they take.",
        )
        self.mode_combo = WheelSafeComboBox()
        for key, label in SEARCH_MODE_LABELS.items():
            self.mode_combo.addItem(label, key)
        self._select_data(self.mode_combo, str(self.search_settings.get("default_mode", "meta")))
        labelled(section, "Search by default in", self.mode_combo)

        self.debounce_spin = spin(0, 2000, int(self.search_settings.get("debounce_ms", 140)),
                                  " ms", step=10)
        labelled(section, "Wait before searching", self.debounce_spin,
                 "How long after the last keystroke the query runs. Higher means one "
                 "query for a fast typist instead of one per key.")
        self.page_spin = spin(50, 5000, int(self.search_settings.get("page_size", 400)), step=50)
        labelled(section, "Rows fetched per page", self.page_spin,
                 "The shelf never holds more than this, however large the library is.")
        self.snippet_spin = spin(6, 80, int(self.search_settings.get("snippet_words", 18)),
                                 " words")
        labelled(section, "Snippet length", self.snippet_spin,
                 "How much of the matching sentence is shown under a book found by its text.")

        page.finish()
        return page

    # ── tab: reading ───────────────────────────────────────────────────────

    def _reading_page(self) -> QWidget:
        page = SettingsPage()
        section = page.section("Appearance")
        self.theme_combo = WheelSafeComboBox()
        for key, label in (("dark", "Night"), ("light", "Paper"), ("sepia", "Sepia")):
            self.theme_combo.addItem(label, key)
        self._select_data(self.theme_combo, str(self.store.data.get("theme", "dark")))
        labelled(section, "Theme", self.theme_combo)
        self.font_spin = spin(10, 48, int(self.store.data.get("font_size", 20)), " px")
        labelled(section, "Reading text size", self.font_spin)
        self.sidebar_check = QCheckBox("Show the book panel beside the page")
        self.sidebar_check.setChecked(bool(self.store.data.get("sidebar_visible", True)))
        section.addWidget(self.sidebar_check)

        elsewhere = page.section(
            "Settings that live in their own windows",
            "The speed reader and the definition sources have their own dedicated "
            "setup, reachable from the header of the main window: ⚡ Speed and ◇ Definer.",
        )
        elsewhere.addWidget(QLabel(""))

        page.finish()
        return page

    # ── saving ─────────────────────────────────────────────────────────────

    def collected_config(self) -> ScanConfig:
        """The ScanConfig the controls currently describe."""
        config = ScanConfig(
            extensions=tuple(sorted(self._chosen_suffixes())),
            skip_directories=lines_to_tuple(self.skip_edit.toPlainText()),
            exclude_globs=lines_to_tuple(self.glob_edit.toPlainText()),
            max_depth=self.depth_spin.value(),
            follow_symlinks=self.symlink_check.isChecked(),
            min_bytes=self.min_size_spin.value() * 1024,
            max_bytes=self.max_size_spin.value() * 1024 * 1024,
            processes=self.processes_spin.value(),
            priority=self.priority_combo.currentData() or "high",
            walkers=self.walkers_spin.value(),
            walk_queue_depth=self.walk_queue_spin.value(),
            job_queue_depth=self.job_queue_spin.value(),
            triage_batch=self.triage_spin.value(),
            write_batch=self.write_spin.value(),
            with_text=self.text_check.isChecked(),
            text_budget=max(0, self.budget_spin.value() * 1024),
            pdf_page_cap=self.page_cap_spin.value(),
            extraction_backend=self.extraction_combo.currentData() or AUTO,
            scan_on_startup=self.startup_check.isChecked(),
            prune_missing=self.prune_check.isChecked(),
        )
        return config

    @property
    def chosen_root(self) -> str:
        return self.root_edit.text().strip()

    def apply_to_store(self) -> None:
        """Write every control back into reader state.

        ``text_budget`` is deliberately mirrored at the top level as well: it was
        already stored there before this window existed, and other code still
        reads it from that key.
        """
        config = self.collected_config()
        self.store.data["scan"] = config.to_dict()
        self.store.data["text_budget"] = config.text_budget

        root = self.chosen_root
        if root:
            self.store.data["library_root"] = root
            remembered = [str(item) for item in (self.store.data.get("recent_roots") or [])]
            remembered = [item for item in remembered
                          if os.path.normcase(item) != os.path.normcase(root)]
            remembered.insert(0, root)
            self.store.data["recent_roots"] = remembered[:12]

        self.store.data["search"] = {
            "default_mode": self.mode_combo.currentData() or "meta",
            "debounce_ms": self.debounce_spin.value(),
            "page_size": self.page_spin.value(),
            "snippet_words": self.snippet_spin.value(),
        }
        self.store.data["accel"] = {
            "extraction": self.extraction_combo.currentData() or AUTO,
            "search": self.search_combo.currentData() or AUTO,
            "shards": self.shard_spin.value(),
        }
        self.store.data["theme"] = self.theme_combo.currentData() or "dark"
        self.store.data["font_size"] = self.font_spin.value()
        self.store.data["sidebar_visible"] = self.sidebar_check.isChecked()
        self.store.save()

    def accept(self) -> None:
        if not self._confirm_root():
            return
        self.apply_to_store()
        super().accept()

    def _save_and_sweep(self) -> None:
        if not self._confirm_root():
            return
        self.apply_to_store()
        self.sweep_requested.emit()
        super().accept()

    def _confirm_root(self) -> bool:
        """Refuse to save a folder that does not exist; warn about an empty one."""
        root = self.chosen_root
        if not root:
            QMessageBox.warning(self, "No library folder",
                                "Choose the folder your books are in before saving.")
            self.tabs.setCurrentIndex(0)
            return False
        if not Path(root).expanduser().is_dir():
            QMessageBox.warning(self, "That folder does not exist",
                                f"Lumen cannot open:\n\n{root}\n\n"
                                f"Check the path, or use Browse to pick it.")
            self.tabs.setCurrentIndex(0)
            return False
        return True

    # ── theming ────────────────────────────────────────────────────────────

    def apply_palette(self, colors: dict[str, str]) -> None:
        self.colors = dict(colors)
        c = colors
        self.setStyleSheet(f"""
            #configurationDialog {{ background: {c['bg']}; }}
            QLabel {{ color: {c['fg']}; }}
            #configTitle {{ color: {c['accent']}; font-size: 22px; font-weight: 850;
                            letter-spacing: 1px; }}
            #configSubtitle {{ color: {c['muted']}; font-size: 12px; }}
            #settingsBody {{ background: transparent; }}
            #settingsSection {{ background: {c['panel']}; border: 1px solid {c['line']};
                                border-radius: 12px; }}
            #settingsHeading {{ color: {c['fg']}; font-size: 14px; font-weight: 800; }}
            #settingsNote {{ color: {c['muted']}; font-size: 11px; }}
            #settingsLabel {{ color: {c['fg']}; font-size: 12px; font-weight: 650; }}
            #settingsHint {{ color: {c['muted']}; font-size: 11px; }}
            #settingsMono {{ color: {c['fg']}; font-family: Consolas, 'Courier New', monospace;
                             font-size: 11px; }}
            #deadRoot {{ color: #e2725b; font-family: Consolas, 'Courier New', monospace;
                         font-size: 11px; }}
            #fleetSummary {{ color: {c['accent']}; background: {c['accent2']};
                             border: 1px solid {c['accent']}; border-radius: 9px;
                             padding: 9px 11px; font-size: 12px; font-weight: 700; }}
            #capacityLabel {{ color: {c['accent']}; font-size: 12px; font-weight: 700; }}
            #rootEdit {{ font-size: 14px; font-weight: 650; padding: 10px 11px; }}
            #rootStatus {{ font-size: 12px; font-weight: 700; padding: 4px 2px; }}
            #rootStatus[tone="good"] {{ color: {c['accent']}; }}
            #rootStatus[tone="warn"] {{ color: {c['muted']}; }}
            #rootStatus[tone="bad"]  {{ color: #e2725b; }}
            #accelRow {{ background: {c['panel2']}; border: 1px solid {c['line']};
                         border-radius: 9px; }}
            #accelText {{ color: {c['fg']}; font-size: 11px; }}
            #accelBadgeGood {{ color: #09130f; background: {c['accent']}; border-radius: 6px;
                               padding: 3px 0; font-size: 9px; font-weight: 850;
                               letter-spacing: 1px; }}
            #accelBadgeBad {{ color: {c['muted']}; background: {c['panel']};
                              border: 1px solid {c['line']}; border-radius: 6px; padding: 3px 0;
                              font-size: 9px; font-weight: 850; letter-spacing: 1px; }}
            QTabWidget::pane {{ border: 1px solid {c['line']}; border-radius: 10px;
                                background: {c['bg']}; }}
            QTabBar::tab {{ color: {c['muted']}; background: transparent; padding: 9px 16px;
                            border-bottom: 2px solid transparent; font-weight: 700;
                            font-size: 12px; }}
            QTabBar::tab:selected {{ color: {c['accent']}; border-bottom-color: {c['accent']}; }}
            QLineEdit, QPlainTextEdit {{ color: {c['fg']}; background: {c['panel2']};
                                         border: 1px solid {c['line']}; border-radius: 8px;
                                         padding: 8px 9px; selection-background-color: {c['accent']}; }}
            QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
            QSpinBox, QComboBox {{ color: {c['fg']}; background: {c['panel2']};
                                   border: 1px solid {c['line']}; border-radius: 8px;
                                   padding: 7px 9px; }}
            QComboBox QAbstractItemView {{ color: {c['fg']}; background: {c['panel2']};
                                           border: 1px solid {c['line']};
                                           selection-background-color: {c['accent2']}; }}
            QCheckBox {{ color: {c['fg']}; font-size: 12px; spacing: 8px; }}
            QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 4px;
                                    border: 1px solid {c['line']}; background: {c['panel2']}; }}
            QCheckBox::indicator:checked {{ background: {c['accent']};
                                            border-color: {c['accent']}; }}
            QPushButton {{ color: {c['fg']}; background: {c['panel2']};
                           border: 1px solid {c['line']}; border-radius: 9px;
                           padding: 8px 15px; font-size: 12px; font-weight: 700; }}
            QPushButton:hover {{ border-color: {c['accent']}; }}
            #configSweep {{ color: #09130f; background: {c['accent']}; border: none;
                            font-weight: 850; }}
            #configDanger {{ color: #e2725b; border-color: #e2725b; }}
            #configDanger:hover {{ color: #09130f; background: #e2725b; }}
            QScrollArea {{ background: transparent; border: none; }}
        """)
