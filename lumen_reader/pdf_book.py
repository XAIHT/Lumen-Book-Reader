"""Faithful PDF rendering with a selectable, definition-ready text layer."""

from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pymupdf

from .models import BookMetadata, Chapter, SearchResult, TocEntry


class PdfError(ValueError):
    """Raised when a PDF cannot be opened or rendered."""


class PdfPasswordRequired(PdfError):
    """Raised when a PDF requires a password that was not accepted."""


_SPACE_RE = re.compile(r"\s+")
_MAX_RENDER_EDGE = 4096
_TARGET_RENDER_SCALE = 2.25
_CONTEXT_RADIUS = 4


def _clean_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


class PdfBook:
    """A PDF exposed through the same page-oriented interface as an EPUB.

    Every original page is rasterized by MuPDF, preserving its layout, colors,
    images, vector art, and annotations. A transparent text layer made from
    MuPDF word coordinates sits above that image so Lumen's existing selection
    and definition mechanics continue to work for words and phrases.
    """

    document_type = "PDF"

    def __init__(self, path: str | os.PathLike[str], password: str = ""):
        self.path = Path(path).expanduser().resolve()
        self._temporary = tempfile.TemporaryDirectory(prefix="lumen_pdf_")
        self.extract_dir = Path(self._temporary.name)
        self.metadata = BookMetadata(title=self.path.stem)
        self.chapters: list[Chapter] = []
        self.toc: list[TocEntry] = []
        self._document: pymupdf.Document | None = None
        self._text_cache: dict[int, str] = {}
        self._word_cache: dict[int, list[tuple[float, float, float, float, str, int, int]]] = {}
        self._closed = False
        self._ocr_available = shutil.which("tesseract") is not None

        try:
            self._open(password)
        except Exception:
            self.close()
            raise

    @property
    def key(self) -> str:
        stat = self.path.stat()
        signature = f"{self.path}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha256(signature.encode("utf-8", "surrogatepass")).hexdigest()[:24]

    @property
    def cover_path(self) -> Path | None:
        if not self.chapters:
            return None
        try:
            return self._render_page(0)[0]
        except PdfError:
            return None

    @property
    def uses_ocr(self) -> bool:
        """Whether scanned pages can use the optional Tesseract fallback."""
        return self._ocr_available

    def __enter__(self) -> "PdfBook":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._document is not None:
            self._document.close()
            self._document = None
        self._temporary.cleanup()
        self._closed = True

    def _open(self, password: str) -> None:
        if not self.path.is_file():
            raise PdfError(f"Document not found: {self.path}")
        if self.path.suffix.lower() != ".pdf":
            raise PdfError("The selected file is not a .pdf file.")
        try:
            document = pymupdf.open(self.path)
        except Exception as exc:
            raise PdfError("The selected file is not a readable PDF document.") from exc
        self._document = document
        if document.needs_pass and not document.authenticate(password):
            raise PdfPasswordRequired("This PDF is password protected.")
        if document.page_count < 1:
            raise PdfError("This PDF does not contain any pages.")

        source = document.metadata or {}
        title = _clean_text(source.get("title")) or self.path.stem
        author = _clean_text(source.get("author"))
        self.metadata = BookMetadata(
            title=title,
            authors=[author] if author else [],
            language=_clean_text(source.get("language")),
            publisher=_clean_text(source.get("producer")),
            description=_clean_text(source.get("subject")),
            identifier=_clean_text(source.get("keywords")) or self.key,
        )
        self.chapters = [
            Chapter(
                id=f"pdf-page-{index + 1}",
                href=f"page-{index + 1}.html",
                media_type="application/pdf",
                title=f"Page {index + 1}",
            )
            for index in range(document.page_count)
        ]
        self.toc = self._build_toc()

    def _require_document(self) -> pymupdf.Document:
        if self._document is None or self._closed:
            raise PdfError("This PDF is already closed.")
        return self._document

    def _build_toc(self) -> list[TocEntry]:
        document = self._require_document()
        roots: list[TocEntry] = []
        stack: list[tuple[int, list[TocEntry]]] = [(0, roots)]
        try:
            outline = document.get_toc(simple=True)
        except Exception:
            outline = []
        for raw in outline:
            if len(raw) < 3:
                continue
            level, raw_title, raw_page = int(raw[0]), str(raw[1]), int(raw[2])
            if raw_page < 1 or raw_page > document.page_count:
                continue
            level = max(1, level)
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1]
            title = _clean_text(raw_title) or f"Page {raw_page}"
            entry = TocEntry(title, f"page-{raw_page}.html", raw_page - 1)
            parent.append(entry)
            self.chapters[raw_page - 1].title = title
            stack.append((level, entry.children))
        if roots:
            return roots

        if document.page_count <= 60:
            return [
                TocEntry(chapter.title, chapter.href, index)
                for index, chapter in enumerate(self.chapters)
            ]
        grouped: list[TocEntry] = []
        for start in range(0, document.page_count, 25):
            end = min(start + 25, document.page_count)
            children = [
                TocEntry(self.chapters[index].title, self.chapters[index].href, index)
                for index in range(start, end)
            ]
            grouped.append(
                TocEntry(
                    f"Pages {start + 1}–{end}",
                    self.chapters[start].href,
                    start,
                    children,
                )
            )
        return grouped

    def chapter_base_url(self, index: int) -> str:
        if index < 0 or index >= len(self.chapters):
            raise PdfError("PDF page is outside the document.")
        return self.extract_dir.as_uri() + "/"

    def _render_page(self, index: int) -> tuple[Path, float, float, float]:
        document = self._require_document()
        if index < 0 or index >= document.page_count:
            raise PdfError("PDF page is outside the document.")
        page = document.load_page(index)
        width, height = float(page.rect.width), float(page.rect.height)
        longest_edge = max(width, height, 1.0)
        scale = min(_TARGET_RENDER_SCALE, _MAX_RENDER_EDGE / longest_edge)
        scale = max(1.0, scale)
        destination = self.extract_dir / f"page-{index + 1}.png"
        if not destination.is_file():
            try:
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                    annots=True,
                )
                pixmap.save(destination)
            except Exception as exc:
                raise PdfError(f"Could not render PDF page {index + 1}.") from exc
        return destination, width, height, scale

    def _words_for_page(
        self, index: int
    ) -> list[tuple[float, float, float, float, str, int, int]]:
        if index in self._word_cache:
            return self._word_cache[index]
        document = self._require_document()
        page = document.load_page(index)
        try:
            raw_words = page.get_text("words", sort=True)
        except Exception:
            raw_words = []
        if not raw_words and self._ocr_available:
            try:
                text_page = page.get_textpage_ocr(language="eng", dpi=150, full=True)
                raw_words = page.get_text("words", textpage=text_page, sort=True)
            except Exception:
                raw_words = []

        result: list[tuple[float, float, float, float, str, int, int]] = []
        rotation = page.rotation_matrix if page.rotation else None
        page_rect = page.rect
        for raw in raw_words:
            if len(raw) < 8:
                continue
            text = _clean_text(str(raw[4]))
            if not text:
                continue
            rect = pymupdf.Rect(float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
            if rotation is not None:
                rect = rect * rotation
            x0 = max(0.0, rect.x0 - page_rect.x0)
            y0 = max(0.0, rect.y0 - page_rect.y0)
            x1 = min(float(page_rect.width), rect.x1 - page_rect.x0)
            y1 = min(float(page_rect.height), rect.y1 - page_rect.y0)
            if x1 <= x0 or y1 <= y0:
                continue
            result.append((x0, y0, x1, y1, text, int(raw[5]), int(raw[6])))
        self._word_cache[index] = result
        return result

    def _line_groups(
        self, index: int
    ) -> list[list[tuple[float, float, float, float, str, int, int]]]:
        groups: list[list[tuple[float, float, float, float, str, int, int]]] = []
        current_key: tuple[int, int] | None = None
        for word in self._words_for_page(index):
            key = (word[5], word[6])
            if key != current_key:
                groups.append([])
                current_key = key
            groups[-1].append(word)
        return groups

    def _text_layer(self, index: int) -> str:
        lines = self._line_groups(index)
        line_text = [" ".join(word[4] for word in line) for line in lines]
        fragments: list[str] = []
        for line_index, line in enumerate(lines):
            start = max(0, line_index - _CONTEXT_RADIUS)
            end = min(len(lines), line_index + _CONTEXT_RADIUS + 1)
            context = _clean_text(" ".join(line_text[start:end]))
            fragments.append(
                '<span class="pdf-text-line" data-context="'
                + html.escape(context, quote=True)
                + '">'
            )
            for x0, y0, x1, y1, text, _block, _line in line:
                height = max(1.0, y1 - y0)
                style = (
                    f"left:{x0:.3f}px;top:{y0:.3f}px;"
                    f"height:{height:.3f}px;font-size:{max(1.0, height * 0.84):.3f}px;"
                    f"line-height:{height:.3f}px"
                )
                fragments.append(
                    '<span class="pdf-word" data-width="'
                    + f"{max(1.0, x1 - x0):.3f}"
                    + '" style="'
                    + style
                    + '">'
                    + html.escape(text)
                    + "</span> "
                )
            fragments.append("</span>")
        return "".join(fragments)

    def chapter_html(self, index: int, theme: str = "dark", font_size: int = 20) -> str:
        """Return one faithful PDF page plus its transparent selectable text."""
        image_path, width, height, render_scale = self._render_page(index)
        text_layer = self._text_layer(index)
        has_text = bool(text_layer)
        palette = {
            "dark": ("#0c1017", "#e8e6df", "#9aa8bf", "#67d0b0", "#151c28"),
            "light": ("#e7e9ed", "#24262b", "#5e6470", "#147c6b", "#fffdf8"),
            "sepia": ("#d9cdb6", "#3d3328", "#766650", "#8a5b2b", "#f7edd7"),
        }
        bg, fg, muted, accent, panel = palette.get(theme, palette["dark"])
        title = html.escape(self.chapters[index].title)
        scan_notice = ""
        if not has_text:
            if self._ocr_available:
                detail = "No selectable text could be recovered from this scanned page."
            else:
                detail = (
                    "This appears to be a scanned page. Install Tesseract OCR to enable "
                    "word and phrase definitions on image-only pages."
                )
            scan_notice = (
                '<aside class="scan-notice"><strong>Definitions unavailable on this page</strong>'
                f"<span>{html.escape(detail)}</span></aside>"
            )
        return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>
:root {{ color-scheme: {"dark" if theme == "dark" else "light"}; }}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: auto; background: {bg}; }}
body {{ margin: 0; min-height: 100vh; padding: 34px 24px 84px; overflow-x: hidden;
  background: {bg}; color: {fg}; font-family: 'Segoe UI', sans-serif; }}
