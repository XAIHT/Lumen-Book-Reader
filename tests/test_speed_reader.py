from __future__ import annotations

import os
import sys

if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lumen_reader.speed_reader import (
    SpeedChapter,
    SpeedReaderDialog,
    SpeedReaderSettings,
    SpeedReadingCursor,
    SpeedReadingDocument,
    contrast_ratio,
    optimal_recognition_index,
    presentation_timing,
    tokenize_text,
)


def _document() -> SpeedReadingDocument:
    return SpeedReadingDocument(
        [
            SpeedChapter("One", tokenize_text("Alpha beta. Gamma delta epsilon")),
            SpeedChapter("Empty", []),
            SpeedChapter("Two", tokenize_text("Zeta eta theta.")),
        ]
    )


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_cursor_chunks_do_not_cross_sentences_or_chapters() -> None:
    cursor = SpeedReadingCursor(_document())
    first = cursor.consume(5)
    second = cursor.consume(5)
    third = cursor.consume(5)

    assert first is not None and first.words == ("Alpha", "beta.")
    assert second is not None and second.words == ("Gamma", "delta", "epsilon")
    assert third is not None and third.words == ("Zeta", "eta", "theta.")
    assert third.chapter_index == 2
    assert cursor.consume(5) is None


def test_cursor_starts_from_section_scroll_and_seeks_globally() -> None:
    document = _document()
    cursor = SpeedReadingCursor(document, chapter_index=0, chapter_scroll=0.6)
    assert cursor.word_index == 3
    assert cursor.consume(1).text == "delta"

    cursor.seek_global(5)
    assert cursor.chapter_index == 2
    assert cursor.consume(1).text == "Zeta"
    cursor.seek_relative(-2)
    assert cursor.chapter_index == 0
    assert cursor.consume(1).text == "epsilon"

    exact = SpeedReadingCursor(
        document,
        chapter_index=0,
        chapter_scroll=0.95,
        word_index=1,
    )
    assert exact.consume(1).text == "beta."


def test_adaptive_timing_respects_punctuation_length_and_blank_interval() -> None:
    document = SpeedReadingDocument(
        [SpeedChapter("One", ["short", "extraordinarily", "done."])]
    )
    settings = SpeedReaderSettings(wpm=300, blank_percent=10, long_word_extra_ms=10)
    cursor = SpeedReadingCursor(document)
    short = cursor.consume(1)
    long_word = cursor.consume(1)
    sentence = cursor.consume(1)

    short_visible, short_blank = presentation_timing(short, settings)
    long_visible, _ = presentation_timing(long_word, settings)
    sentence_visible, sentence_blank = presentation_timing(sentence, settings)
    assert long_visible > short_visible
    assert sentence_visible + sentence_blank > short_visible + short_blank
    assert short_blank > 0


def test_settings_round_trip_clamps_unsafe_or_invalid_values() -> None:
    settings = SpeedReaderSettings.from_mapping(
        {
            "wpm": 9000,
            "chunk_size": 0,
            "blank_percent": "bad",
            "text_color": "not-a-color",
            "fullscreen": False,
        }
    )
    assert settings.wpm == 1200
    assert settings.chunk_size == 1
    assert settings.blank_percent == SpeedReaderSettings().blank_percent
    assert settings.text_color == SpeedReaderSettings().text_color
    assert settings.fullscreen is False
    assert SpeedReaderSettings.from_mapping(settings.to_dict()) == settings
    assert SpeedReaderSettings.from_mapping({"countdown_seconds": 0}).countdown_seconds == 3


def test_speed_reader_welcomes_then_counts_down_before_first_word() -> None:
    app = _application()
    player = SpeedReaderDialog(
        document=_document(),
        settings=SpeedReaderSettings(countdown_seconds=0),
        book_title="Test Book",
        start_chapter=0,
        start_scroll=0.0,
    )

    player.start_session()
    assert player.stage == "countdown"
    assert player.playing is False
    assert "Welcome to Lumen Speed Reading" in player.message.text()
    assert player.display.text == "3"
    assert not player.play_button.isEnabled()

    player.toggle_playback()
    assert player.stage == "countdown"
    assert player.display.text == "3"

    player._timer_fired()
    assert player.display.text == "2"
    player._timer_fired()
    assert player.display.text == "1"
    player._timer_fired()
    assert player.stage == "visible"
    assert player.playing is True
    assert player.display.text == "Alpha"
    assert player.last_presented_position() == (0, 0, 1)
    assert player.play_button.isEnabled()

    player.timer.stop()
    player.close()
    app.processEvents()


def test_focus_position_and_default_contrast_are_readable() -> None:
    assert optimal_recognition_index("I") == 0
    assert optimal_recognition_index("reading") == 2
    defaults = SpeedReaderSettings()
    assert contrast_ratio(defaults.text_color, defaults.background_color) >= 7.0
