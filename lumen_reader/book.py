"""Safe EPUB extraction, package parsing, rendering, and full-text search."""

from __future__ import annotations

import hashlib
import html
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

from .models import BookMetadata, Chapter, SearchResult, TocEntry


class EpubError(ValueError):
    """Raised when an EPUB cannot be opened safely or is malformed."""


_MAX_UNCOMPRESSED_SIZE = 512 * 1024 * 1024
_SPACE_RE = re.compile(r"\s+")
_UNSAFE_TAGS = {
    "script",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "base",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def _normalized_href(base: str, href: str) -> str:
    path = unquote(urlsplit(href).path).replace("\\", "/")
    return posixpath.normpath(posixpath.join(base, path)).lstrip("/")


class EpubBook:
    """An extracted, parsed EPUB package.

    Instances own a temporary extraction directory and should be closed when the
    book is no longer in use. They can also be used as context managers.
    """

    document_type = "EPUB"

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()
        self._temporary = tempfile.TemporaryDirectory(prefix="lumen_epub_")
        self.extract_dir = Path(self._temporary.name)
        self.opf_path = ""
        self.opf_dir = ""
        self.metadata = BookMetadata(title=self.path.stem)
        self.chapters: list[Chapter] = []
        self.toc: list[TocEntry] = []
        self._manifest: dict[str, dict[str, str]] = {}
        self._search_cache: dict[int, str] = {}
        self._closed = False

        try:
            self._open()
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
        if not self.metadata.cover_href:
            return None
        candidate = self.extract_dir / Path(*PurePosixPath(self.metadata.cover_href).parts)
        return candidate if candidate.is_file() else None

    def __enter__(self) -> "EpubBook":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._temporary.cleanup()
            self._closed = True

    def _open(self) -> None:
        if not self.path.is_file():
            raise EpubError(f"Book not found: {self.path}")
        if self.path.suffix.lower() != ".epub":
            raise EpubError("The selected file is not an .epub file.")
        if not zipfile.is_zipfile(self.path):
            raise EpubError("The selected file is not a valid EPUB/ZIP archive.")

        with zipfile.ZipFile(self.path) as archive:
            self._safe_extract(archive)
        self._parse_container()
        self._parse_package()
        if not self.chapters:
            raise EpubError("This EPUB has no readable chapters in its spine.")

    def _safe_extract(self, archive: zipfile.ZipFile) -> None:
        total = sum(item.file_size for item in archive.infolist())
        if total > _MAX_UNCOMPRESSED_SIZE:
            raise EpubError("This EPUB is too large to open safely (over 512 MB unpacked).")

        root = self.extract_dir.resolve()
        for item in archive.infolist():
            raw_name = item.filename.replace("\\", "/")
            path = PurePosixPath(raw_name)
            if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
                raise EpubError(f"Unsafe path in EPUB archive: {item.filename}")
            destination = (root / Path(*path.parts)).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise EpubError(f"Unsafe path in EPUB archive: {item.filename}") from exc
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)

    def _parse_container(self) -> None:
        container = self.extract_dir / "META-INF" / "container.xml"
        if not container.is_file():
            raise EpubError("EPUB is missing META-INF/container.xml.")
        try:
            root = ET.parse(container).getroot()
        except ET.ParseError as exc:
            raise EpubError("EPUB container.xml is malformed.") from exc
        rootfile = next((node for node in root.iter() if _local_name(node.tag) == "rootfile"), None)
        if rootfile is None or not rootfile.get("full-path"):
            raise EpubError("EPUB container does not identify a package document.")
        self.opf_path = _normalized_href("", rootfile.get("full-path", ""))
        self.opf_dir = posixpath.dirname(self.opf_path)
        if not self._disk_path(self.opf_path).is_file():
            raise EpubError("EPUB package document could not be found.")

    def _parse_package(self) -> None:
        try:
            root = ET.parse(self._disk_path(self.opf_path)).getroot()
        except ET.ParseError as exc:
            raise EpubError("EPUB package document is malformed.") from exc

        metadata_node = next((node for node in root.iter() if _local_name(node.tag) == "metadata"), None)
        manifest_node = next((node for node in root.iter() if _local_name(node.tag) == "manifest"), None)
        spine_node = next((node for node in root.iter() if _local_name(node.tag) == "spine"), None)
        if manifest_node is None or spine_node is None:
            raise EpubError("EPUB package is missing its manifest or spine.")

        cover_id = ""
        if metadata_node is not None:
            titles, authors = [], []
            fields: dict[str, str] = {}
            for node in metadata_node:
                name = _local_name(node.tag).lower()
                value = _clean_text("".join(node.itertext()))
                if name == "title" and value:
                    titles.append(value)
                elif name in {"creator", "author"} and value:
                    authors.append(value)
                elif name in {"language", "publisher", "description", "identifier"} and value:
                    fields.setdefault(name, value)
                elif name == "meta" and node.get("name", "").lower() == "cover":
                    cover_id = node.get("content", "")
            self.metadata = BookMetadata(
                title=titles[0] if titles else self.path.stem,
                authors=authors,
                language=fields.get("language", ""),
                publisher=fields.get("publisher", ""),
                description=fields.get("description", ""),
                identifier=fields.get("identifier", ""),
            )

        for node in manifest_node:
            if _local_name(node.tag) != "item" or not node.get("id") or not node.get("href"):
                continue
            self._manifest[node.get("id", "")] = {
                "href": _normalized_href(self.opf_dir, node.get("href", "")),
                "media_type": node.get("media-type", ""),
                "properties": node.get("properties", ""),
            }

        cover_item = self._manifest.get(cover_id)
        if cover_item:
            self.metadata.cover_href = cover_item["href"]
        else:
            for item in self._manifest.values():
                if "cover-image" in item["properties"].split():
                    self.metadata.cover_href = item["href"]
                    break

        spine_ids: list[str] = []
        for node in spine_node:
            if _local_name(node.tag) == "itemref" and node.get("idref") in self._manifest:
                spine_ids.append(node.get("idref", ""))
        for item_id in spine_ids:
            item = self._manifest[item_id]
            if item["media_type"] not in {"application/xhtml+xml", "text/html"}:
                continue
            self.chapters.append(
                Chapter(item_id, item["href"], item["media_type"], self._fallback_title(item_id))
            )

        self.toc = self._parse_navigation(spine_node.get("toc", ""))
        self._assign_toc_indices_and_titles(self.toc)
        if not self.toc:
            self.toc = [TocEntry(chapter.title, chapter.href, index) for index, chapter in enumerate(self.chapters)]

    def _fallback_title(self, item_id: str) -> str:
        title = re.sub(r"[_-]+", " ", item_id).strip()
        return title.title() or "Untitled section"

    def _parse_navigation(self, ncx_id: str) -> list[TocEntry]:
        nav_item = next(
            (item for item in self._manifest.values() if "nav" in item["properties"].split()), None
        )
        if nav_item:
            entries = self._parse_epub3_nav(nav_item["href"])
            if entries:
                return entries
        ncx_item = self._manifest.get(ncx_id)
        if ncx_item:
            return self._parse_ncx(ncx_item["href"])
        fallback_ncx = next(
            (item for item in self._manifest.values() if item["media_type"] == "application/x-dtbncx+xml"),
            None,
        )
        return self._parse_ncx(fallback_ncx["href"]) if fallback_ncx else []

    def _parse_epub3_nav(self, href: str) -> list[TocEntry]:
        try:
            soup = BeautifulSoup(self._disk_path(href).read_bytes(), "html.parser")
        except OSError:
            return []
        nav = next(
            (
                node
                for node in soup.find_all("nav")
                if "toc" in (node.get("epub:type", ""), node.get("type", ""), node.get("role", ""))
                or node.get("role") == "doc-toc"
            ),
            soup.find("nav"),
        )
        if not nav:
            return []

        def parse_list(list_node: Tag) -> list[TocEntry]:
            result: list[TocEntry] = []
            for li in list_node.find_all("li", recursive=False):
                anchor = li.find("a", recursive=False) or li.find("span", recursive=False)
                if not anchor:
                    continue
                target = anchor.get("href", "") if anchor.name == "a" else ""
                child_list = li.find(["ol", "ul"], recursive=False)
                result.append(
                    TocEntry(
                        _clean_text(anchor.get_text(" ")) or "Untitled section",
                        _normalized_href(posixpath.dirname(href), target) if target else "",
                        children=parse_list(child_list) if child_list else [],
                    )
                )
            return result

        first_list = nav.find(["ol", "ul"])
        return parse_list(first_list) if first_list else []

    def _parse_ncx(self, href: str) -> list[TocEntry]:
        try:
            root = ET.parse(self._disk_path(href)).getroot()
        except (OSError, ET.ParseError):
            return []

        def parse_points(parent: ET.Element) -> list[TocEntry]:
            result: list[TocEntry] = []
            for point in parent:
                if _local_name(point.tag) != "navPoint":
                    continue
                label = next((node for node in point.iter() if _local_name(node.tag) == "navLabel"), None)
                content = next((node for node in point if _local_name(node.tag) == "content"), None)
                title = _clean_text(" ".join(label.itertext())) if label is not None else "Untitled section"
                target = content.get("src", "") if content is not None else ""
                result.append(
                    TocEntry(
                        title or "Untitled section",
                        _normalized_href(posixpath.dirname(href), target) if target else "",
                        children=parse_points(point),
                    )
                )
            return result

        nav_map = next((node for node in root.iter() if _local_name(node.tag) == "navMap"), None)
        return parse_points(nav_map) if nav_map is not None else []

    def _assign_toc_indices_and_titles(self, entries: list[TocEntry]) -> None:
        chapter_lookup = {chapter.href: index for index, chapter in enumerate(self.chapters)}

        def visit(nodes: list[TocEntry]) -> None:
            for entry in nodes:
                clean_href = entry.href.split("#", 1)[0]
                entry.chapter_index = chapter_lookup.get(clean_href)
                if entry.chapter_index is not None:
                    self.chapters[entry.chapter_index].title = entry.title
                visit(entry.children)

        visit(entries)

    def _disk_path(self, href: str) -> Path:
        clean = href.split("#", 1)[0]
        return self.extract_dir / Path(*PurePosixPath(clean).parts)

    def chapter_base_url(self, index: int) -> str:
        return self._disk_path(self.chapters[index].href).parent.as_uri() + "/"

    def chapter_html(self, index: int, theme: str = "dark", font_size: int = 20) -> str:
        """Return a sanitized, styled HTML document for a spine chapter."""
        chapter = self.chapters[index]
        try:
            raw = self._disk_path(chapter.href).read_bytes()
        except OSError as exc:
            raise EpubError(f"Could not read chapter: {chapter.title}") from exc
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all(_UNSAFE_TAGS):
            tag.decompose()
        for tag in soup.find_all(True):
            for attribute in list(tag.attrs):
                lowered = attribute.lower()
                if lowered.startswith("on") or lowered in {"srcdoc", "formaction"}:
                    del tag.attrs[attribute]
        # Book links must never enter Chromium's keyboard focus chain.  Apart
        # from making Tab predictable, this prevents a focused fragment link
        # from silently scrolling the document to its target while the reader
        # is selecting text.  Ctrl+click activation is enforced by the UI.
        for anchor in soup.find_all("a"):
            anchor["tabindex"] = "-1"
            anchor["draggable"] = "false"
            if not anchor.get("title"):
                anchor["title"] = "Hold Ctrl and click to open this link"

        safe_head = []
        if soup.head:
            for tag in soup.head.find_all(["style", "link"]):
                if tag.name == "link":
                    href = str(tag.get("href", ""))
                    scheme = urlsplit(href).scheme.lower()
                    if scheme not in {"", "file"} or "stylesheet" not in tag.get("rel", []):
                        continue
                safe_head.append(str(tag))
        body = soup.body or soup
        palette = {
            "dark": ("#11141c", "#e8e6df", "#a8b0c0", "#71d4b4", "#1c2230", "#89a7ff"),
            "light": ("#f6f2e9", "#24262b", "#5e6470", "#147c6b", "#fffdf8", "#315ab3"),
            "sepia": ("#efe4cc", "#3d3328", "#766650", "#8a5b2b", "#f7edd7", "#855c2f"),
        }
        bg, fg, muted, accent, panel, link = palette.get(theme, palette["dark"])
        title = html.escape(chapter.title)
        content = "".join(str(node) for node in body.contents)
        return f"""<!doctype html>
<html><head><meta charset=\"utf-8\">
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data: file:; style-src 'unsafe-inline' file:; font-src data: file:; media-src file: data:\">
{''.join(safe_head)}
<style>
:root {{ color-scheme: {"light" if theme != "dark" else "dark"}; }}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: auto; background: {bg}; }}
body {{ margin: 0 auto; padding: 62px 72px 110px; max-width: 900px; background: {bg}; color: {fg};
  font-family: Georgia, 'Palatino Linotype', Palatino, serif; font-size: {font_size}px;
  line-height: 1.72; letter-spacing: .006em; overflow-wrap: break-word; }}
p {{ margin: 0 0 1.15em; }}
h1, h2, h3, h4, h5, h6 {{ color: {fg}; line-height: 1.2; margin: 1.55em 0 .7em;
  font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif; letter-spacing: -.025em; }}
h1 {{ font-size: 2.25em; }} h2 {{ font-size: 1.65em; }} h3 {{ font-size: 1.28em; }}
a {{ color: {link}; cursor: text; -webkit-user-drag: none; text-decoration-color: color-mix(in srgb, {link}, transparent 55%); text-underline-offset: .15em; }}
a:focus, a:focus-visible {{ outline: none !important; box-shadow: none !important; }}
html.lumen-ctrl-links a[href] {{ cursor: pointer; }}
blockquote {{ border-left: 4px solid {accent}; background: {panel}; margin: 1.7em 0; padding: 1em 1.35em; color: {muted}; }}
img, svg {{ display: block; max-width: 100% !important; height: auto !important; margin: 1.8em auto; border-radius: 8px; }}
figure {{ margin: 2em 0; }} figcaption {{ color: {muted}; text-align: center; font-size: .82em; }}
pre, code {{ font-family: 'Cascadia Mono', Consolas, monospace; }}
pre {{ white-space: pre-wrap; background: {panel}; padding: 1em; border-radius: 8px; overflow-x: auto; }}
hr {{ border: 0; height: 1px; background: color-mix(in srgb, {muted}, transparent 70%); margin: 2.5em 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 1.5em 0; }}
td, th {{ border: 1px solid color-mix(in srgb, {muted}, transparent 55%); padding: .5em .7em; }}
.lumen-section-label {{ color: {accent}; font: 650 12px/1.2 'Segoe UI', sans-serif; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 25px; }}
@media (max-width: 720px) {{ body {{ padding: 38px 28px 90px; }} }}
</style><title>{title}</title></head>
<body><div class=\"lumen-section-label\">{index + 1:02d} · {title}</div>{content}</body></html>"""

    def text_for_chapter(self, index: int) -> str:
        if index not in self._search_cache:
            raw = self._disk_path(self.chapters[index].href).read_bytes()
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup.find_all(["script", "style"]):
                tag.decompose()
            self._search_cache[index] = _clean_text(soup.get_text(" "))
        return self._search_cache[index]

    def search(self, query: str, limit: int = 100) -> list[SearchResult]:
        needle = _clean_text(query)
        if not needle:
            return []
        folded_needle = needle.casefold()
        results: list[SearchResult] = []
        for index, chapter in enumerate(self.chapters):
            text = self.text_for_chapter(index)
            folded_text = text.casefold()
            start = folded_text.find(folded_needle)
            if start < 0:
                continue
            before = max(0, start - 85)
            after = min(len(text), start + len(needle) + 125)
            excerpt = text[before:after]
            if before:
                excerpt = "…" + excerpt
            if after < len(text):
                excerpt += "…"
            results.append(
                SearchResult(index, chapter.title, excerpt, folded_text.count(folded_needle))
            )
            if len(results) >= limit:
                break
        return results

    def chapter_index_for_url(self, url_path: str) -> tuple[int | None, str]:
        """Map an extracted local URL to a spine index and optional fragment."""
        split = urlsplit(url_path)
        local_path = unquote(split.path)
        # RFC file URLs represent a Windows drive as /C:/..., while pathlib
        # expects C:/... on Windows.
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", local_path):
            local_path = local_path[1:]
        try:
            local = Path(local_path).resolve().relative_to(self.extract_dir.resolve())
        except (ValueError, OSError):
            return None, split.fragment
        href = PurePosixPath(*local.parts).as_posix()
        for index, chapter in enumerate(self.chapters):
            if chapter.href == href:
                return index, split.fragment
        return None, split.fragment
