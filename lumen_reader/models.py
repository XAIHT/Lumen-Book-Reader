"""Data models shared by the parser, persistence layer, and GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TocEntry:
    title: str
    href: str
    chapter_index: int | None = None
    children: list["TocEntry"] = field(default_factory=list)


@dataclass(slots=True)
class Chapter:
    id: str
    href: str
    media_type: str
    title: str


@dataclass(slots=True)
class BookMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    language: str = ""
    publisher: str = ""
    description: str = ""
    identifier: str = ""
    cover_href: str | None = None

    @property
    def author_line(self) -> str:
        return ", ".join(self.authors) if self.authors else "Unknown author"


@dataclass(slots=True)
class SearchResult:
    chapter_index: int
    chapter_title: str
    excerpt: str
    match_count: int


@dataclass(slots=True)
class Bookmark:
    chapter_index: int
    chapter_title: str
    scroll_percent: float
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bookmark":
        return cls(
            chapter_index=int(data.get("chapter_index", 0)),
            chapter_title=str(data.get("chapter_title", "Bookmark")),
            scroll_percent=float(data.get("scroll_percent", 0.0)),
            created_at=str(data.get("created_at", "")),
        )

