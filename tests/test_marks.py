from __future__ import annotations

import json
from pathlib import Path

from lumen_reader.marks import MARKS_FILENAME, MarksStore, ReadingMark


def _mark(book: Path, note: str = "Remember this") -> ReadingMark:
    return ReadingMark.create(
        book_path=str(book),
        book_title="Example Book",
        book_author="Ada Reader",
        chapter_index=3,
        chapter_title="A Useful Chapter",
        scroll_percent=0.42,
        overall_percent=0.25,
        note=note,
        quote="A selected passage",
        tags=["idea", "Review", "idea"],
    )


def test_marks_file_is_created_and_round_trips(tmp_path: Path) -> None:
    store = MarksStore(tmp_path / MARKS_FILENAME)
    assert store.path.exists()
    book = tmp_path / "book.epub"
    book.touch()
    created = store.add(_mark(book))

    loaded = MarksStore(store.path)
    mark = loaded.get(created.id)
    assert mark is not None
    assert mark.note == "Remember this"
    assert mark.tags == ["idea", "Review"]
    assert mark.scroll_percent == 0.42
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1


def test_marks_are_searchable_editable_and_removable(tmp_path: Path) -> None:
    book = tmp_path / "book.epub"
    book.touch()
    store = MarksStore(tmp_path / MARKS_FILENAME)
    mark = store.add(_mark(book))
    assert store.search("ada useful review") == [mark]
    assert store.search("selected passage") == [mark]
    assert store.has_position(str(book), 3, 0.421)

    updated = store.update(mark.id, note="Revised thought", tags=["important"])
    assert updated is not None
    assert store.search("revised important") == [mark]
    assert store.remove(mark.id)
    assert not store.search()


def test_marks_relink_automatically_after_library_folder_moves(tmp_path: Path) -> None:
    old_library = tmp_path / "old-library"
    new_library = tmp_path / "new-library"
    old_library.mkdir()
    new_library.mkdir()
    old_book = old_library / "portable-book.epub"
    old_book.touch()
    marks_path = new_library / MARKS_FILENAME
    store = MarksStore(marks_path)
    mark = store.add(_mark(old_book))

    new_book = old_book.replace(new_library / old_book.name)
    relocated = MarksStore(marks_path)

    assert relocated.get(mark.id).book_path == str(new_book.resolve())
    persisted = json.loads(marks_path.read_text(encoding="utf-8"))
    assert persisted["marks"][0]["book_path"] == str(new_book.resolve())
