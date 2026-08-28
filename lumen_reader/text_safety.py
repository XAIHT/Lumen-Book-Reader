"""Unicode boundaries shared by every reader and persistence path.

Python can carry lone UTF-16 surrogate code points in ``str`` values even
though they are not Unicode scalar values and therefore cannot be encoded as
strict UTF-8.  MuPDF exposes them for a small number of malformed PDF metadata
strings.  Qt, JSON and SQLite all eventually require valid Unicode, so the
repair belongs at the document boundary instead of being rediscovered by each
consumer.
"""

from __future__ import annotations

import re
from typing import Any


_INVALID_TEXT_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffd]+"
)
_SPACE_RE = re.compile(r"\s+")


def clean_unicode_text(value: Any, *, collapse_whitespace: bool = True) -> str:
    """Return display- and UTF-8-safe text without inventing replacement data.

    Invalid scalar values and control padding are changed to spaces, not the
    replacement glyph: malformed PDF metadata commonly repeats byte-padding
    pairs, and displaying one replacement character per byte makes a title less
    readable than omitting the padding.  Valid non-ASCII text is preserved.
    """

    text = "" if value is None else str(value)
    cleaned = _INVALID_TEXT_RE.sub(" ", text)
    if collapse_whitespace:
        cleaned = _SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def contains_invalid_unicode(value: Any) -> bool:
    """Whether *value* contains a scalar that strict UTF-8 cannot represent."""

    return _INVALID_TEXT_RE.search("" if value is None else str(value)) is not None


def require_utf8(value: Any, *, label: str = "text") -> str:
    """Validate a value that cannot be rewritten, such as a filesystem path."""

    text = "" if value is None else str(value)
    try:
        text.encode("utf-8", "strict")
    except UnicodeError as exception:
        raise ValueError(f"{label} contains invalid Unicode") from exception
    return text


def escaped_for_log(value: Any, limit: int = 320) -> str:
    """A single-line diagnostic that never writes raw invalid scalars."""

    text = "" if value is None else str(value)
    escaped = text.encode("ascii", "backslashreplace").decode("ascii")
    escaped = _SPACE_RE.sub(" ", escaped).strip()
    return escaped[:max(0, int(limit))]


__all__ = [
    "clean_unicode_text",
    "contains_invalid_unicode",
    "escaped_for_log",
    "require_utf8",
]
