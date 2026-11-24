"""Which title a book is shown under: its own metadata, unless that is not a title.

A PDF's ``/Title`` is usually written by whatever program produced the file,
not by whoever published the book.  Measured on a real 14,126-book library,
706 stored titles (699 of them from the 5,567 PDFs) were a producer's leftovers
rather than titles - ``0071613757.pdf`` (the ISBN the file once had), ``Microsoft Word -
43A0E3A9-2D54-20F7A6.doc``, ``fasc0b.dvi``, ``file:///F|/rah/...txt``,
``Untitled Document``, and 86 copies of ``THIS CD PRODUCED BY``.  The shelf
sorted them to the top and showed them in place of the book.

The file name is what the owner of the book chose, so it is the fallback.
Every rule below was taken from that library and has to answer one question:
is this string a title, or is it the trace of a tool?  When unsure, the
metadata wins - a short real title (``1984``, ``It``, ``22/11/63``) is kept
whenever the file name begins with the same words.

EPUB titles are the publisher's own and get the general rules only.  A
one-word PDF title that shares no word with the file name (``ACmain``,
``urbaf2``, ``Image4``) is a job name in practice; the same test on EPUBs
would throw away ``Olympos`` and ``Tumbuctú``.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

#: Bump when the rules change, so stored catalogs are judged again once.
TITLE_POLICY_VERSION = 1
TITLE_POLICY_KEY = "title_policy_version"

# The name of the document the PDF was printed from, optionally with the page
# suffix some drivers append: "Rounded Text.qxd (Page 1)".
_FILE_NAME_RE = re.compile(
    r"\.(?:pdf|epub|mobi|azw3?|djvu|chm|lit|docx?|dot|rtf|txt|odt|wpd|wps|html?|xml|tex"
    r"|dvi|e?ps|ai|indd|indb|qxd|qxp|pm[56]|p65|fm|pptx?|xlsx?|cdr|tiff?|tmp)"
    r"(?:\s*\(page\s*\d+\))?$",
    re.IGNORECASE,
)
# file:///C|/..., http://..., C:\Documents and Settings\..., \\server\share
_LOCATION_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[a-z]:[\\/|]|\\\\)", re.IGNORECASE)
_APPLICATION_RE = re.compile(
    r"^microsoft\s+(?:word|powerpoint|excel|publisher)\s+-", re.IGNORECASE
)
# Elsevier "PII: S0040-6031(01)00805-X", "doi:...", and bare content hashes.
_IDENTIFIER_RE = re.compile(r"^(?:(?:pii|doi)\s*:|[0-9a-f]{16,}$)", re.IGNORECASE)
# Typesetter job names carry their page range: "jcr809955c 288..289".
_PAGE_RANGE_RE = re.compile(r"\d\.\.\d")
_TRAILING_COUNTER_RE = re.compile(r"[\s_-]*\d+$")
_CD_STAMP_RE = re.compile(r"^this cd (?:is )?produced by\b")
_WORD_RE = re.compile(r"[^\W_]+")

# Compared after folding case and accents, and after a trailing counter is
# removed ("Untitled-2", "Document1", "Test1").
_PLACEHOLDERS = frozenset({
    "untitled", "untitled document", "document", "new document", "unknown", "title",
    "titulo", "no title", "book title", "paper title (use style: paper title)",
    "app note template", "powerpoint presentation", "djvu document", "pdf document",
    "no job name", "cover", "front matter", "frontmatter", "table of contents",
    "contents", "introduction", "preface", "index", "author's note", "full page fax print",
    "test", "main", "sin titulo", "sans titre", "ohne titel",
    "неизвестное название", "без названия",
})


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def title_problem(title: str, file_name: str, ext: str = "") -> str | None:
    """Why *title* is not a usable title for the file *file_name*, or ``None``."""
    text = (title or "").strip()
    if not text:
        return "empty"
    if _FILE_NAME_RE.search(text):
        return "file name"
    if _LOCATION_RE.match(text):
        return "file location"
    if _APPLICATION_RE.match(text):
        return "application label"
    if _IDENTIFIER_RE.match(text) or _PAGE_RANGE_RE.search(text):
        return "identifier"
    folded = _fold(text)
    if _TRAILING_COUNTER_RE.sub("", folded) in _PLACEHOLDERS or _CD_STAMP_RE.match(folded):
        return "placeholder"

    words = _WORD_RE.findall(folded)
    stem_words = _WORD_RE.findall(_fold(Path(file_name).stem))
    if words and stem_words[: len(words)] == words:
        return None  # the file is named after it: "1984 - George Orwell.epub"
    if not any(c.isalpha() for c in text) or len(text) <= 3:
        return "code"
    suffix = (ext or Path(file_name).suffix).casefold()
    if suffix == ".pdf" and len(words) == 1 and words[0] not in stem_words:
        return "code"
    return None


def choose_title(title: str, file_name: str, ext: str = "") -> str:
    """The metadata title when it is one, else the file name without extension."""
    if title_problem(title, file_name, ext) is None:
        return title.strip()
    return Path(file_name).stem
