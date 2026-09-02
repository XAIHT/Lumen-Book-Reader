"""Deterministic, Unicode-safe passage boundaries with exact text offsets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterator

from .passage_models import CHUNKER_VERSION, PassageChunk
from .text_safety import clean_unicode_text


_BOUNDARY_RE = re.compile(r"(?:[.!?][\"'\)\]\u201d\u2019]*\s+|\n\s*\n+)", re.UNICODE)
_WORD_RE = re.compile(r"\w+(?:[-'\u2019]\w+)*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    target_tokens: int = 700
    soft_min_tokens: int = 450
    soft_max_tokens: int = 900
    hard_max_tokens: int = 1200
    overlap_tokens: int = 80
    chars_per_token: int = 4

    @property
    def target_chars(self) -> int:
        return self.target_tokens * self.chars_per_token

    @property
    def soft_min_chars(self) -> int:
        return self.soft_min_tokens * self.chars_per_token

    @property
    def soft_max_chars(self) -> int:
        return self.soft_max_tokens * self.chars_per_token

    @property
    def hard_max_chars(self) -> int:
        return self.hard_max_tokens * self.chars_per_token

    @property
    def overlap_chars(self) -> int:
        return self.overlap_tokens * self.chars_per_token


class PassageChunker:
    version = CHUNKER_VERSION

    def __init__(self, config: ChunkerConfig | None = None):
        self.config = config or ChunkerConfig()
        if not (0 < self.config.soft_min_tokens <= self.config.target_tokens
                <= self.config.soft_max_tokens <= self.config.hard_max_tokens):
            raise ValueError("invalid passage chunk size ordering")
        if not (0 <= self.config.overlap_tokens < self.config.soft_min_tokens):
            raise ValueError("passage overlap must be smaller than the soft minimum")

    def chunks(self, value: str) -> Iterator[PassageChunk]:
        text = clean_unicode_text(value)
        if not text.strip():
            return

        length = len(text)
        start = _skip_space_forward(text, 0)
        ordinal = 0
        estimated_token_cursor = 0
        previous_start = -1

        while start < length:
            if start <= previous_start:
                start = previous_start + 1
            previous_start = start
            hard_end = min(length, start + self.config.hard_max_chars)
            if hard_end == length:
                end = length
            else:
                minimum = min(hard_end, start + self.config.soft_min_chars)
                preferred = min(hard_end, start + self.config.target_chars)
                maximum = min(hard_end, start + self.config.soft_max_chars)
                end = _best_boundary(text, minimum, preferred, maximum)
                if end <= start:
                    end = _whitespace_boundary(text, preferred, hard_end)
                if end <= start:
                    end = hard_end

            content_start = _skip_space_forward(text, start)
            content_end = _skip_space_backward(text, end)
            if content_end <= content_start:
                start = max(end, start + 1)
                continue
            body = text[content_start:content_end]
            words = len(_WORD_RE.findall(body))
            token_count = max(1, (len(body) + self.config.chars_per_token - 1)
                              // self.config.chars_per_token)
            yield PassageChunk(
                ordinal=ordinal,
                char_start=content_start,
                char_end=content_end,
                text=body,
                word_count=words,
                token_start=estimated_token_cursor,
                token_end=estimated_token_cursor + token_count,
                content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
            ordinal += 1
            estimated_token_cursor += token_count
            if end >= length:
                break
            overlap_start = max(content_start + 1, content_end - self.config.overlap_chars)
            start = _skip_space_forward(text, _word_start(text, overlap_start, content_end))


def _best_boundary(text: str, minimum: int, preferred: int, maximum: int) -> int:
    candidates = [match.end() for match in _BOUNDARY_RE.finditer(text, minimum, maximum)]
    if not candidates:
        return 0
    before = [value for value in candidates if value <= preferred]
    return before[-1] if before else candidates[0]


def _whitespace_boundary(text: str, preferred: int, maximum: int) -> int:
    for index in range(min(maximum, len(text)) - 1, preferred - 1, -1):
        if text[index].isspace():
            return index + 1
    return 0


def _word_start(text: str, start: int, end: int) -> int:
    index = min(max(start, 0), len(text))
    while index < end and index > 0 and not text[index - 1].isspace():
        index += 1
    return min(index, end)


def _skip_space_forward(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_space_backward(text: str, index: int) -> int:
    while index > 0 and text[index - 1].isspace():
        index -= 1
    return index
