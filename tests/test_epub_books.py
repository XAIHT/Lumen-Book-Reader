from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from lumen_reader.book import EpubBook
from lumen_reader.speed_reader import SpeedReadingDocument


# Keep the regression suite deterministic even when the working library grows.
# These two fixtures exercise different internal EPUB directory layouts.
BOOKS = sorted(Path(__file__).parents[1].glob("AI Superpowers*.epub"))


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_repository_epubs_parse_completely(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        assert book.metadata.title == "AI Superpowers"
        assert "Kai-Fu Lee" in book.metadata.authors
        assert book.metadata.identifier
        assert len(book.chapters) >= 20
        assert len(book.toc) >= 10
        assert book.cover_path is not None
        assert book.cover_path.is_file()
        assert book.cover_path.is_relative_to(book.extract_dir)


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_every_spine_document_can_be_rendered(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        for index, chapter in enumerate(book.chapters):
            rendered = book.chapter_html(index, theme="sepia", font_size=22)
            soup = BeautifulSoup(rendered, "html.parser")
            assert soup.title is not None
            assert chapter.title in soup.title.get_text()
            assert soup.find("script") is None
            assert "Content-Security-Policy" in rendered
            assert "font-size: 22px" in rendered
            assert len(soup.get_text(" ", strip=True)) > 0


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_rendered_links_cannot_take_focus_or_smooth_scroll(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        rendered = "\n".join(book.chapter_html(index) for index in range(len(book.chapters)))
        soup = BeautifulSoup(rendered, "html.parser")
        links = soup.find_all("a")
        assert links, "The EPUB fixture should exercise real hyperlinks"
        assert all(link.get("tabindex") == "-1" for link in links)
        assert all(link.get("draggable") == "false" for link in links)
        assert "scroll-behavior: auto" in rendered
        assert "a:focus, a:focus-visible" in rendered


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_toc_entries_resolve_to_spine(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        resolved: list[int] = []

        def visit(entries: list) -> None:
            for entry in entries:
                if entry.chapter_index is not None:
                    resolved.append(entry.chapter_index)
                visit(entry.children)

        visit(book.toc)
        assert len(resolved) >= 10
        assert all(0 <= index < len(book.chapters) for index in resolved)
        assert "Introduction" in {book.chapters[index].title for index in resolved}


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_full_text_search_is_case_insensitive(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        lower = book.search("artificial intelligence")
        upper = book.search("ARTIFICIAL INTELLIGENCE")
        assert lower
        assert [(item.chapter_index, item.match_count) for item in lower] == [
            (item.chapter_index, item.match_count) for item in upper
        ]
        assert all(item.excerpt for item in lower)


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_epub_spine_builds_complete_speed_reading_document(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        document = SpeedReadingDocument.from_book(book)
        assert len(document.chapters) == len(book.chapters)
        assert document.total_words > 1_000
        assert all(chapter.title for chapter in document.chapters)


@pytest.mark.parametrize("epub_path", BOOKS, ids=lambda path: path.name[:45])
def test_local_chapter_links_map_back_to_the_spine(epub_path: Path) -> None:
    with EpubBook(epub_path) as book:
        target = book.extract_dir.joinpath(*book.chapters[5].href.split("/"))
        index, fragment = book.chapter_index_for_url(target.as_uri() + "#example")
        assert index == 5
        assert fragment == "example"


def test_repository_contains_test_books() -> None:
    assert len(BOOKS) >= 2, "The repository EPUB fixtures are missing"
