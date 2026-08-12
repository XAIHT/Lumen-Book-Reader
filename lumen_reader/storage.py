"""Small, resilient JSON persistence layer for reader preferences and progress."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .smart_definition import default_definition_fallbacks
from .speed_reader import DEFAULT_SPEED_READER_SETTINGS


DEFAULT_DATA: dict[str, Any] = {
    "theme": "dark",
    "font_size": 20,
    "sidebar_visible": True,
    "recent_books": [],
    "books": {},
    "definition_fallbacks": default_definition_fallbacks(),
    "speed_reader": DEFAULT_SPEED_READER_SETTINGS,
}


class ReaderStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.data = {}
        for key, value in DEFAULT_DATA.items():
            self.data.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def book_state(self, key: str) -> dict[str, Any]:
        books = self.data.setdefault("books", {})
        return books.setdefault(key, {"chapter": 0, "scroll": 0.0, "bookmarks": []})

    def remember_book(self, path: str, title: str, author: str) -> None:
        recent = self.data.setdefault("recent_books", [])
        recent[:] = [item for item in recent if item.get("path") != path]
        recent.insert(0, {"path": path, "title": title, "author": author})
        del recent[8:]
