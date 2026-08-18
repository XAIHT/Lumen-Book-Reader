from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from lumen_reader.book import EpubBook, EpubError
from lumen_reader.storage import ReaderStore, _book_state_key


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


def test_store_relinks_recent_book_and_progress_after_library_move(tmp_path: Path) -> None:
    old_library = tmp_path / "old-library"
    new_library = tmp_path / "new-library"
    old_library.mkdir()
    new_library.mkdir()
    old_book = old_library / "portable-book.pdf"
    old_book.write_bytes(b"portable fixture")
    stat = old_book.stat()
    old_path = str(old_book.resolve())
    old_key = _book_state_key(old_path, stat.st_size, stat.st_mtime_ns)

    store = ReaderStore(tmp_path / "reader-state.json")
    store.remember_book(old_path, "Portable Book", "Ada Reader")
    store.data["books"][old_key] = {"chapter": 7, "scroll": 0.42, "bookmarks": []}
    store.save()

    new_book = old_book.replace(new_library / old_book.name)
    assert store.relink_missing_books(new_library) == 1
    new_stat = new_book.stat()
    new_path = str(new_book.resolve())
    new_key = _book_state_key(new_path, new_stat.st_size, new_stat.st_mtime_ns)

    loaded = ReaderStore(store.path)
    assert loaded.data["recent_books"][0]["path"] == new_path
    assert loaded.data["books"][new_key]["chapter"] == 7
    assert loaded.data["books"][new_key]["scroll"] == 0.42
