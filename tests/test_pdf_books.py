from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import pymupdf
import pytest
from bs4 import BeautifulSoup
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas

from lumen_reader.pdf_book import PdfBook, PdfPasswordRequired
from lumen_reader.speed_reader import SpeedReadingDocument
from lumen_reader.storage import ReaderStore
from lumen_reader.ui import (
    READER_INTERACTION_GUARD_SCRIPT,
    SELECTION_CONTEXT_SCRIPT,
    generic_document_label,
    library_books,
)


def make_pdf(path: Path) -> Path:
    document = canvas.Canvas(str(path), pagesize=letter)
    document.setTitle("Lumen PDF Definition Fixture")
    document.setAuthor("Lumen Test Lab")
    document.setSubject("Faithful PDF rendering and selectable phrase definitions")

    document.bookmarkPage("intro")
    document.addOutlineEntry("Introduction", "intro", level=0)
    document.setFillColor(colors.HexColor("#0F766E"))
    document.rect(0, 720, 612, 72, fill=1, stroke=0)
    document.setFillColor(colors.white)
    document.setFont("Helvetica-Bold", 22)
    document.drawString(48, 752, "PDF reading, faithfully rendered")
    document.setFillColor(colors.black)
    document.setFont("Times-Roman", 15)
    document.drawString(54, 670, "China's early copykittens were playful copycat internet companies.")
    document.drawString(54, 640, "Select copykittens to define it with the surrounding passage.")
    document.setFillColor(colors.HexColor("#7C3AED"))
    document.roundRect(54, 520, 240, 72, 10, fill=1, stroke=0)
    document.setFillColor(colors.white)
    document.drawString(76, 551, "Original vector color and layout")
    document.showPage()

    document.bookmarkPage("phrases")
    document.addOutlineEntry("Phrase definitions", "phrases", level=0)
    document.setFont("Helvetica-Bold", 24)
    document.setFillColor(colors.HexColor("#1D4ED8"))
    document.drawString(58, 710, "Laughing Out Loud")
    document.setFont("Times-Roman", 15)
    document.setFillColor(colors.black)
    document.drawString(58, 670, "The complete selected phrase must reach the definition card.")
    document.showPage()

    document.setPageSize(landscape(letter))
    document.bookmarkPage("landscape")
    document.addOutlineEntry("Landscape evidence", "landscape", level=1)
    document.setFillColor(colors.HexColor("#C2410C"))
    document.rect(0, 0, 140, 612, fill=1, stroke=0)
    document.setFillColor(colors.black)
    document.setFont("Helvetica", 18)
    document.drawString(180, 500, "Landscape PDF pages remain complete and selectable.")
    document.save()
    return path


@pytest.fixture()
def pdf_path(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / "definition-fixture.pdf")


