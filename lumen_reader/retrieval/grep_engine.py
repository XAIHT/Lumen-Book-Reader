"""Bounded exact and optional timeout-capable regex verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

from .contracts import RetrievalError


try:
    import regex as _bounded_regex
except ImportError:  # pragma: no cover - exercised in packaged dependency tests
    _bounded_regex = None


@dataclass(frozen=True, slots=True)
class MatchRange:
    start: int
    end: int


def regex_available() -> bool:
    return _bounded_regex is not None


def required_literal(pattern: str) -> str:
    # Conservative only: escaped/alphanumeric runs outside obvious character
    # classes are candidate hints, never a correctness filter by themselves.
    candidates = re.findall(r"[\w\u0080-\uffff]{3,}", pattern, flags=re.UNICODE)
    return max(candidates, key=len, default="")


def exact_ranges(
    text: str,
    query: str,
    *,
    case_sensitive: bool,
    whole_word: bool,
    maximum: int,
) -> list[MatchRange]:
    if not query:
        return []
    haystack = text if case_sensitive else text.casefold()
    needle = query if case_sensitive else query.casefold()
    ranges: list[MatchRange] = []
    start = 0
    while len(ranges) < maximum:
        index = haystack.find(needle, start)
        if index < 0:
            break
        end = index + len(needle)
        if not whole_word or _is_word_boundary(text, index, end):
            ranges.append(MatchRange(index, end))
        start = max(index + 1, end)
    return ranges


def regex_ranges(
    text: str,
    pattern: str,
    *,
    case_sensitive: bool,
    maximum: int,
    timeout_seconds: float = 0.05,
) -> list[MatchRange]:
    if _bounded_regex is None:
        raise RetrievalError(
            "BACKEND_UNAVAILABLE",
            "Regex mode requires the optional timeout-capable `regex` package.",
            suggested_action="Install the MCP extra or request literal fallback explicitly.",
        )
    flags = _bounded_regex.VERSION1
    if not case_sensitive:
        flags |= _bounded_regex.IGNORECASE | _bounded_regex.FULLCASE
    try:
        compiled = _bounded_regex.compile(pattern, flags)
        ranges: list[MatchRange] = []
        for match in compiled.finditer(text, timeout=timeout_seconds):
            ranges.append(MatchRange(match.start(), match.end()))
            if len(ranges) >= maximum:
                break
        return ranges
    except TimeoutError as exception:
        raise RetrievalError(
            "QUERY_TIMEOUT",
            "Regex verification exceeded its per-passage deadline.",
            suggested_action="Narrow the expression by book, format, or required literal.",
        ) from exception
    except Exception as exception:
        error_type = getattr(_bounded_regex, "error", ValueError)
        if isinstance(exception, error_type):
            raise RetrievalError("INVALID_ARGUMENT", f"Invalid regular expression: {exception}.") from exception
        raise


def excerpt_for_ranges(
    text: str,
    ranges: list[MatchRange],
    *,
    context_chars: int,
) -> tuple[str, list[dict[str, int]]]:
    if not ranges:
        excerpt = text[:context_chars]
        return excerpt, []
    first = ranges[0]
    radius = max(40, context_chars // 2)
    start = max(0, first.start - radius)
    end = min(len(text), max(first.end + radius, start + context_chars))
    if end - start > context_chars:
        end = start + context_chars
    excerpt = text[start:end]
    mapped = [
        {"start": item.start - start, "end": item.end - start}
        for item in ranges
        if item.start >= start and item.end <= end
    ]
    return excerpt, mapped


def _is_word_boundary(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return (not before or not (before.isalnum() or before == "_")) and (
        not after or not (after.isalnum() or after == "_")
    )
