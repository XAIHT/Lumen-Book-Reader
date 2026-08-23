"""Tests for the parallel library indexer and its search engine."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from lumen_reader.library_index import (
    LibraryIndex,
    build_match_expression,
    extract_book,
    normalize_root,
    walk_library,
)

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
   media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:publisher>{publisher}</dc:publisher>
    <dc:language>en</dc:language>
    {subjects}
  </metadata>
  <manifest>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""

CHAPTER = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<style>.x {{ color: red; }}</style>
<script>var hidden = "neverindexed";</script>
<p>{body}</p>
</body></html>"""


def make_epub(
    path: Path,
    title: str = "A Title",
    author: str = "An Author",
    body: str = "ordinary prose",
    publisher: str = "A Press",
    subjects: list[str] | None = None,
) -> Path:
    tags = "".join(f"<dc:subject>{s}</dc:subject>" for s in (subjects or []))
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            OPF.format(title=title, author=author, publisher=publisher, subjects=tags),
        )
        archive.writestr("OEBPS/c1.xhtml", CHAPTER.format(body=body))
    return path


def make_pdf(path: Path, title: str = "PDF Title", body: str = "printed words") -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), body)
    document.set_metadata({"title": title, "author": "Pdf Writer"})
    document.save(str(path))
    document.close()
    return path


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    root = tmp_path / "datalake"
    make_epub(root / "alpha.epub", "Alpha World", "Ada Writer",
              "the alpha book discusses hydrology at length", subjects=["Science", "Water"])
    make_epub(root / "nested" / "beta.epub", "Beta Days", "Ben Author",
              "beta explores medieval falconry")
    make_pdf(root / "gamma.pdf", "Gamma Report", "detonation velocity measurements")
    # Never indexed: not a book, and inside a skipped directory.
    (root / "notes.txt").write_text("ignore me", encoding="utf-8")
    make_epub(root / ".git" / "hidden.epub", "Hidden", "Nobody", "secret")
    return root


# ────────────────────────────── the walk ──────────────────────────────────


def test_walk_finds_books_recursively_and_skips_noise(library: Path) -> None:
    found = {Path(path).name for path, _size, _mtime, _suffix in walk_library(library)}
    assert found == {"alpha.epub", "beta.epub", "gamma.pdf"}


def test_walk_reports_size_and_mtime(library: Path) -> None:
    for path, size, mtime_ns, suffix in walk_library(library):
        assert size == Path(path).stat().st_size
        assert mtime_ns == Path(path).stat().st_mtime_ns
        assert suffix in {".epub", ".pdf"}


# ─────────────────────────── metadata extraction ──────────────────────────


def test_epub_metadata_and_text_are_extracted(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "b.epub", "Deep Title", "Real Author",
                     "indexable sentence here", subjects=["History", "War"])
    record = extract_book((str(path), ".epub", 100_000))
    assert record["ok"] is True
    assert record["title"] == "Deep Title"
    assert record["author"] == "Real Author"
    assert record["subjects"] == "History, War"
    assert "indexable sentence here" in record["body"]


def test_script_and_style_never_reach_the_index(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "b.epub", body="visible prose")
    record = extract_book((str(path), ".epub", 100_000))
    assert "visible prose" in record["body"]
    assert "neverindexed" not in record["body"]
    assert "color: red" not in record["body"]


def test_text_budget_is_honoured(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "b.epub", body="word " * 5000)
    record = extract_book((str(path), ".epub", 400))
    assert len(record["body"]) <= 400


def test_a_corrupt_book_is_reported_not_raised(tmp_path: Path) -> None:
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"this is definitely not a zip archive")
    record = extract_book((str(broken), ".epub", 1000))
    assert record["ok"] is False
    assert record["error"]
    assert record["title"] == "broken"  # falls back to the filename


def test_pdf_metadata_and_text_are_extracted(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "d.pdf", "Gamma Report", "detonation velocity")
    record = extract_book((str(path), ".pdf", 100_000))
    assert record["ok"] is True
    assert record["title"] == "Gamma Report"
    assert record["pages"] == 1
    assert "detonation" in record["body"]


# ────────────────────────────── query parser ──────────────────────────────


@pytest.mark.parametrize(
    "query, expression, extensions",
    [
        ("sitchin", "sitchin*", []),
        ("lost book", "lost AND book*", []),
        ("ext:pdf ballistics", "ballistics*", [".pdf"]),
        ("ext:epub", "", [".epub"]),
        ('"exact phrase" more', '"exact phrase" AND more*', []),
        ("", "", []),
    ],
)
def test_query_parser(query: str, expression: str, extensions: list[str]) -> None:
    assert build_match_expression(query) == (expression, extensions)


@pytest.mark.parametrize("hostile", ['a OR b', 'x AND NOT y', '"unclosed', 'a* *b', '(())', 'NEAR(a b)'])
def test_hostile_queries_never_raise(hostile: str, library: Path, tmp_path: Path) -> None:
    """FTS5 operators typed by a reader must not become a syntax error."""
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, with_text=True, workers=1)
        index.search(library, hostile)      # must not raise
        index.count_matching(library, hostile)


# ──────────────────────────── index and search ────────────────────────────


