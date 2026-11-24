# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""A book is shown under its title, never under the trace of the tool that made it.

Every junk title here was found on a real 14,126-book shelf, where 706 of them
sorted to the top of the catalog in place of the books they belonged to - the
first row read ``0071613757.pdf`` for *Hacking Exposed 6*.  Every kept title is
there too, because the opposite failure is worse: throwing away ``1984`` or
``22/11/63`` because it looks like a number.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lumen_reader.book import EpubBook
from lumen_reader.book_titles import (
    TITLE_POLICY_KEY,
    TITLE_POLICY_VERSION,
    choose_title,
    title_problem,
)
from lumen_reader.library_index import FTS_MAP_KEY, LibraryIndex, extract_book, meta_get

from test_library_index import make_epub, make_pdf


@pytest.mark.parametrize(("title", "file_name"), [
    ("0071613757.pdf", "Hacking Exposed 6 - Network Security Secrets and Solutions (2009).pdf"),
    ("Microsoft Word - 43A0E3A9-2D54-20F7A6.doc", "20 Years Progress in Explosives.pdf"),
    ("fasc0b.dvi", "7.1.1 Boolean basics.pdf"),
    ("Rounded Text.qxd (Page 1)", "Digital Circuit Analysis.pdf"),
    ("file:///F|/rah/Ben%20Bova/Bova,%20Ben%20-%20Jupiter.txt", "Ben Bova - Jupiter.pdf"),
    (r"C:\Documents and Settings\Owner\My Documents\psionic", "The Psionic Grimoire.pdf"),
    ("Microsoft Word - Chapter 1", "Yadava R. Advanced Engineering Mathematics 2016.pdf"),
    ("PII: S0040-6031(01)00805-X", "A review of energetic materials synthesis.pdf"),
    ("605e39e31b5f4965e0bb354251b0b6f2", "Bremaud P. Probability theory 2020.pdf"),
    ("JCR809955C 288..289", "tetrameric AP.pdf"),
    ("Untitled-2", "HEL 5.56mm Supressor for M16A1.pdf"),
    ("THIS CD PRODUCED BY", "Blackpowder.pdf"),
    ("Неизвестное название", "Anderson D. Introduction to probability 2018.pdf"),
    ("Título", "Mision del bibliotecario - Jose Ortega y Gasset.epub"),
    ("9781119148326", "Mann P. Introductory Statistics 9ed 2016.pdf"),
    ("44", "V1 Ch44 1962-05-04 The Laws of Thermodynamics.pdf"),
    ("PH1", "PHERMEX Data volume I.pdf"),
    ("ACmain", "Active Calculus.pdf"),
    ("", "Plain File Name.pdf"),
])
def test_a_tool_trace_gives_way_to_the_file_name(title: str, file_name: str) -> None:
    assert title_problem(title, file_name) is not None
    assert choose_title(title, file_name) == Path(file_name).stem


@pytest.mark.parametrize(("title", "file_name"), [
    ("1984", "1984 - George Orwell.epub"),
    ("22/11/63", "22 - 11 - 63 - Stephen King.epub"),
    ("It", "It_ A Novel - Stephen King.epub"),
    ("Eón", "Eon - Greg Bear.epub"),
    ("Q", "Q - Luther Blissett (Wu Ming).epub"),
    ("Olympos", "Olympo - Dan Simmons.epub"),
    ("Creative Projects for Rust Programmers", "9781789346220.pdf"),
    ("Software Defined Radio: Theory and Practice", "Reyland J. Software Defined Radio 2024.pdf"),
    ("Unknown publication", "Unknown.pdf"),
    ("Node.js Design Patterns", "Casciaro M. Node.js Design Patterns 2020.pdf"),
])
def test_a_real_title_is_kept(title: str, file_name: str) -> None:
    assert title_problem(title, file_name) is None
    assert choose_title(title, file_name) == title


def test_a_lone_unrelated_word_is_a_job_name_only_in_a_pdf() -> None:
    # PDF /Title is written by the producing tool; EPUB dc:title by the publisher.
    assert choose_title("Olympos", "Olympo - Dan Simmons.pdf") == "Olympo - Dan Simmons"
    assert choose_title("Olympos", "Olympo - Dan Simmons.epub") == "Olympos"


def test_a_sweep_files_a_book_under_its_name_when_its_title_is_a_file_name(
    tmp_path: Path,
) -> None:
    path = make_pdf(tmp_path / "Hacking Exposed 6.pdf", "0071613757.pdf")
    assert extract_book((str(path), ".pdf", 1000))["title"] == "Hacking Exposed 6"


def test_the_reader_shows_the_same_title_as_the_shelf(tmp_path: Path) -> None:
    from lumen_reader.pdf_book import PdfBook

    pdf = make_pdf(tmp_path / "Hacking Exposed 6.pdf", "0071613757.pdf")
    with PdfBook(pdf) as book:
        assert book.metadata.title == "Hacking Exposed 6"
    epub = make_epub(tmp_path / "Mision del bibliotecario.epub", "Título")
    with EpubBook(epub) as book:
        assert book.metadata.title == "Mision del bibliotecario"


def _as_an_older_lumen_left_it(database: Path, title: str, *, unmapped: bool) -> None:
    """Store the junk title the way every Lumen before the title policy did."""
    connection = sqlite3.connect(str(database))
    connection.execute("UPDATE books SET title = ?", (title,))
    connection.execute("UPDATE books_fts SET title = ?", (title,))
    connection.execute("DELETE FROM index_meta WHERE key = ?", (TITLE_POLICY_KEY,))
    if unmapped:
        connection.execute("DELETE FROM fts_rowid")
        connection.execute("DELETE FROM index_meta WHERE key = ?", (FTS_MAP_KEY,))
    connection.commit()
    connection.close()


@pytest.mark.parametrize("unmapped", [False, True], ids=["fts-mapped", "fts-unmapped"])
def test_titles_already_on_the_shelf_are_corrected_once(tmp_path: Path, unmapped: bool) -> None:
    # A sweep never re-reads an unchanged book, so only the stored catalog can
    # be corrected - and it must be, in the title search index as well.
    root = tmp_path / "books"
    make_pdf(root / "Hacking Exposed 6.pdf", "Hacking Exposed 6", "network security secrets")
    make_epub(root / "1984 - George Orwell.epub", "1984", "George Orwell")
    database = tmp_path / "index.db"
    with LibraryIndex(database) as index:
        index.scan(root, workers=1)
    _as_an_older_lumen_left_it(database, "0071613757.pdf", unmapped=unmapped)

    with LibraryIndex(database) as index:
        titles = sorted(row.title for row in index.search(root, sort="title"))
        assert titles == ["1984 - George Orwell", "Hacking Exposed 6"]
        assert index.search(root, "0071613757") == []
        assert [row.title for row in index.search(root, "orwell")] == ["1984 - George Orwell"]
        assert meta_get(index.connection, TITLE_POLICY_KEY) == str(TITLE_POLICY_VERSION)
        assert index._apply_title_policy() == 0
