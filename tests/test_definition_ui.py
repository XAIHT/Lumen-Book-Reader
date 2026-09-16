"""Regression coverage for definition-source orchestration in the live reader."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform != "win32" and not (
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from lumen_reader import ui as ui_module
from lumen_reader.dictionary import DictionaryEntry
from lumen_reader.library_index import LibraryIndex
from lumen_reader.marks import MarksStore
from lumen_reader.storage import ReaderStore
from lumen_reader.ui import ReaderWindow


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_enabled_ollama_starts_immediately_beside_a_cached_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _application()
    monkeypatch.setattr(ui_module, "lookup_offline_wordnet_entries", lambda *_args: [])
    index = LibraryIndex(tmp_path / "library.db")
    window = ReaderWindow(
        ReaderStore(tmp_path / "reader.json"),
        marks_store=MarksStore(tmp_path / "marks.json"),
        library_root=tmp_path,
        library_index=index,
    )
    window.show()
    app.processEvents()
    cached = DictionaryEntry(
        word="lumen",
        phonetic="",
        part_of_speech="noun",
        definition="A deterministic cached definition.",
        synonyms=(),
        source="WordNet · offline",
    )
    window._dictionary_cache["lumen"] = cached
    window.definition_fallbacks.update(
        {"ollama_enabled": True, "ollama_model": "context-model:latest"}
    )
    ollama_starts: list[tuple[str, int, str]] = []
    start_order: list[str] = []

    def record_dictionary_start(source: str, *_args) -> None:
        start_order.append(source)

    def record_ollama_start(term: str, request_id: int, model: str) -> None:
        start_order.append("ollama")
        ollama_starts.append((term, request_id, model))

    monkeypatch.setattr(
        window,
        "_start_dictionary_source",
        record_dictionary_start,
    )
    monkeypatch.setattr(window, "_start_ollama_source", record_ollama_start)

    try:
        window._lookup_selected_word(
            "lumen", QPoint(40, 40), "The lumen definition depends on this passage."
        )

        assert window.definition_card.definition_count == 1
        assert ollama_starts == [
            ("lumen", window._dictionary_request_id, "context-model:latest")
        ]
        assert start_order == ["ollama", "wiktionary", "dictionaryapi"]
        assert "ollama" in window._dictionary_pending_sources
        assert {"wordnet", "wiktionary", "dictionaryapi"}.issubset(
            window._dictionary_pending_sources
        )
    finally:
        window._cancel_dictionary_lookup()
        window.close()
        window.deleteLater()
        app.processEvents()
        index.close()