def test_pdf_metadata_outline_pages_and_cover(pdf_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        assert book.document_type == "PDF"
        assert book.metadata.title == "Lumen PDF Definition Fixture"
        assert book.metadata.authors == ["Lumen Test Lab"]
        assert len(book.chapters) == 3
        assert [entry.title for entry in book.toc[:2]] == [
            "Introduction",
            "Phrase definitions",
        ]
        assert book.toc[1].children[0].title == "Landscape evidence"
        cover = book.cover_path
        assert cover is not None and cover.is_file()
        assert cover.is_relative_to(book.extract_dir)
        rendered_cover = pymupdf.Pixmap(cover)
        assert rendered_cover.width > 1200
        assert rendered_cover.height > 1500


def test_every_pdf_page_renders_with_original_image_and_selectable_text(pdf_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        for index, chapter in enumerate(book.chapters):
            rendered = book.chapter_html(index, theme="dark", font_size=32)
            soup = BeautifulSoup(rendered, "html.parser")
            assert soup.title is not None
            assert chapter.title in soup.title.get_text()
            assert soup.select_one("img.pdf-page-image") is not None
            assert soup.select_one(".pdf-text-layer") is not None
            assert soup.select(".pdf-word")
            assert soup.select_one(".pdf-text-line[data-context]") is not None
            assert "Content-Security-Policy" in rendered
            assert "default-src 'none'" in rendered
            assert "color: transparent" in rendered
            assert "font-size: 32px" not in rendered
            scripts = soup.find_all("script")
            assert len(scripts) == 1
            assert "ResizeObserver" in scripts[0].get_text()


def test_pdf_selection_layer_preserves_word_and_complete_phrase_context(pdf_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        first = BeautifulSoup(book.chapter_html(0), "html.parser")
        words = [node.get_text(strip=True) for node in first.select(".pdf-word")]
        assert "copykittens" in words
        word = next(node for node in first.select(".pdf-word") if node.get_text(strip=True) == "copykittens")
        context = word.find_parent(class_="pdf-text-line")["data-context"]
        assert "early copykittens" in context
        assert "surrounding passage" in context

        phrase_page = BeautifulSoup(book.chapter_html(1), "html.parser")
        phrase_words = [node.get_text(strip=True) for node in phrase_page.select(".pdf-word")]
        start = phrase_words.index("Laughing")
        assert phrase_words[start : start + 3] == ["Laughing", "Out", "Loud"]
        phrase_context = phrase_page.select_one(".pdf-text-line")["data-context"]
        assert "Laughing Out Loud" in phrase_context
        assert ".pdf-text-line" in SELECTION_CONTEXT_SCRIPT


def test_pdf_search_and_page_url_mapping_are_case_insensitive(pdf_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        lower = book.search("laughing out loud")
        upper = book.search("LAUGHING OUT LOUD")
        assert lower
        assert [(item.chapter_index, item.match_count) for item in lower] == [
            (item.chapter_index, item.match_count) for item in upper
        ]
        index, fragment = book.chapter_index_for_url(
            (book.extract_dir / "page-2.html").as_uri() + "#selection"
        )
        assert index == 1
        assert fragment == "selection"


def test_pdf_text_layer_builds_complete_speed_reading_document(pdf_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        document = SpeedReadingDocument.from_book(book)
        assert len(document.chapters) == 3
        assert document.total_words > 20
        assert "copykittens" in document.chapters[0].words
        assert document.chapters[1].words[:3] == ["Laughing", "Out", "Loud"]


def test_rotated_pdf_page_keeps_render_and_text_coordinates_aligned(
    pdf_path: Path, tmp_path: Path
) -> None:
    rotated = tmp_path / "rotated.pdf"
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index == 2:
            page.rotate(90)
        writer.add_page(page)
    with rotated.open("wb") as stream:
        writer.write(stream)

    with PdfBook(rotated) as book:
        rendered = book.chapter_html(2)
        soup = BeautifulSoup(rendered, "html.parser")
        target = next(
            node
            for node in soup.select(".pdf-word")
            if node.get_text(strip=True) == "Landscape"
        )
        assert target.get("data-width")
        image = book._render_page(2)[0]
        pixmap = pymupdf.Pixmap(image)
        assert pixmap.height > pixmap.width
        assert "Landscape PDF pages remain complete" in book.text_for_chapter(2)


def test_password_protected_pdf_prompts_for_a_valid_password(pdf_path: Path, tmp_path: Path) -> None:
    protected = tmp_path / "protected.pdf"
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata(reader.metadata or {})
    writer.encrypt("lumen-secret")
    with protected.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(PdfPasswordRequired):
        PdfBook(protected)
    with PdfBook(protected, password="lumen-secret") as book:
        assert len(book.chapters) == 3
        assert "copykittens" in book.text_for_chapter(0)


def test_library_discovers_pdf_and_epub_files_only(tmp_path: Path) -> None:
    (tmp_path / "one.epub").touch()
    (tmp_path / "two.PDF").touch()
    (tmp_path / "ignored.txt").touch()
    assert [path.name for path in library_books(tmp_path)] == ["one.epub", "two.PDF"]
    assert generic_document_label(tmp_path / "two.PDF") == "PDF document"
    assert generic_document_label(tmp_path / "one.epub") == "EPUB book"


def test_pdf_last_page_and_within_page_position_persist(pdf_path: Path, tmp_path: Path) -> None:
    with PdfBook(pdf_path) as book:
        key = book.key
    state_path = tmp_path / "reader-state.json"
    store = ReaderStore(state_path)
    store.book_state(key).update({"chapter": 2, "scroll": 0.375})
    store.save()

    restored = ReaderStore(state_path).book_state(key)
    assert restored["chapter"] == 2
    assert restored["scroll"] == 0.375


def _wait_for_page(page: QWebEnginePage, html_text: str, base_url: QUrl) -> None:
    loop = QEventLoop()
    completed: list[bool] = []
    page.loadFinished.connect(lambda ok: (completed.append(ok), loop.quit()))
    page.setHtml(html_text, base_url)
    QTimer.singleShot(10_000, loop.quit)
    loop.exec()
    assert completed == [True]


def _run_javascript(page: QWebEnginePage, script: str) -> object:
    loop = QEventLoop()
    result: list[object] = []
    page.runJavaScript(script, lambda value: (result.append(value), loop.quit()))
    QTimer.singleShot(5_000, loop.quit)
    loop.exec()
    assert result, "Qt WebEngine did not return a JavaScript result"
    return result[0]


def test_live_pdf_phrase_selection_reinserts_spaces_before_definition(
    pdf_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    with PdfBook(pdf_path) as book:
        page = QWebEnginePage()
        _wait_for_page(
            page,
            book.chapter_html(0),
            QUrl(book.chapter_base_url(0)),
        )
        assert _run_javascript(page, READER_INTERACTION_GUARD_SCRIPT) is True
        native_and_fixed = _run_javascript(
            page,
            """
            (() => {
              const words = [...document.querySelectorAll('.pdf-word')];
              const first = words.findIndex((node) => node.textContent.trim() === 'playful');
              const last = first + 3;
              const range = document.createRange();
              range.setStart(words[first].firstChild, 0);
              range.setEnd(words[last].firstChild, words[last].firstChild.length);
              const selection = window.getSelection();
              selection.removeAllRanges();
              selection.addRange(range);
              return JSON.stringify({
                native: selection.toString(),
                fixed: window.__lumenSelectedText()
              });
            })()
            """,
        )
        values = json.loads(str(native_and_fixed))
        assert values["fixed"] == "playful copycat internet companies."
        assert values["fixed"] != values["native"]

        payload = json.loads(str(_run_javascript(page, SELECTION_CONTEXT_SCRIPT)))
        assert payload["selection"] == "playful copycat internet companies."
        assert "early copykittens" in payload["context"]
        page.deleteLater()
        app.processEvents()
