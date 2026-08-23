# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Re-indexing a book must not read the whole full-text index to do it.

FTS5 will not index a column, so ``book_id`` in ``content_fts`` is UNINDEXED and
``DELETE FROM content_fts WHERE book_id = ?`` plans as a full scan of the entire
index.  On a real 10.4 GB library that was about ten seconds per book: a sweep
of 304 changed books committed seventeen and then sat there, every extractor
finished, apparently hung forever.

``fts_rowid`` is the index FTS5 would not give us.  These tests hold two lines
at once - that the deletes are now keyed by rowid, and that they still delete
the *right* rows, because a fast delete that strands stale text in the index
would be a worse bug than the slow one it replaced.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lumen_reader.library_index import (
    FTS_MAP_KEY,
    LibraryIndex,
    build_fts_map,
    drop_fts_rows,
    fts_map_ready,
    meta_get,
)

from test_library_index import make_epub


# ── helpers ───────────────────────────────────────────────────────────────


def _swept(database: Path, root: Path) -> LibraryIndex:
    index = LibraryIndex(database)
    index.scan(root, workers=1)
    return index


def _content_hits(index: LibraryIndex, root: Path, needle: str) -> list[str]:
    return [row.title for row in index.search(root, needle, mode="content")]


def _fts_rows(index: LibraryIndex, table: str) -> int:
    return int(index.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


@pytest.fixture()
def shelf(tmp_path: Path) -> Path:
    root = tmp_path / "books"
    make_epub(root / "alpha.epub", "Alpha World", "Ada Writer",
              "the alpha book discusses hydrology zebrafish")
    make_epub(root / "beta.epub", "Beta Days", "Ben Author",
              "beta explores medieval falconry aardvark")
    return root


# ── the map is built, and correct ─────────────────────────────────────────


def test_a_fresh_index_is_born_already_mapped(tmp_path: Path) -> None:
    """Nobody with nothing to migrate should pay for a migration pass."""
    with LibraryIndex(tmp_path / "fresh.db") as index:
        assert index.fts_map_ready() is True
        assert meta_get(index.connection, FTS_MAP_KEY) == "1"


def test_a_sweep_records_a_rowid_for_every_book(shelf: Path, tmp_path: Path) -> None:
    with _swept(tmp_path / "index.db", shelf) as index:
        rows = index.connection.execute(
            "SELECT book_id, meta_row, content_row FROM fts_rowid ORDER BY book_id"
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["meta_row"] is not None
            assert row["content_row"] is not None

        # every recorded rowid must actually be that book's row
        for row in rows:
            found = index.connection.execute(
                "SELECT book_id FROM content_fts WHERE rowid = ?", (row["content_row"],)
            ).fetchone()
            assert found is not None and found[0] == row["book_id"]


def test_the_map_points_at_the_right_book(shelf: Path, tmp_path: Path) -> None:
    """A rowid pointing at the wrong book would delete someone else's text."""
    with _swept(tmp_path / "index.db", shelf) as index:
        for row in index.connection.execute("SELECT book_id, content_row FROM fts_rowid"):
            body = index.connection.execute(
                "SELECT body FROM content_fts WHERE rowid = ?", (row["content_row"],)
            ).fetchone()[0]
            title = index.connection.execute(
                "SELECT title FROM books WHERE id = ?", (row["book_id"],)
            ).fetchone()[0]
            expected = "zebrafish" if title == "Alpha World" else "aardvark"
            assert expected in body


# ── deleting by rowid deletes the right thing ─────────────────────────────


def test_dropping_one_book_leaves_the_others_intact(shelf: Path, tmp_path: Path) -> None:
    with _swept(tmp_path / "index.db", shelf) as index:
        alpha = index.connection.execute(
            "SELECT id FROM books WHERE title = 'Alpha World'").fetchone()[0]

        drop_fts_rows(index.connection.cursor(), (alpha,))
        index.connection.commit()

        assert _content_hits(index, shelf, "zebrafish") == []
        assert _content_hits(index, shelf, "aardvark") == ["Beta Days"]
        assert index.connection.execute(
            "SELECT 1 FROM fts_rowid WHERE book_id = ?", (alpha,)).fetchone() is None


def test_dropping_nothing_is_harmless(shelf: Path, tmp_path: Path) -> None:
    with _swept(tmp_path / "index.db", shelf) as index:
        before = _fts_rows(index, "content_fts")
        drop_fts_rows(index.connection.cursor(), ())
        assert _fts_rows(index, "content_fts") == before


# ── the regression itself: re-sweeping must not strand old text ───────────


def test_re_indexing_a_changed_book_replaces_its_text(shelf: Path, tmp_path: Path) -> None:
    """The whole point.  Old body gone, new body searchable, no duplicates."""
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        assert _content_hits(index, shelf, "zebrafish") == ["Alpha World"]
        assert _fts_rows(index, "content_fts") == 2

    # rewrite the book with different prose, then sweep again
    make_epub(shelf / "alpha.epub", "Alpha World", "Ada Writer",
              "the alpha book now discusses cartography pangolin")

    with LibraryIndex(database) as index:
        index.scan(shelf, workers=1)
        assert _content_hits(index, shelf, "pangolin") == ["Alpha World"]
        assert _content_hits(index, shelf, "zebrafish") == [], "stale text survived the re-index"
        assert _content_hits(index, shelf, "aardvark") == ["Beta Days"]
        # one row per book: a delete that missed would leave two
        assert _fts_rows(index, "content_fts") == 2
        assert _fts_rows(index, "books_fts") == 2
        assert int(index.connection.execute(
            "SELECT count(*) FROM fts_rowid").fetchone()[0]) == 2


def test_sweeping_twice_without_changes_keeps_one_row_each(shelf: Path, tmp_path: Path) -> None:
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        pass
    with LibraryIndex(database) as index:
        index.scan(shelf, workers=1)
        index.scan(shelf, workers=1)
        assert _fts_rows(index, "content_fts") == 2
        assert _content_hits(index, shelf, "zebrafish") == ["Alpha World"]


def test_a_book_that_leaves_the_shelf_takes_its_text_with_it(
    shelf: Path, tmp_path: Path
) -> None:
    """prune_generation goes through the map too."""
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        pass
    (shelf / "beta.epub").unlink()
    with LibraryIndex(database) as index:
        index.scan(shelf, workers=1)
        assert _content_hits(index, shelf, "aardvark") == []
        assert _content_hits(index, shelf, "zebrafish") == ["Alpha World"]
        assert _fts_rows(index, "content_fts") == 1
        assert int(index.connection.execute(
            "SELECT count(*) FROM fts_rowid").fetchone()[0]) == 1


# ── upgrading an index written before the map existed ─────────────────────


def _unmap(database: Path) -> None:
    """Return a database to the state a pre-map Lumen would have left it in."""
    connection = sqlite3.connect(str(database))
    connection.execute("DELETE FROM fts_rowid")
    connection.execute("DELETE FROM index_meta WHERE key = ?", (FTS_MAP_KEY,))
    connection.commit()
    connection.close()


def test_an_unmapped_index_is_not_claimed_to_be_ready(shelf: Path, tmp_path: Path) -> None:
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        pass
    _unmap(database)
    connection = sqlite3.connect(str(database))
    try:
        assert fts_map_ready(connection) is False
    finally:
        connection.close()


def test_building_the_map_recovers_every_rowid(shelf: Path, tmp_path: Path) -> None:
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        expected = {
            int(row["book_id"]): (row["meta_row"], row["content_row"])
            for row in index.connection.execute(
                "SELECT book_id, meta_row, content_row FROM fts_rowid")
        }
    _unmap(database)

    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        said: list[str] = []
        report = build_fts_map(connection, said.append)
        assert report["mapped"] == 2
        assert report["orphans"] == 0
        assert fts_map_ready(connection) is True
        assert said, "a migration that costs a full pass must say it is happening"

        rebuilt = {
            int(row["book_id"]): (row["meta_row"], row["content_row"])
            for row in connection.execute(
                "SELECT book_id, meta_row, content_row FROM fts_rowid")
        }
        assert rebuilt == expected
    finally:
        connection.close()


def test_an_unmapped_book_still_loses_its_text(shelf: Path, tmp_path: Path) -> None:
    """The fallback must stay correct, however slow it is.

    A book with no mapping is deleted by the old scanning query rather than
    skipped - stale text answering searches would be worse than a slow sweep.
    """
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        alpha = index.connection.execute(
            "SELECT id FROM books WHERE title = 'Alpha World'").fetchone()[0]
        index.connection.execute("DELETE FROM fts_rowid WHERE book_id = ?", (alpha,))
        index.connection.commit()

        drop_fts_rows(index.connection.cursor(), (alpha,))
        index.connection.commit()

        assert _content_hits(index, shelf, "zebrafish") == []
        assert _content_hits(index, shelf, "aardvark") == ["Beta Days"]


def test_sweeping_an_unmapped_index_builds_the_map_first(shelf: Path, tmp_path: Path) -> None:
    """The upgrade happens once, at the start of the first sweep after it."""
    database = tmp_path / "index.db"
    with _swept(database, shelf) as index:
        pass
    _unmap(database)

    with LibraryIndex(database) as index:
        assert index.fts_map_ready() is False
        index.scan(shelf, workers=1)
        assert index.fts_map_ready() is True
        assert int(index.connection.execute(
            "SELECT count(*) FROM fts_rowid").fetchone()[0]) == 2


# ── deletes are keyed by rowid, not by a scan ─────────────────────────────


def test_the_delete_uses_a_rowid_lookup_not_a_column_scan(shelf: Path, tmp_path: Path) -> None:
    """Pin the plan, so nobody quietly reintroduces the scanning delete.

    SQLite reports both FTS5 paths as ``SCAN``, but the rowid form carries the
    ``:=`` constraint marker and a far lower cost estimate - that is what a
    rowid seek looks like from EXPLAIN QUERY PLAN.
    """
    with _swept(tmp_path / "index.db", shelf) as index:
        by_rowid = index.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM content_fts WHERE rowid = 1").fetchall()
        by_column = index.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM content_fts WHERE book_id = 1").fetchall()

        assert "INDEX 0:=" in by_rowid[0]["detail"]
        assert by_column[0]["detail"].endswith("INDEX 0:")
        assert int(by_rowid[0][2]) < int(by_column[0][2]), (
            "the rowid path must be the cheaper one")