.pdf-label {{ max-width: 1120px; margin: 0 auto 16px; color: {accent};
  font: 700 12px/1.2 'Segoe UI', sans-serif; letter-spacing: .13em; text-transform: uppercase; }}
#pdf-page-shell {{ position: relative; margin: 0 auto; filter: drop-shadow(0 18px 36px rgba(0,0,0,.34)); }}
#pdf-page {{ position: absolute; left: 0; top: 0; width: {width:.3f}px; height: {height:.3f}px;
  transform-origin: 0 0; background: white; }}
.pdf-page-image {{ position: absolute; inset: 0; width: 100%; height: 100%; display: block;
  pointer-events: none; user-select: none; -webkit-user-drag: none; }}
.pdf-text-layer {{ position: absolute; inset: 0; z-index: 2; user-select: text;
  -webkit-user-select: text; cursor: text; }}
.pdf-text-line {{ display: contents; }}
.pdf-word {{ position: absolute; display: block; margin: 0; padding: 0; overflow: visible;
  white-space: nowrap; color: transparent; -webkit-text-fill-color: transparent;
  font-family: Arial, sans-serif; font-weight: 400; user-select: text; -webkit-user-select: text; }}
.pdf-word::selection {{ color: transparent; -webkit-text-fill-color: transparent; background: rgba(43,104,246,.78); }}
.scan-notice {{ max-width: 720px; margin: 22px auto 0; padding: 15px 18px; border: 1px solid {accent};
  border-radius: 12px; background: {panel}; color: {muted}; display: grid; gap: 5px; }}
