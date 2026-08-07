"""Cross-book reading marks and notes stored beside the EPUB library."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


MARKS_FILENAME = "lumen-reading-marks.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class ReadingMark:
    id: str
    book_path: str
    book_title: str
    book_author: str
    chapter_index: int
    chapter_title: str
    scroll_percent: float
    overall_percent: float
    note: str
    quote: str
    tags: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        book_path: str,
        book_title: str,
        book_author: str,
        chapter_index: int,
        chapter_title: str,
        scroll_percent: float,
        overall_percent: float,
        note: str = "",
        quote: str = "",
        tags: list[str] | None = None,
    ) -> "ReadingMark":
        timestamp = _now()
        return cls(
            id=uuid.uuid4().hex,
            book_path=str(Path(book_path).expanduser().resolve()),
            book_title=book_title.strip(),
            book_author=book_author.strip(),
            chapter_index=max(0, int(chapter_index)),
            chapter_title=chapter_title.strip(),
            scroll_percent=max(0.0, min(float(scroll_percent), 1.0)),
            overall_percent=max(0.0, min(float(overall_percent), 1.0)),
            note=note.strip(),
            quote=quote.strip()[:1000],
            tags=_clean_tags(tags or []),
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReadingMark":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            book_path=str(data.get("book_path") or ""),
            book_title=str(data.get("book_title") or "Unknown book"),
            book_author=str(data.get("book_author") or "Unknown author"),
            chapter_index=max(0, int(data.get("chapter_index", 0))),
            chapter_title=str(data.get("chapter_title") or "Untitled section"),
            scroll_percent=max(0.0, min(float(data.get("scroll_percent", 0.0)), 1.0)),
            overall_percent=max(0.0, min(float(data.get("overall_percent", 0.0)), 1.0)),
            note=str(data.get("note") or ""),
            quote=str(data.get("quote") or "")[:1000],
            tags=_clean_tags(data.get("tags") or []),
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or data.get("created_at") or _now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def searchable_text(self) -> str:
        return " ".join(
            (
                self.book_title,
                self.book_author,
                Path(self.book_path).name,
                self.chapter_title,
                self.note,
                self.quote,
                " ".join(self.tags),
            )
        ).casefold()

    @property
    def summary(self) -> str:
        return self.note or self.quote or "Position marker"


def _clean_tags(tags: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        clean = str(tag).strip().lstrip("#")
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result[:20]


class MarksStore:
    """Atomic JSON store for marks spanning every book in a library folder."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()
        self.marks: list[ReadingMark] = []
        self.load()
        if not self.path.exists():
            self.save()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.marks = []
            return
        raw_marks = payload.get("marks", []) if isinstance(payload, dict) else []
        parsed: list[ReadingMark] = []
        for item in raw_marks:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(ReadingMark.from_dict(item))
            except (TypeError, ValueError):
                continue
        self.marks = parsed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "marks": [mark.to_dict() for mark in self.marks]}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def add(self, mark: ReadingMark) -> ReadingMark:
        self.marks.append(mark)
        self.save()
        return mark

    def get(self, mark_id: str) -> ReadingMark | None:
        return next((mark for mark in self.marks if mark.id == mark_id), None)

    def update(self, mark_id: str, *, note: str, tags: list[str]) -> ReadingMark | None:
        mark = self.get(mark_id)
        if mark is None:
            return None
        mark.note = note.strip()
        mark.tags = _clean_tags(tags)
        mark.updated_at = _now()
        self.save()
        return mark

    def remove(self, mark_id: str) -> bool:
        original = len(self.marks)
        self.marks = [mark for mark in self.marks if mark.id != mark_id]
        if len(self.marks) == original:
            return False
        self.save()
        return True

    def for_book(self, book_path: str | os.PathLike[str]) -> list[ReadingMark]:
        resolved = os.path.normcase(str(Path(book_path).expanduser().resolve()))
        return sorted(
            (mark for mark in self.marks if os.path.normcase(mark.book_path) == resolved),
            key=lambda mark: (mark.chapter_index, mark.scroll_percent, mark.created_at),
        )

    def search(self, query: str = "") -> list[ReadingMark]:
        terms = query.casefold().split()
        matches = [
            mark for mark in self.marks if all(term in mark.searchable_text for term in terms)
        ]
        return sorted(matches, key=lambda mark: mark.updated_at, reverse=True)

    def has_position(self, book_path: str, chapter_index: int, scroll_percent: float) -> bool:
        return any(
            mark.chapter_index == chapter_index
            and abs(mark.scroll_percent - scroll_percent) < 0.005
            for mark in self.for_book(book_path)
        )

