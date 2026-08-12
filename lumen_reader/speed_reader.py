"""Format-neutral rapid serial visual presentation (RSVP) reading mode."""

from __future__ import annotations

import bisect
import math
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

from PySide6.QtCore import QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


WORD_RE = re.compile(r"\S+", re.UNICODE)
SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'”’»)\]}]*$", re.UNICODE)
CLAUSE_END_RE = re.compile(r"[,;:][\"'”’»)\]}]*$", re.UNICODE)


@dataclass(slots=True)
class SpeedReaderSettings:
    """Persisted RSVP preferences with conservative, usable defaults."""

    wpm: int = 300
    chunk_size: int = 1
    font_family: str = "Segoe UI"
    font_size: int = 68
    background_color: str = "#050709"
    text_color: str = "#76ffb2"
    focus_color: str = "#ffd166"
    show_focus_letter: bool = True
    show_fixation_guides: bool = True
    blank_percent: int = 12
    punctuation_pauses: bool = True
    clause_pause_factor: float = 1.35
    sentence_pause_factor: float = 1.85
    long_word_extra_ms: int = 12
    countdown_seconds: int = 3
    rest_interval_minutes: int = 10
    fullscreen: bool = True
    minimal_chrome: bool = True

    @classmethod
    def from_mapping(cls, data: Any) -> "SpeedReaderSettings":
        values = data if isinstance(data, dict) else {}
        defaults = cls()

        def integer(name: str, low: int, high: int) -> int:
            try:
                value = int(values.get(name, getattr(defaults, name)))
            except (TypeError, ValueError):
                value = int(getattr(defaults, name))
            return min(max(value, low), high)

        def decimal(name: str, low: float, high: float) -> float:
            try:
                value = float(values.get(name, getattr(defaults, name)))
            except (TypeError, ValueError):
                value = float(getattr(defaults, name))
            return min(max(value, low), high)

        def flag(name: str) -> bool:
            value = values.get(name, getattr(defaults, name))
            return value if isinstance(value, bool) else bool(getattr(defaults, name))

        def color(name: str) -> str:
            candidate = QColor(str(values.get(name, getattr(defaults, name))))
            return candidate.name() if candidate.isValid() else str(getattr(defaults, name))

        return cls(
            wpm=integer("wpm", 80, 1200),
            chunk_size=integer("chunk_size", 1, 5),
            font_family=str(values.get("font_family") or defaults.font_family),
            font_size=integer("font_size", 28, 144),
            background_color=color("background_color"),
            text_color=color("text_color"),
            focus_color=color("focus_color"),
            show_focus_letter=flag("show_focus_letter"),
            show_fixation_guides=flag("show_fixation_guides"),
            blank_percent=integer("blank_percent", 0, 40),
            punctuation_pauses=flag("punctuation_pauses"),
            clause_pause_factor=decimal("clause_pause_factor", 1.0, 3.0),
            sentence_pause_factor=decimal("sentence_pause_factor", 1.0, 4.0),
            long_word_extra_ms=integer("long_word_extra_ms", 0, 60),
            countdown_seconds=integer("countdown_seconds", 0, 10),
            rest_interval_minutes=integer("rest_interval_minutes", 0, 60),
            fullscreen=flag("fullscreen"),
            minimal_chrome=flag("minimal_chrome"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_SPEED_READER_SETTINGS = SpeedReaderSettings().to_dict()


@dataclass(slots=True)
class SpeedChapter:
    title: str
    words: list[str]


@dataclass(slots=True)
class SpeedUnit:
    text: str
    words: tuple[str, ...]
    chapter_index: int
    word_index: int
    global_index: int

    @property
    def word_count(self) -> int:
        return len(self.words)


class SpeedReadingDocument:
    """Plain-text book representation shared by EPUB and PDF adapters."""

    def __init__(self, chapters: Iterable[SpeedChapter]):
        self.chapters = list(chapters)
        self.offsets: list[int] = []
        total = 0
        for chapter in self.chapters:
            self.offsets.append(total)
            total += len(chapter.words)
        self.total_words = total

    @classmethod
    def from_book(cls, book: Any) -> "SpeedReadingDocument":
        chapters = []
        for index, chapter in enumerate(book.chapters):
            text = book.text_for_chapter(index)
            chapters.append(SpeedChapter(chapter.title, tokenize_text(text)))
        return cls(chapters)

    def locate_global(self, global_index: int) -> tuple[int, int]:
        if not self.chapters:
            return 0, 0
        target = min(max(int(global_index), 0), self.total_words)
        if target == self.total_words:
            last = len(self.chapters) - 1
            return last, len(self.chapters[last].words)
        chapter_index = max(0, bisect.bisect_right(self.offsets, target) - 1)
        while (
            chapter_index + 1 < len(self.chapters)
            and target >= self.offsets[chapter_index] + len(self.chapters[chapter_index].words)
        ):
            chapter_index += 1
        return chapter_index, max(0, target - self.offsets[chapter_index])


class SpeedReadingCursor:
    """Seekable cursor that never joins a chunk across a sentence or chapter."""

    def __init__(
        self,
        document: SpeedReadingDocument,
        chapter_index: int = 0,
        chapter_scroll: float = 0.0,
    ):
        self.document = document
        self.chapter_index = min(max(chapter_index, 0), max(len(document.chapters) - 1, 0))
        words = document.chapters[self.chapter_index].words if document.chapters else []
        self.word_index = min(max(round(len(words) * chapter_scroll), 0), len(words))
        self._skip_empty_forward()

    @property
    def global_index(self) -> int:
        if not self.document.chapters:
            return 0
        return self.document.offsets[self.chapter_index] + self.word_index

    def _skip_empty_forward(self) -> None:
        while self.chapter_index < len(self.document.chapters):
            words = self.document.chapters[self.chapter_index].words
            if self.word_index < len(words):
                return
            if self.chapter_index + 1 >= len(self.document.chapters):
                self.word_index = len(words)
                return
            self.chapter_index += 1
            self.word_index = 0

    def consume(self, chunk_size: int) -> SpeedUnit | None:
        self._skip_empty_forward()
        if not self.document.chapters:
            return None
        chapter = self.document.chapters[self.chapter_index]
        if self.word_index >= len(chapter.words):
            return None
        start = self.word_index
        selected: list[str] = []
        while self.word_index < len(chapter.words) and len(selected) < max(1, chunk_size):
            word = chapter.words[self.word_index]
            selected.append(word)
            self.word_index += 1
            if SENTENCE_END_RE.search(word):
                break
        return SpeedUnit(
            text=" ".join(selected),
            words=tuple(selected),
            chapter_index=self.chapter_index,
            word_index=start,
            global_index=self.document.offsets[self.chapter_index] + start,
        )

    def seek_global(self, global_index: int) -> None:
        self.chapter_index, self.word_index = self.document.locate_global(global_index)
        self._skip_empty_forward()

    def seek_relative(self, word_delta: int) -> None:
        self.seek_global(self.global_index + int(word_delta))

    def chapter_scroll(self, chapter_index: int | None = None, word_index: int | None = None) -> float:
        if not self.document.chapters:
            return 0.0
        index = self.chapter_index if chapter_index is None else chapter_index
        words = self.document.chapters[index].words
        position = self.word_index if word_index is None else word_index
        return min(max(position / max(len(words), 1), 0.0), 1.0)


def tokenize_text(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def optimal_recognition_index(word: str) -> int:
    """Return a pragmatic ORP/focal character offset for a displayed word."""
    length = len(word)
    if length <= 1:
        return 0
    if length <= 5:
        return 1
    if length <= 9:
        return 2
    if length <= 13:
        return 3
    return 4


def presentation_timing(unit: SpeedUnit, settings: SpeedReaderSettings) -> tuple[int, int]:
    """Return visible and blank milliseconds for one chunk at nominal WPM."""
    total = (60_000.0 / settings.wpm) * unit.word_count
    longest = max((len(re.sub(r"\W", "", word, flags=re.UNICODE)) for word in unit.words), default=0)
    total += max(0, longest - 8) * settings.long_word_extra_ms
    if settings.punctuation_pauses and unit.words:
        last = unit.words[-1]
        if SENTENCE_END_RE.search(last):
            total *= settings.sentence_pause_factor
        elif CLAUSE_END_RE.search(last):
            total *= settings.clause_pause_factor
    total = max(total, 40.0)
    blank = round(total * settings.blank_percent / 100.0)
    visible = max(30, round(total) - blank)
    return visible, max(0, blank)


def contrast_ratio(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        color = QColor(value)
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ColorButton(QPushButton):
    color_changed = Signal(str)

    def __init__(self, color: str, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color).name()
        self.setText(self._color.upper())
        self.setAccessibleName(label)
        self.clicked.connect(self._choose_color)
        self._refresh()

    @property
    def color(self) -> str:
        return self._color

    def _choose_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, self.accessibleName())
        if not chosen.isValid():
            return
        self._color = chosen.name()
        self.setText(self._color.upper())
        self._refresh()
        self.color_changed.emit(self._color)

    def _refresh(self) -> None:
        color = QColor(self._color)
        foreground = "#08100d" if color.lightnessF() > 0.55 else "#f4f7f5"
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; color: {foreground}; "
            "border: 1px solid rgba(255,255,255,.24); border-radius: 7px; padding: 7px 12px; }}"
        )


class SpeedWordDisplay(QWidget):
    clicked = Signal()

    def __init__(self, settings: SpeedReaderSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.text = ""
        self.setMinimumHeight(180)
        self.setAccessibleName("Rapid serial word display")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_settings(self, settings: SpeedReaderSettings) -> None:
        self.settings = settings
        self.update()

    def set_text(self, text: str) -> None:
        self.text = text
        self.setAccessibleDescription(text)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        del event
        if not self.text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = QFont(self.settings.font_family, self.settings.font_size)
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        maximum = self.width() * 0.88
        if metrics.horizontalAdvance(self.text) > maximum:
            scaled = max(22, math.floor(self.settings.font_size * maximum / metrics.horizontalAdvance(self.text)))
            font.setPointSize(scaled)
            metrics = QFontMetrics(font)
        painter.setFont(font)
        baseline = (self.height() + metrics.ascent() - metrics.descent()) / 2
        text_color = QColor(self.settings.text_color)
        focus_color = QColor(self.settings.focus_color)

        if not self.settings.show_focus_letter:
            painter.setPen(text_color)
            painter.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignCenter, self.text)
            return

        first = WORD_RE.search(self.text)
        anchor = (first.start() + optimal_recognition_index(first.group(0))) if first else 0
        anchor = min(max(anchor, 0), len(self.text) - 1)
        prefix, focal, suffix = self.text[:anchor], self.text[anchor], self.text[anchor + 1 :]
        focal_width = metrics.horizontalAdvance(focal)
        focal_left = self.width() / 2 - focal_width / 2
        painter.setPen(text_color)
        painter.drawText(QRectF(focal_left - metrics.horizontalAdvance(prefix), 0, metrics.horizontalAdvance(prefix), baseline + metrics.descent()), Qt.AlignmentFlag.AlignBottom, prefix)
        painter.setPen(focus_color)
        painter.drawText(QRectF(focal_left, 0, focal_width + 2, baseline + metrics.descent()), Qt.AlignmentFlag.AlignBottom, focal)
        painter.setPen(text_color)
        painter.drawText(QRectF(focal_left + focal_width, 0, metrics.horizontalAdvance(suffix) + 2, baseline + metrics.descent()), Qt.AlignmentFlag.AlignBottom, suffix)

        if self.settings.show_fixation_guides:
            guide = QPen(focus_color)
            guide.setWidth(2)
            painter.setPen(guide)
            center = round(self.width() / 2)
            top = round(baseline - metrics.ascent() - 24)
            bottom = round(baseline + metrics.descent() + 14)
            painter.drawLine(center, top, center, top + 12)
            painter.drawLine(center, bottom, center, bottom + 12)


class SpeedReaderSettingsDialog(QDialog):
    """Settings-first launch dialog with a live RSVP appearance preview."""

    def __init__(self, settings: SpeedReaderSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Speed Reader Studio")
        self.setMinimumSize(690, 650)
        self.resize(760, 720)
        self._initial = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        heading = QLabel("⚡  Speed Reader Studio")
        heading.setObjectName("speedSettingsHeading")
        intro = QLabel(
            "Tune a fixed-focus RSVP stream for this session. Start conservatively, pause whenever "
            "meaning becomes unclear, and use normal page reading for close analysis."
        )
        intro.setWordWrap(True)
        intro.setObjectName("speedSettingsIntro")
        root.addWidget(heading)
        root.addWidget(intro)

        self.preview = SpeedWordDisplay(settings)
        self.preview.setObjectName("speedPreview")
        self.preview.setFixedHeight(150)
        self.preview.set_text("Luminous reading")
        root.addWidget(self.preview)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        pace_page = QWidget()
        pace_form = QFormLayout(pace_page)
        pace_form.setContentsMargins(18, 18, 18, 18)
        pace_form.setSpacing(12)
        self.wpm = self._spin(settings.wpm, 80, 1200, " WPM")
        self.chunk_size = self._spin(settings.chunk_size, 1, 5, " words")
        self.blank_percent = self._spin(settings.blank_percent, 0, 40, "%")
        self.long_word_extra = self._spin(settings.long_word_extra_ms, 0, 60, " ms/character")
        self.punctuation = QCheckBox("Pause naturally at punctuation")
        self.punctuation.setChecked(settings.punctuation_pauses)
        self.clause_factor = self._decimal(settings.clause_pause_factor, 1.0, 3.0)
        self.sentence_factor = self._decimal(settings.sentence_pause_factor, 1.0, 4.0)
        self.countdown = self._spin(settings.countdown_seconds, 0, 10, " seconds")
        self.rest_interval = self._spin(settings.rest_interval_minutes, 0, 60, " minutes (0 = off)")
        pace_form.addRow("Nominal speed", self.wpm)
        pace_form.addRow("Words per fixation", self.chunk_size)
        pace_form.addRow("Dark interval", self.blank_percent)
        pace_form.addRow("Long-word allowance", self.long_word_extra)
        pace_form.addRow("Rhythm", self.punctuation)
        pace_form.addRow("Comma / clause pause", self.clause_factor)
        pace_form.addRow("Sentence pause", self.sentence_factor)
        pace_form.addRow("Start countdown", self.countdown)
        pace_form.addRow("Eye-rest reminder", self.rest_interval)
        tabs.addTab(pace_page, "Pace & rhythm")

        appearance_page = QWidget()
        appearance_form = QFormLayout(appearance_page)
        appearance_form.setContentsMargins(18, 18, 18, 18)
        appearance_form.setSpacing(12)
        self.font_family = QFontComboBox()
        self.font_family.setCurrentFont(QFont(settings.font_family))
        self.font_size = self._spin(settings.font_size, 28, 144, " pt")
        self.background_color = ColorButton(settings.background_color, "Choose background color")
        self.text_color = ColorButton(settings.text_color, "Choose word color")
        self.focus_color = ColorButton(settings.focus_color, "Choose focal-letter color")
        self.focus_letter = QCheckBox("Highlight one stable focal letter")
        self.focus_letter.setChecked(settings.show_focus_letter)
        self.fixation_guides = QCheckBox("Show fixation guide marks")
        self.fixation_guides.setChecked(settings.show_fixation_guides)
        self.fullscreen = QCheckBox("Use the complete screen")
        self.fullscreen.setChecked(settings.fullscreen)
        self.minimal_chrome = QCheckBox("Hide controls while words are playing")
        self.minimal_chrome.setChecked(settings.minimal_chrome)
        appearance_form.addRow("Typeface", self.font_family)
        appearance_form.addRow("Word size", self.font_size)
        appearance_form.addRow("Background", self.background_color)
        appearance_form.addRow("Word color", self.text_color)
        appearance_form.addRow("Focal color", self.focus_color)
        appearance_form.addRow("Recognition point", self.focus_letter)
        appearance_form.addRow("Eye position", self.fixation_guides)
        appearance_form.addRow("Immersion", self.fullscreen)
        appearance_form.addRow("Minimal view", self.minimal_chrome)
        self.contrast_label = QLabel()
        appearance_form.addRow("Word contrast", self.contrast_label)
        tabs.addTab(appearance_page, "Appearance & focus")

        caution = QLabel(
            "Comfort note: rapidly changing, high-contrast text can cause fatigue or afterimages. "
            "Stop if you feel strain, headache, nausea, visual disturbance, or loss of comprehension. "
            "This feature is a reading aid, not a medical or memory treatment."
        )
        caution.setWordWrap(True)
        caution.setObjectName("speedCaution")
        root.addWidget(caution)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start speed reading")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("speedStartButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for control in (
            self.wpm,
            self.chunk_size,
            self.blank_percent,
            self.font_size,
            self.font_family,
            self.focus_letter,
            self.fixation_guides,
        ):
            signal = getattr(control, "valueChanged", None) or getattr(control, "currentFontChanged", None) or getattr(control, "toggled", None)
            signal.connect(self._refresh_preview)
        for button in (self.background_color, self.text_color, self.focus_color):
            button.color_changed.connect(self._refresh_preview)
        self._refresh_preview()
        self.setStyleSheet(self._style())

    @staticmethod
    def _spin(value: int, low: int, high: int, suffix: str = "") -> QSpinBox:
        control = QSpinBox()
        control.setRange(low, high)
        control.setValue(value)
        control.setSuffix(suffix)
        control.setKeyboardTracking(False)
        return control

    @staticmethod
    def _decimal(value: float, low: float, high: float) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(low, high)
        control.setSingleStep(0.05)
        control.setDecimals(2)
        control.setValue(value)
        control.setSuffix("×")
        return control

    @property
    def settings(self) -> SpeedReaderSettings:
        return SpeedReaderSettings(
            wpm=self.wpm.value(),
            chunk_size=self.chunk_size.value(),
            font_family=self.font_family.currentFont().family(),
            font_size=self.font_size.value(),
            background_color=self.background_color.color,
            text_color=self.text_color.color,
            focus_color=self.focus_color.color,
            show_focus_letter=self.focus_letter.isChecked(),
            show_fixation_guides=self.fixation_guides.isChecked(),
            blank_percent=self.blank_percent.value(),
            punctuation_pauses=self.punctuation.isChecked(),
            clause_pause_factor=self.clause_factor.value(),
            sentence_pause_factor=self.sentence_factor.value(),
            long_word_extra_ms=self.long_word_extra.value(),
            countdown_seconds=self.countdown.value(),
            rest_interval_minutes=self.rest_interval.value(),
            fullscreen=self.fullscreen.isChecked(),
            minimal_chrome=self.minimal_chrome.isChecked(),
        )

    def _refresh_preview(self, *_: Any) -> None:
        settings = self.settings
        self.preview.setStyleSheet(f"background: {settings.background_color}; border-radius: 10px;")
        self.preview.set_settings(settings)
        ratio = contrast_ratio(settings.text_color, settings.background_color)
        target = 3.0  # Preview text is always large text under WCAG terminology.
        verdict = "strong" if ratio >= 7 else "good" if ratio >= target else "low"
        color = "#76ffb2" if ratio >= target else "#ff8b8b"
        self.contrast_label.setText(f"<span style='color:{color}'>{ratio:.1f}:1 · {verdict}</span>")

    @staticmethod
    def _style() -> str:
        return """
            QDialog { background: #0c1017; color: #eef2ef; }
            QWidget { font-family: 'Segoe UI'; font-size: 13px; }
            #speedSettingsHeading { color: #76ffb2; font-size: 24px; font-weight: 750; }
            #speedSettingsIntro { color: #a1aaba; font-size: 13px; }
            #speedCaution { color: #c5a873; background: #18160f; border: 1px solid #403821; border-radius: 8px; padding: 10px; }
            QTabWidget::pane { border: 1px solid #283142; border-radius: 9px; background: #111722; }
            QTabBar::tab { color: #8994a7; padding: 10px 16px; }
            QTabBar::tab:selected { color: #76ffb2; border-bottom: 2px solid #76ffb2; }
            QSpinBox, QDoubleSpinBox, QFontComboBox { color: #eef2ef; background: #171e2a; border: 1px solid #30394a; border-radius: 7px; padding: 7px 9px; min-width: 180px; }
            QCheckBox { color: #eef2ef; spacing: 8px; padding: 5px 0; }
            QPushButton { color: #eef2ef; background: #171e2a; border: 1px solid #30394a; border-radius: 7px; padding: 8px 12px; }
            QPushButton:hover { border-color: #76ffb2; }
            #speedStartButton { color: #07110d; background: #76ffb2; border: none; font-weight: 700; padding: 9px 17px; }
        """


class SpeedReaderDialog(QDialog):
    """Immersive, keyboard-first RSVP player over a complete book document."""

    def __init__(
        self,
        document: SpeedReadingDocument,
        settings: SpeedReaderSettings,
        book_title: str,
        start_chapter: int,
        start_scroll: float,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.document = document
        self.settings = settings
        self.cursor = SpeedReadingCursor(document, start_chapter, start_scroll)
        self.current_unit: SpeedUnit | None = None
        self.playing = False
        self.stage = "idle"
        self.countdown_remaining = settings.countdown_seconds
        self.next_break_at = 0.0
        self.in_rest_break = False
        self._resume_after_seek = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timer_fired)
        self.setWindowTitle(f"Speed Reader — {book_title}")
        self.setModal(True)
        self.setMinimumSize(850, 560)
        self.resize(1180, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(10)
        self.header = QFrame()
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(f"⚡  {book_title}")
        title.setObjectName("speedBookTitle")
        self.chapter_label = QLabel()
        self.chapter_label.setObjectName("speedChapter")
        self.wpm_label = QLabel()
        self.wpm_label.setObjectName("speedBadge")
        close_button = QPushButton("Close  Esc")
        close_button.clicked.connect(self.accept)
        header_layout.addWidget(title)
        header_layout.addWidget(self.chapter_label, 1)
        header_layout.addWidget(self.wpm_label)
        header_layout.addWidget(close_button)
        root.addWidget(self.header)

        root.addStretch(1)
        self.message = QLabel()
        self.message.setObjectName("speedMessage")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.display = SpeedWordDisplay(settings)
        self.display.clicked.connect(self.toggle_playback)
        root.addWidget(self.display, 2)
        root.addStretch(1)

        self.footer = QFrame()
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(0, 0, 0, 0)
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, max(document.total_words, 1))
        self.progress.sliderPressed.connect(self._begin_seek)
        self.progress.sliderReleased.connect(self._finish_seek)
        footer.addWidget(self.progress)
        controls = QHBoxLayout()
        self.position_label = QLabel()
        self.position_label.setObjectName("speedPosition")
        rewind = QPushButton("↶  10 sec")
        rewind.clicked.connect(lambda: self.jump_words(-round(self.settings.wpm / 6)))
        slower = QPushButton("−  25 WPM")
        slower.clicked.connect(lambda: self.adjust_wpm(-25))
        self.play_button = QPushButton("▶  Play")
        self.play_button.setObjectName("speedPlay")
        self.play_button.clicked.connect(self.toggle_playback)
        faster = QPushButton("+  25 WPM")
        faster.clicked.connect(lambda: self.adjust_wpm(25))
        forward = QPushButton("10 sec  ↷")
        forward.clicked.connect(lambda: self.jump_words(round(self.settings.wpm / 6)))
        controls.addWidget(self.position_label)
        controls.addStretch(1)
        controls.addWidget(rewind)
        controls.addWidget(slower)
        controls.addWidget(self.play_button)
        controls.addWidget(faster)
        controls.addWidget(forward)
        footer.addLayout(controls)
        root.addWidget(self.footer)

        self._apply_style()
        self._update_labels()

    def start_session(self) -> None:
        if not self.document.total_words:
            self._complete("This book has no extractable text for speed reading.")
            return
        if self.settings.countdown_seconds:
            self.stage = "countdown"
            self.message.setText(f"{self.countdown_remaining}\n\nSpace pauses · ← rewinds · ↑/↓ changes WPM · Esc closes")
            self.display.set_text("")
            self.timer.start(1000)
        else:
            self._begin_playback()

    def _begin_playback(self) -> None:
        self.playing = True
        self.in_rest_break = False
        self.message.clear()
        self.play_button.setText("❚❚  Pause")
        if self.settings.rest_interval_minutes:
            self.next_break_at = time.monotonic() + self.settings.rest_interval_minutes * 60
        self._set_chrome_for_playback()
        self._show_next_unit()

    def _show_next_unit(self) -> None:
        if not self.playing:
            return
        if self.next_break_at and time.monotonic() >= self.next_break_at:
            self._start_rest_break()
            return
        unit = self.cursor.consume(self.settings.chunk_size)
        if unit is None:
            self._complete("End of book\n\nPress Esc to return to the page reader")
            return
        self.current_unit = unit
        self.stage = "visible"
        self.message.clear()
        self.display.set_text(unit.text)
        self._update_labels()
        visible, _ = presentation_timing(unit, self.settings)
        self.timer.start(visible)

    def _timer_fired(self) -> None:
        if self.stage == "countdown":
            self.countdown_remaining -= 1
            if self.countdown_remaining > 0:
                self.message.setText(str(self.countdown_remaining))
                self.timer.start(1000)
            else:
                self._begin_playback()
            return
        if not self.playing or self.current_unit is None:
            return
        if self.stage == "visible":
            _, blank = presentation_timing(self.current_unit, self.settings)
            if blank:
                self.stage = "blank"
                self.display.set_text("")
                self.timer.start(blank)
                return
        self._show_next_unit()

    def toggle_playback(self) -> None:
        if self.stage == "countdown":
            self.timer.stop()
            self.countdown_remaining = 0
            self._begin_playback()
            return
        if self.playing:
            self.pause()
            return
        if self.in_rest_break:
            self.in_rest_break = False
            self.current_unit = None
            self.next_break_at = (
                time.monotonic() + self.settings.rest_interval_minutes * 60
                if self.settings.rest_interval_minutes
                else 0.0
            )
        self.playing = True
        self.message.clear()
        self.play_button.setText("❚❚  Pause")
        self._set_chrome_for_playback()
        if self.current_unit is not None:
            self.stage = "visible"
            self.display.set_text(self.current_unit.text)
            visible, _ = presentation_timing(self.current_unit, self.settings)
            self.timer.start(visible)
        else:
            self._show_next_unit()

    def pause(self) -> None:
        self.playing = False
        self.timer.stop()
        self.play_button.setText("▶  Resume")
        if self.current_unit is not None:
            self.display.set_text(self.current_unit.text)
        self.header.show()
        self.footer.show()

    def _start_rest_break(self) -> None:
        self.playing = False
        self.in_rest_break = True
        self.timer.stop()
        self.display.set_text("")
        self.message.setText("Eye-rest pause\n\nLook away, blink naturally, and resume with Space when comfortable.")
        self.play_button.setText("▶  Resume")
        self.header.show()
        self.footer.show()

    def _complete(self, message: str) -> None:
        self.playing = False
        self.timer.stop()
        self.stage = "complete"
        self.display.set_text("")
        self.message.setText(message)
        self.play_button.setText("▶  Play")
        self.header.show()
        self.footer.show()

    def adjust_wpm(self, delta: int) -> None:
        self.settings = replace(self.settings, wpm=min(max(self.settings.wpm + delta, 80), 1200))
        self.display.set_settings(self.settings)
        self._update_labels()

    def jump_words(self, delta: int) -> None:
        was_playing = self.playing
        self.timer.stop()
        start = self.current_unit.global_index if self.current_unit else self.cursor.global_index
        self.cursor.seek_global(start + delta)
        self.current_unit = None
        self.progress.setValue(self.cursor.global_index)
        if was_playing:
            self._show_next_unit()
        else:
            unit = self.cursor.consume(self.settings.chunk_size)
            if unit:
                self.current_unit = unit
                self.display.set_text(unit.text)
                self._update_labels()

    def _begin_seek(self) -> None:
        self._resume_after_seek = self.playing
        if self.playing:
            self.pause()

    def _finish_seek(self) -> None:
        self.cursor.seek_global(self.progress.value())
        self.current_unit = None
        if self._resume_after_seek:
            self.playing = True
            self.play_button.setText("❚❚  Pause")
            self._set_chrome_for_playback()
            self._show_next_unit()
        else:
            unit = self.cursor.consume(self.settings.chunk_size)
            if unit:
                self.current_unit = unit
                self.display.set_text(unit.text)
        self._update_labels()

    def _set_chrome_for_playback(self) -> None:
        if self.settings.minimal_chrome:
            self.header.hide()
            self.footer.hide()

    def _update_labels(self) -> None:
        unit = self.current_unit
        chapter_index = unit.chapter_index if unit else self.cursor.chapter_index
        title = self.document.chapters[chapter_index].title if self.document.chapters else ""
        self.chapter_label.setText(title)
        self.wpm_label.setText(f"{self.settings.wpm} WPM  ·  {self.settings.chunk_size}×")
        current = unit.global_index if unit else self.cursor.global_index
        total = max(self.document.total_words, 1)
        self.progress.blockSignals(True)
        self.progress.setValue(current)
        self.progress.blockSignals(False)
        self.position_label.setText(f"{round(current / total * 100)}%  ·  {current:,} / {self.document.total_words:,} words")

    def reading_position(self) -> tuple[int, float]:
        if self.current_unit is not None:
            return (
                self.current_unit.chapter_index,
                self.cursor.chapter_scroll(self.current_unit.chapter_index, self.current_unit.word_index),
            )
        return self.cursor.chapter_index, self.cursor.chapter_scroll()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left:
            self.jump_words(-round(self.settings.wpm / 6))
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.jump_words(round(self.settings.wpm / 6))
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up:
            self.adjust_wpm(25)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self.adjust_wpm(-25)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: Any) -> None:
        self.timer.stop()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        background = self.settings.background_color
        foreground = self.settings.text_color
        focus = self.settings.focus_color
        self.setStyleSheet(f"""
            QDialog {{ background: {background}; color: {foreground}; }}
            QWidget {{ font-family: 'Segoe UI'; font-size: 13px; }}
            #speedBookTitle {{ color: {foreground}; font-weight: 750; font-size: 14px; }}
            #speedChapter, #speedPosition {{ color: rgba(210, 220, 218, .62); }}
            #speedBadge {{ color: {focus}; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.10); border-radius: 13px; padding: 6px 10px; }}
            #speedMessage {{ color: {foreground}; font-size: 25px; font-weight: 650; padding: 16px; }}
            QPushButton {{ color: #dce5e1; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.12); border-radius: 8px; padding: 8px 12px; }}
            QPushButton:hover {{ color: #07110d; background: {focus}; border-color: {focus}; }}
            #speedPlay {{ color: #07110d; background: {foreground}; border: none; font-weight: 750; min-width: 96px; }}
            QSlider::groove:horizontal {{ height: 4px; background: rgba(255,255,255,.12); border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {foreground}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {focus}; border: 2px solid {background}; width: 14px; margin: -6px 0; border-radius: 7px; }}
        """)