def test_scan_counts_by_type(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        counts = index.scan(library, workers=1)
    assert counts.total == 3
    assert counts.epub == 2
    assert counts.pdf == 1
    assert counts.bytes_total > 0
    assert counts.with_text == 3


def test_search_matches_title_author_and_filename(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        assert [r.title for r in index.search(library, "Alpha")] == ["Alpha World"]
        assert [r.title for r in index.search(library, "Ben Author")] == ["Beta Days"]
        assert [r.title for r in index.search(library, "gamma")] == ["Gamma Report"]


def test_search_is_prefix_matched_while_typing(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        assert [r.title for r in index.search(library, "Alph")] == ["Alpha World"]


def test_topic_search_looks_inside_the_books(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        hits = index.search(library, "falconry", mode="content")
        assert [r.title for r in hits] == ["Beta Days"]
        assert hits[0].snippet  # a snippet comes back for display
        assert index.search(library, "falconry", mode="meta") == []


def test_subjects_are_searchable_as_topics(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        assert [r.title for r in index.search(library, "hydrology", mode="content")] == ["Alpha World"]
        assert [r.title for r in index.search(library, "Water", mode="meta")] == ["Alpha World"]


def test_extension_filter(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        assert index.count_matching(library, extensions=[".pdf"]) == 1
        assert index.count_matching(library, extensions=[".epub"]) == 2
        assert index.count_matching(library, "ext:pdf") == 1


def test_paging_returns_every_row_exactly_once(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        seen: list[str] = []
        for offset in range(0, 3, 2):
            seen.extend(row.path for row in index.search(library, limit=2, offset=offset))
    assert len(seen) == len(set(seen)) == 3


def test_rescan_is_incremental(library: Path, tmp_path: Path) -> None:
    database = tmp_path / "i.db"
    with LibraryIndex(database) as index:
        index.scan(library, workers=1)
        phases: list[tuple[str, int]] = []
        index.scan(library, workers=1, progress=lambda u: phases.append((u.phase, u.total)))
    # Nothing changed, so extraction never runs a second time.
    assert not [phase for phase, _ in phases if phase == "extract"]


def test_a_deleted_book_leaves_the_index(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        (library / "alpha.epub").unlink()
        counts = index.scan(library, workers=1)
        assert counts.total == 2
        assert index.search(library, "Alpha") == []


def test_an_edited_book_is_reindexed(library: Path, tmp_path: Path) -> None:
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(library, workers=1)
        make_epub(library / "alpha.epub", "Renamed Entirely", "Ada Writer", "new text")
        index.scan(library, workers=1)
        assert [r.title for r in index.search(library, "Renamed")] == ["Renamed Entirely"]
        assert index.search(library, "Alpha World") == []


def test_two_libraries_stay_separate(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    make_epub(first / "a.epub", "Only In One", "X", "aaa")
    make_epub(second / "b.epub", "Only In Two", "Y", "bbb")
    with LibraryIndex(tmp_path / "i.db") as index:
        index.scan(first, workers=1)
        index.scan(second, workers=1)
        assert index.counts(first).total == 1
        assert index.counts(second).total == 1
        assert [r.title for r in index.search(first, "Only")] == ["Only In One"]


def test_normalize_root_is_stable(tmp_path: Path) -> None:
    assert normalize_root(tmp_path) == normalize_root(str(tmp_path) + "//")


# ─────────────────────────── upgrading an old index ───────────────────────


#: The books table exactly as Lumen wrote it before generation marking existed.
LEGACY_SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '', subjects TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', pages INTEGER NOT NULL DEFAULT 0,
    has_text INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT ''
);
CREATE VIRTUAL TABLE books_fts USING fts5(
    title, author, name, subjects, publisher, book_id UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2");
CREATE VIRTUAL TABLE content_fts USING fts5(
    body, book_id UNINDEXED, tokenize = "unicode61 remove_diacritics 2");
"""


def make_legacy_index(database: Path, root: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(str(database))
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO books (root, path, name, ext, size, mtime_ns, title, author)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (normalize_root(root), str(root / "old.epub"), "old.epub", ".epub", 10, 20,
         "An Older Book", "Prior Author"),
    )
    connection.execute(
        "INSERT INTO books_fts (title, author, name, subjects, publisher, book_id)"
        " VALUES (?,?,?,?,?,?)", ("An Older Book", "Prior Author", "old.epub", "", "", 1),
    )
    connection.commit()
    connection.close()


def test_an_index_from_an_older_lumen_still_opens(tmp_path: Path) -> None:
    """Opening a pre-generation-marking database must not raise.

    It did.  ``SCHEMA`` builds an index over ``seen_gen``, and ``CREATE INDEX``
    naming a column the table does not have is a hard error - so the very first
    thing Lumen did on a real 7.79 GB index from the previous version was fail
    to start.  Nothing about a fresh database can catch that.
    """
    database = tmp_path / "legacy.db"
    make_legacy_index(database, tmp_path)
    with LibraryIndex(database) as index:
        assert [row.title for row in index.search(tmp_path, "Older")] == ["An Older Book"]


def test_upgrading_adds_the_generation_column(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    make_legacy_index(database, tmp_path)
    with LibraryIndex(database) as index:
        columns = {row["name"] for row in index.connection.execute("PRAGMA table_info(books)")}
        assert "seen_gen" in columns
        assert index.next_generation(tmp_path) == 1


def test_an_upgraded_index_can_be_swept_again(library: Path, tmp_path: Path) -> None:
    """The rows an older Lumen wrote must survive - and be re-swept correctly."""
    database = tmp_path / "legacy.db"
    make_legacy_index(database, library)
    with LibraryIndex(database) as index:
        counts = index.scan(library, workers=1)
    assert counts.total == 3          # the phantom legacy row is pruned, the real books stay
