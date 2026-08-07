from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from lumen_reader.book import EpubBook, EpubError
from lumen_reader.storage import ReaderStore


def test_rejects_zip_path_traversal(tmp_path: Path) -> None:
    malicious = tmp_path / "unsafe.epub"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("../outside.txt", "should never be extracted")
    with pytest.raises(EpubError, match="Unsafe path"):
        EpubBook(malicious)
    assert not (tmp_path / "outside.txt").exists()


def test_store_round_trip_and_recent_book_deduplication(tmp_path: Path) -> None:
    path = tmp_path / "reader-state.json"
    store = ReaderStore(path)
    store.data["theme"] = "sepia"
    state = store.book_state("book-key")
    state["chapter"] = 7
    store.remember_book("C:/Books/example.epub", "Example", "Writer")
    store.remember_book("C:/Books/example.epub", "New title", "Writer")
    store.save()

    loaded = ReaderStore(path)
    assert loaded.data["theme"] == "sepia"
    assert loaded.book_state("book-key")["chapter"] == 7
    assert len(loaded.data["recent_books"]) == 1
    assert loaded.data["recent_books"][0]["title"] == "New title"
    assert json.loads(path.read_text(encoding="utf-8"))["font_size"] == 20


def test_store_recovers_from_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "reader-state.json"
    path.write_text("not json", encoding="utf-8")
    store = ReaderStore(path)
    assert store.data["theme"] == "dark"
    assert store.data["recent_books"] == []

