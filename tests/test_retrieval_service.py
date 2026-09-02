from __future__ import annotations

import sqlite3
from pathlib import Path

from lumen_reader.library_index import LibraryIndex, SCHEMA
from lumen_reader.passage_builder import PassageBuilder
from lumen_reader.retrieval.contracts import RetrievalError
from lumen_reader.retrieval.service import RetrievalService
from lumen_reader.runtime_paths import RuntimePaths

from test_library_index import make_epub


def _library(tmp_path: Path) -> tuple[RuntimePaths, int]:
    root = tmp_path / "books"
    text = (
        "Frequency hopping protects a radio link against narrowband interference. "
        "The receiver follows a shared pseudo-random channel sequence. " * 40
    )
    make_epub(root / "radio systems.epub", "Radio Systems", "Ada Spectrum", text)
    data = tmp_path / "state"
    database = data / "library-index.db"
    with LibraryIndex(database) as index:
        index.scan(root, workers=1)
        book_id = int(index.connection.execute("SELECT id FROM books").fetchone()[0])
    return RuntimePaths(
        data_dir=data,
        state_file=data / "reader-state.json",
        index_file=database,
        logs_dir=data / "logs",
        cache_dir=data / "mcp-cache",
    ), book_id


def test_fast_scan_is_searchable_before_optional_passage_build(tmp_path: Path) -> None:
    paths, book_id = _library(tmp_path)
    service = RetrievalService(paths)
    status = service.status()
    assert status["health"] == "ready"
    assert status["corpus"]["books"] == 1
    assert status["corpus"]["passages"] == 0
    assert status["corpus"]["passage_index"] == "legacy_fallback"

    result = service.search("frequency hopping")
    assert result["hits"]
    hit = result["hits"][0]
    assert hit["book"]["id"] == book_id
    assert hit["precision"] == "book_level"
    assert hit["citation_id"] is None
    assert "Frequency hopping" in hit["excerpt"]

    semantic = service.search("frequency hopping", strategy="semantic")
    assert semantic["hits"]
    assert "wordnet-query-expansion" in semantic["backend"]["used"]
    assert semantic["backend"]["model_id"] == "Princeton WordNet 3.0"


def test_glob_and_exact_grep_are_root_scoped(tmp_path: Path) -> None:
    paths, book_id = _library(tmp_path)
    service = RetrievalService(paths)
    root_id = service.status()["roots"][0]["root_id"]
    globbed = service.glob("*radio*", target="filename", roots=[root_id])
    assert [hit["book_id"] for hit in globbed["hits"]] == [book_id]
    grepped = service.grep("narrowband interference", roots=[root_id])
    assert grepped["hits"]
    assert grepped["hits"][0]["match_ranges"]

    try:
        service.glob("*", roots=["root_not_authorized"])
    except RetrievalError as exception:
        assert exception.code == "ROOT_NOT_AUTHORIZED"
    else:
        raise AssertionError("unknown roots must fail closed")


def test_complete_builder_activates_complete_revision(tmp_path: Path) -> None:
    paths, _book_id = _library(tmp_path)
    summary = PassageBuilder(paths.index_file).build(force=True)
    assert summary.built == 1
    status = RetrievalService(paths).status()
    assert status["corpus"]["coverage"] == {"complete": 1}
    sections = RetrievalService(paths).glob("*c1*", include_sections=True)
    assert any(hit.get("match_kind") == "section" for hit in sections["hits"])


