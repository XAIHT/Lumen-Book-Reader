"""Immutable records shared by passage extraction, storage, and retrieval."""

from __future__ import annotations

from dataclasses import dataclass


EXTRACTOR_VERSION = "lumen-passages-v1"
CHUNKER_VERSION = "unicode-char-v1"

COVERAGE_COMPLETE = "complete"
COVERAGE_CAPPED = "capped"
COVERAGE_METADATA_ONLY = "metadata_only"
COVERAGE_NO_TEXT = "no_text_layer"
COVERAGE_LOCKED = "locked"
COVERAGE_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceSection:
    ordinal: int
    kind: str
    title: str
    text: str
    href: str = ""
    fragment: str = ""
    page_start: int | None = None
    page_end: int | None = None


@dataclass(frozen=True, slots=True)
class PassageChunk:
    ordinal: int
    char_start: int
    char_end: int
    text: str
    word_count: int
    token_start: int
    token_end: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class PassageBuildReport:
    book_id: int
    revision: int
    coverage: str
    sections: int
    passages: int
    characters: int
    words: int
    content_sha256: str
