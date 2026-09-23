"""Shared, conservative book-date extraction, filtering and display contracts.

Publication precision is retained: a year is not silently January 1. File
timestamps are instants; filters use UTC and the shelf displays local time.
"""

from __future__ import annotations

import calendar
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from xml.etree import ElementTree as ET

DATE_METADATA_VERSION = 1
DATE_COLUMNS = {
    "published_date": "TEXT NOT NULL DEFAULT ''",
    "published_start": "TEXT",
    "published_end": "TEXT",
    "publication_source": "TEXT NOT NULL DEFAULT ''",
    "created_ns": "INTEGER",
    "date_metadata_version": "INTEGER NOT NULL DEFAULT 0",
}


def publication_date(value: Any) -> tuple[str, str | None, str | None]:
    """Return display precision plus inclusive earliest/latest possible dates."""
    text = str(value or "").strip()
    if len(text) > 64:
        return "", None, None
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not match:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            text = parsed.date().isoformat()
            match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
        except ValueError:
            return "", None, None
    if not match:
        return "", None, None
    year, month, day = (int(part) if part else None for part in match.groups())
    if month == 0 or day == 0:
        return "", None, None
    try:
        first = date(year, month or 1, day or 1)
        last_month = month or 12
        last = date(year, last_month, day or calendar.monthrange(year, last_month)[1])
    except (ValueError, TypeError):
        return "", None, None
    return text, first.isoformat(), last.isoformat()


def epub_publication(root: ET.Element) -> dict[str, Any]:
    candidates = []
    for element in root.iter("{http://purl.org/dc/elements/1.1/}date"):
        event = next((v for k, v in element.attrib.items() if k.rsplit("}", 1)[-1] == "event"), "")
        if event.casefold() not in {"", "publication", "published"}:
            continue
        candidates.append((0 if event else 1, element.text))
    for _, value in sorted(candidates, key=lambda item: item[0]):
        display, first, last = publication_date(value)
        if display:
            return dict(published_date=display, published_start=first, published_end=last,
                        publication_source="epub:dc:date")
    return {}


def pdf_publication(xmp: str) -> dict[str, Any]:
    # PDF CreationDate/xmp:CreateDate describe the PDF, not the book release.
    if not xmp or len(xmp) > 1_000_000 or "<!DOCTYPE" in xmp.upper() or "<!ENTITY" in xmp.upper():
        return {}
    try:
        root = ET.fromstring(xmp)
    except ET.ParseError:
        return {}
    for element in root.iter("{http://purl.org/dc/elements/1.1/}date"):
        for node in element.iter():
            display, first, last = publication_date(node.text)
            if display:
                return dict(published_date=display, published_start=first, published_end=last,
                            publication_source="pdf:xmp:dc:date")
    return {}


def creation_ns(stat: Any) -> int | None:
    """Birth time where supported; never mistake POSIX inode change for birth."""
    value = getattr(stat, "st_birthtime_ns", None)
    if value is None and hasattr(stat, "st_birthtime"):
        value = int(stat.st_birthtime * 1_000_000_000)
    if value is None and os.name == "nt":
        value = getattr(stat, "st_ctime_ns", None)
    return int(value) if value is not None else None


def timestamp_text(value: int | None, *, local: bool = False) -> str | None:
    if value is None:
        return None
    try:
        instant = datetime.fromtimestamp(value / 1_000_000_000, timezone.utc)
        if local:
            return instant.astimezone().strftime("%Y-%m-%d %H:%M")
        return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def date_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(row)
    published = values.get("published_date") or None
    return {
        "published_date": published,
        "publication_precision": {4: "year", 7: "month", 10: "day"}.get(len(published or "")),
        "publication_source": values.get("publication_source") or None,
        "created_ns": values.get("created_ns"),
        "created_at": timestamp_text(values.get("created_ns")),
        "modified_at": timestamp_text(values.get("mtime_ns")),
        "date_metadata_indexed": values.get("date_metadata_version", 0) >= DATE_METADATA_VERSION,
    }


def _instant_bound(value: str, upper: bool) -> int:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        instant = datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
        if upper:
            try:
                instant += timedelta(days=1)
            except OverflowError as exception:
                raise ValueError("File date is outside SQLite's nanosecond range.") from exception
    else:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("File date-time filters require a timezone (Z or an offset).")
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    ns = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    if upper and len(value) == 10:
        ns -= 1
    if not -(2**63) <= ns < 2**63:
        raise ValueError("File date is outside SQLite's nanosecond range.")
    return ns


def date_predicate(filters: dict[str, str] | None, alias: str = "b") -> tuple[str, list[Any]]:
    """Validated AND predicates with bound parameters; publication ranges overlap."""
    if not filters:
        return "", []
    if not isinstance(filters, dict):
        raise ValueError("date_filters must be an object.")
    allowed = {f"{field}_{end}" for field in ("published", "created", "modified") for end in ("from", "to")}
    if set(filters) - allowed:
        raise ValueError("Unknown date filter; use published/created/modified_from or _to.")
    clauses, parameters = [], []
    for field in ("published", "created", "modified"):
        bounds = []
        for end in ("from", "to"):
            raw = filters.get(f"{field}_{end}")
            if raw is None:
                bounds.append(None)
                continue
            if not isinstance(raw, str) or not raw or len(raw) > 40:
                raise ValueError("Date filter values must be nonempty ISO date strings.")
            if field == "published":
                display, start, finish = publication_date(raw)
                if not display or len(raw) not in {4, 7, 10}:
                    raise ValueError("Publication filters accept YYYY, YYYY-MM or YYYY-MM-DD.")
                bound = start if end == "from" else finish
                column = "published_end" if end == "from" else "published_start"
            else:
                bound = _instant_bound(raw, end == "to")
                column = "created_ns" if field == "created" else "mtime_ns"
            bounds.append(bound)
            clauses.append(f"{alias}.{column} {'>=' if end == 'from' else '<='} ?")
            parameters.append(bound)
        if all(bound is not None for bound in bounds) and bounds[0] > bounds[1]:
            raise ValueError(f"{field}_from must not be after {field}_to.")
    return (" AND " + " AND ".join(clauses) if clauses else ""), parameters


def date_order(sort: str, alias: str = "b") -> str | None:
    base = sort.removesuffix("_asc").removesuffix("_desc")
    column = {"published": "published_start", "created": "created_ns", "modified": "mtime_ns"}.get(base)
    if column is None or sort not in {base, base + "_asc", base + "_desc"}:
        return None
    direction = "ASC" if sort.endswith("_asc") else "DESC"
    return f"{alias}.{column} {direction} NULLS LAST,{alias}.id ASC"
