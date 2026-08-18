"""Small, resilient JSON persistence layer for reader preferences and progress."""

from __future__ import annotations

import hashlib
import json
import os
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


def _book_state_key(path: str, size: int, modified_ns: int) -> str:
    signature = f"{path}|{size}|{modified_ns}"
    return hashlib.sha256(signature.encode("utf-8", "surrogatepass")).hexdigest()[:24]


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

    def relink_missing_books(self, library_directory: str | Path) -> int:
        """Repair recent-book paths and matching progress after a library move."""
        root = Path(library_directory).expanduser().resolve()
        try:
            candidates = [
                path.resolve()
                for path in root.iterdir()
                if path.is_file() and path.suffix.casefold() in {".epub", ".pdf"}
            ]
        except OSError:
            return 0
        by_name: dict[str, list[Path]] = {}
        for candidate in candidates:
            by_name.setdefault(candidate.name.casefold(), []).append(candidate)

        changed = 0
        recent = self.data.setdefault("recent_books", [])
        books = self.data.setdefault("books", {})
        for item in recent:
            if not isinstance(item, dict):
                continue
            old_text = str(item.get("path") or "")
            try:
                if Path(old_text).expanduser().is_file():
                    continue
            except OSError:
                pass
            matches = by_name.get(Path(old_text).name.casefold(), [])
            if len(matches) != 1:
                continue
            candidate = matches[0]
            new_text = str(candidate)
            stat = candidate.stat()
            old_resolved = str(Path(old_text).expanduser().resolve())
            old_key = _book_state_key(old_resolved, stat.st_size, stat.st_mtime_ns)
            new_key = _book_state_key(new_text, stat.st_size, stat.st_mtime_ns)
            if old_key in books and new_key not in books:
                books[new_key] = books.pop(old_key)
            item["path"] = new_text
            changed += 1

        if changed:
            deduplicated: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in recent:
                if not isinstance(item, dict):
                    continue
                key = os.path.normcase(str(item.get("path") or ""))
                if key in seen:
                    continue
                seen.add(key)
                deduplicated.append(item)
            self.data["recent_books"] = deduplicated[:8]
            self.save()
        return changed
