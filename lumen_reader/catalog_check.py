"""Read-only shelf inspection. Never extract a book, render pages or create scratch files."""
from __future__ import annotations

import os
import posixpath
import sqlite3
import stat
import time
import zipfile
import zlib
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .book import _MAX_UNCOMPRESSED_SIZE, _normalized_href
from .library_index import CatalogIdentity, normalize_root


@dataclass
class CheckResult:
    checked: int = 0
    skipped: int = 0
    damaged: list[tuple[CatalogIdentity, str]] = field(default_factory=list)


def _check_epub(path, cancelled):
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        if sum(i.file_size for i in items) > _MAX_UNCOMPRESSED_SIZE or any(i.flag_bits & 1 for i in items):
            return None  # protected/over the reader's safety limit is not corruption
        def key(name):
            return os.path.normcase(posixpath.normpath(name.replace("\\", "/")))
        names = {key(i.filename): i for i in items if not i.is_dir()}
        if key("META-INF/encryption.xml") in names:
            return None
        def xml(name):
            item = names.get(key(name))
            if item is None:
                return None
            if item.file_size > 8 * 1024 * 1024:
                raise NotImplementedError("oversized package metadata")
            return ET.fromstring(archive.read(item))

        container = xml("META-INF/container.xml")
        if container is None:
            return "Missing EPUB container"
        rootfile = next((n for n in container.iter() if n.tag.split("}")[-1] == "rootfile"), None)
        if rootfile is None or not rootfile.get("full-path"):
            return "Missing EPUB package reference"
        opf = _normalized_href("", rootfile.get("full-path"))
        package = xml(opf)
        if package is None:
            return "Missing EPUB package document"
        manifest = next((n for n in package.iter() if n.tag.split("}")[-1] == "manifest"), None)
        spine = next((n for n in package.iter() if n.tag.split("}")[-1] == "spine"), None)
        if manifest is None or spine is None:
            return "Missing EPUB manifest or spine"
        chapters = {n.get("id") for n in manifest if n.tag.split("}")[-1] == "item"
                    and n.get("id") and n.get("href") and n.get("media-type") in {"application/xhtml+xml", "text/html"}
                    and key(_normalized_href(posixpath.dirname(opf), n.get("href"))) in names}
        if not any(n.tag.split("}")[-1] == "itemref" and n.get("idref") in chapters for n in spine):
            return "No readable EPUB chapters"
        # Lumen extracts the whole archive on open. A bad CRC in a later
        # member breaks that open even if the OPF and first chapter are fine.
        for item in items:
            if item.is_dir():
                continue
            with archive.open(item) as member:
                while member.read(128 * 1024):
                    if cancelled():
                        raise CancelledError()
    return ""


def _diagnose(path, cancelled):
    """Reason for confirmed damage, empty for readable, None for indeterminate."""
    if path.suffix.lower() == ".epub":
        try:
            return _check_epub(path, cancelled)
        except (zipfile.BadZipFile, ET.ParseError, zlib.error) as exc:
            return f"Invalid EPUB: {exc}"
    if path.suffix.lower() == ".pdf":
        import pymupdf
        with path.open("rb") as stream:
            if not stream.read(1):
                return "Empty PDF"
        try:
            with pymupdf.open(path) as document:
                if document.needs_pass:
                    return None
                return "" if document.page_count else "PDF has no pages"
        except pymupdf.FileDataError:
            return "PDF cannot be opened by the reader"
    return None


def check_catalog(database, root, cancelled, progress):
    """Inspect the whole indexed root, independent of the shelf's page or filter."""
    result = CheckResult()
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    last_update = 0.0
    try:
        connection.execute("PRAGMA temp_store=MEMORY")
        key = normalize_root(root)
        total = connection.execute("SELECT count(*) FROM books WHERE root=?", (key,)).fetchone()[0]
        def identities():
            # Do not pin a WAL snapshot for the entire time spent reading
            # thousands of files. Only reviewed fingerprints can be removed.
            upper = connection.execute("SELECT max(id) FROM books WHERE root=?", (key,)).fetchone()[0] or 0
            last = 0
            while last < upper:
                rows = connection.execute(
                    "SELECT id,path,size,mtime_ns FROM books WHERE root=? AND id>? AND id<=? ORDER BY id LIMIT 256",
                    (key, last, upper)).fetchall()
                if not rows:
                    break
                yield from rows
                last = rows[-1][0]
        for identity in identities():
            if cancelled():
                raise CancelledError()
            _, text, size, stamp = identity
            path = Path(text)
            try:
                current = path.lstat()
                reason = None
                if stat.S_ISREG(current.st_mode) and (current.st_size, current.st_mtime_ns) == (size, stamp):
                    reason = _diagnose(path, cancelled)
            except CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, NotImplementedError, ImportError, LookupError):
                reason = None  # permissions, unsupported codecs, missing files/dependencies, etc.
            if reason is None:
                result.skipped += 1
            elif reason:
                result.damaged.append((identity, reason))
            result.checked += 1
            now = time.monotonic()
            if now - last_update >= 0.1 or result.checked == total:
                progress(result.checked, total)
                last_update = now
    finally:
        connection.close()
    if cancelled():
        raise CancelledError()
    return result