def test_related_author_and_subject_modes_apply_real_metadata_filters(tmp_path: Path) -> None:
    root = tmp_path / "books"
    make_epub(
        root / "seed.epub", "Seed Radio", "Ada Spectrum",
        "Frequency hopping and radio channel planning. " * 30,
        subjects=["Radio", "Engineering"],
    )
    make_epub(
        root / "same-author.epub", "Antenna Notes", "Ada Spectrum",
        "Antenna arrays and satellite receiver design. " * 30,
        subjects=["Antennas"],
    )
    make_epub(
        root / "same-subject.epub", "Propagation", "Grace Wave",
        "Propagation measurements across the ionosphere. " * 30,
        subjects=["Radio"],
    )
    make_epub(
        root / "unrelated.epub", "Botany", "Lin Green",
        "Plant taxonomy and forest ecology. " * 30,
        subjects=["Botany"],
    )
    data = tmp_path / "state"
    database = data / "library-index.db"
    with LibraryIndex(database) as index:
        index.scan(root, workers=1)
        seed_id = int(index.connection.execute(
            "SELECT id FROM books WHERE name='seed.epub'"
        ).fetchone()[0])
        same_author_id = int(index.connection.execute(
            "SELECT id FROM books WHERE name='same-author.epub'"
        ).fetchone()[0])
        same_subject_id = int(index.connection.execute(
            "SELECT id FROM books WHERE name='same-subject.epub'"
        ).fetchone()[0])
    paths = RuntimePaths(data, data / "reader-state.json", database, data / "logs", data / "cache")
    service = RetrievalService(paths)

    by_author = service.related(
        book_id=seed_id, relationship="same_author", exclude_same_book=True,
    )
    assert {hit["book"]["id"] for hit in by_author["hits"]} == {same_author_id}
    assert "metadata-author-identity" in by_author["backend"]["used"]

    by_subject = service.related(
        book_id=seed_id, relationship="same_subject", exclude_same_book=True,
    )
    assert {hit["book"]["id"] for hit in by_subject["hits"]} == {same_subject_id}
    assert "metadata-subject-overlap" in by_subject["backend"]["used"]


def test_adjacent_and_metadata_relationships_reject_ambiguous_seed_types(tmp_path: Path) -> None:
    paths, book_id = _library(tmp_path)
    service = RetrievalService(paths)
    for arguments in (
        {"book_id": book_id, "relationship": "adjacent"},
        {"text": "radio evidence", "relationship": "same_author"},
        {"text": "radio evidence", "relationship": "same_subject"},
    ):
        try:
            service.related(**arguments)
        except RetrievalError as exception:
            assert exception.code == "INVALID_ARGUMENT"
        else:
            raise AssertionError("ambiguous related-content seed must fail closed")


def test_legacy_catalog_supports_status_glob_book_and_search_before_migration(
    tmp_path: Path,
) -> None:
    data = tmp_path / "legacy"
    data.mkdir()
    database = data / "library-index.db"
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO books(root,path,name,ext,size,mtime_ns,title,author,publisher,"
        "language,subjects,description,pages,has_text,ok,error,seen_gen)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(tmp_path), str(tmp_path / "legacy-radio.epub"), "legacy-radio.epub", ".epub",
         100, 123, "Legacy Radio", "A. Author", "A Press", "en", "radio", "", 1,
         1, 1, "", 1),
    )
    book_id = int(connection.execute("SELECT id FROM books").fetchone()[0])
    connection.execute(
        "INSERT INTO books_fts(title,author,name,subjects,publisher,book_id) VALUES(?,?,?,?,?,?)",
        ("Legacy Radio", "A. Author", "legacy-radio.epub", "radio", "A Press", book_id),
    )
    connection.execute(
        "INSERT INTO content_fts(body,book_id) VALUES(?,?)",
        ("legacy radio frequency hopping evidence", book_id),
    )
    connection.commit()
    connection.close()
    paths = RuntimePaths(data, data / "reader-state.json", database, data / "logs", data / "cache")
    service = RetrievalService(paths)

    assert service.status()["catalog"]["passage_schema_version"] == 0
    assert service.glob("*radio*", target="filename")["hits"][0]["book_id"] == book_id
    book = service.get_book(book_id)
    assert book["book"]["title"] == "Legacy Radio"
    assert book["coverage"]["status"] == "metadata_only"
    search = service.search("frequency hopping")
    assert search["hits"][0]["precision"] == "book_level"
    related = service.related(book_id=book_id, relationship="conceptual", limit=1)
    assert related["hits"]