.scan-notice strong {{ color: {fg}; }}
@media (max-width: 640px) {{ body {{ padding: 20px 10px 70px; }} .pdf-label {{ padding-left: 8px; }} }}
</style><title>{title}</title></head>
<body>
<div class="pdf-label">PDF · Page {index + 1} of {len(self.chapters)} · {title}</div>
<div id="pdf-page-shell"><div id="pdf-page">
<img class="pdf-page-image" src="{html.escape(image_path.name, quote=True)}" alt="Rendered PDF page {index + 1}">
<div class="pdf-text-layer" aria-label="Selectable PDF text">{text_layer}</div>
</div></div>
{scan_notice}
<script>
(() => {{
  const logicalWidth = {width:.6f};
  const logicalHeight = {height:.6f};
  const maximumScale = {render_scale:.6f};
  const shell = document.getElementById('pdf-page-shell');
  const page = document.getElementById('pdf-page');
  const measure = document.createElement('canvas').getContext('2d');
  document.querySelectorAll('.pdf-word').forEach((word) => {{
    const style = getComputedStyle(word);
    measure.font = style.font;
    const measured = Math.max(0.1, measure.measureText(word.textContent.trim()).width);
    const target = Math.max(0.1, Number(word.dataset.width));
    word.style.transformOrigin = '0 0';
    word.style.transform = 'scaleX(' + (target / measured) + ')';
  }});
  function fitPage() {{
    const available = Math.max(280, document.documentElement.clientWidth - 48);
    const scale = Math.max(0.1, Math.min(maximumScale, available / logicalWidth));
    page.style.transform = 'scale(' + scale + ')';
    shell.style.width = (logicalWidth * scale) + 'px';
    shell.style.height = (logicalHeight * scale) + 'px';
  }}
  fitPage();
  new ResizeObserver(fitPage).observe(document.documentElement);
}})();
</script>
</body></html>"""

    def text_for_chapter(self, index: int) -> str:
        if index not in self._text_cache:
            lines = self._line_groups(index)
            self._text_cache[index] = _clean_text(
                "\n".join(" ".join(word[4] for word in line) for line in lines)
            )
        return self._text_cache[index]

    def search(self, query: str, limit: int = 100) -> list[SearchResult]:
        needle = _clean_text(query)
        if not needle:
            return []
        folded_needle = needle.casefold()
        results: list[SearchResult] = []
        for index, chapter in enumerate(self.chapters):
            text = self.text_for_chapter(index)
            folded = text.casefold()
            start = folded.find(folded_needle)
            if start < 0:
                continue
            before = max(0, start - 85)
            after = min(len(text), start + len(needle) + 125)
            excerpt = text[before:after]
            if before:
                excerpt = "…" + excerpt
            if after < len(text):
                excerpt += "…"
            results.append(SearchResult(index, chapter.title, excerpt, folded.count(folded_needle)))
            if len(results) >= limit:
                break
        return results

    def chapter_index_for_url(self, url_path: str) -> tuple[int | None, str]:
        split = urlsplit(url_path)
        local_path = unquote(split.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", local_path):
            local_path = local_path[1:]
        name = Path(local_path).name
        match = re.fullmatch(r"page-(\d+)\.html", name, flags=re.IGNORECASE)
        if not match:
            return None, split.fragment
        index = int(match.group(1)) - 1
        if 0 <= index < len(self.chapters):
            return index, split.fragment
        return None, split.fragment
