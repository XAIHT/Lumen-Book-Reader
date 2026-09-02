"""Complete read-only retrieval facade exposed by the Lumen MCP server."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit

from .. import accel
from ..passage_index import corpus_revision, passage_schema_available
from ..runtime_paths import RuntimePaths
from ..version import get_version
from .citations import CitationCodec
from .contracts import BackendReport, RetrievalError, RootScope, SCHEMA_VERSION
from .cursors import CursorCodec
from .glob_engine import compile_glob, fixed_prefix
from .grep_engine import (
    exact_ranges,
    excerpt_for_ranges,
    regex_available,
    regex_ranges,
    required_literal,
)
from .lexical import safe_fts_query
from .planner import explain as explain_plan
from .pool import QueryPool
from .semantic import expand_terms as semantic_expand_terms
from .semantic import status as semantic_status


MAX_LIMIT = 100
MAX_EXCERPT = 4000
MAX_REGEX_CANDIDATES = 20_000
MAX_REGEX_TEXT = 4 * 1024 * 1024


class RetrievalService:
    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        max_connections: int = 8,
    ):
        self.paths = paths or RuntimePaths.discover()
        self.pool = QueryPool(self.paths.index_file, max_connections=max_connections)
        citation_key = self.paths.cache_dir / "citation.key"
        self.citations = CitationCodec(citation_key)
        self.cursors = CursorCodec(self.citations.secret)
        self.started_at = time.monotonic()
        # MCP SDK 1.x dispatches synchronous tools through a worker thread.
        # Preload NLTK's lazy WordNet corpus on the construction thread so its
        # first import/corpus resolution cannot deadlock that legacy worker.
        semantic_status()
        accel.start_background_probe()

    # ── public operations ────────────────────────────────────────────────

    def status(
        self,
        *,
        include_roots: bool = True,
        include_backends: bool = True,
        include_recent_failures: bool = False,
    ) -> dict[str, Any]:
        request_id = _request_id()
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation": "lumen_status",
            "request_id": request_id,
            "server": {
                "version": get_version(),
                "process_id": os.getpid(),
                "python": sys.version.split()[0],
                "uptime_seconds": round(time.monotonic() - self.started_at, 3),
                "transport_default": "stdio",
                "read_only": True,
            },
            "catalog": {
                "path": str(self.paths.index_file),
                "exists": self.paths.index_file.is_file(),
            },
            "roots": [],
            "backends": {},
            "warnings": [],
        }
        if not self.paths.index_file.is_file():
            base["health"] = "not_indexed"
            base["corpus"] = {
                "corpus_revision": 0,
                "books": 0,
                "passages": 0,
                "coverage": {},
            }
            base["warnings"].append("Open Lumen and sweep the configured library first.")
            return base
        with self.pool.connection() as connection:
            roots = self._root_scopes(connection)
            passage_schema = passage_schema_available(connection)
            book_count = int(connection.execute("SELECT COUNT(*) FROM books").fetchone()[0])
            passage_count = 0
            coverage: dict[str, int] = {}
            if passage_schema:
                passage_count = int(connection.execute(
                    "SELECT COALESCE(SUM(passage_count),0) FROM rag_documents"
                    " WHERE active_revision IS NOT NULL"
                ).fetchone()[0] or 0)
                coverage = {str(row[0]): int(row[1]) for row in connection.execute(
                    "SELECT coverage,COUNT(*) FROM rag_documents"
                    " WHERE active_revision IS NOT NULL GROUP BY coverage"
                )}
            base["catalog"].update({
                "bytes": self.paths.index_file.stat().st_size,
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "query_only": True,
                "passage_schema_version": 1 if passage_schema else 0,
            })
            base["corpus"] = {
                "corpus_revision": corpus_revision(connection) if passage_count else 0,
                "books": book_count,
                "passages": passage_count,
                "coverage": coverage,
                "passage_index": "ready" if passage_count else "legacy_fallback",
            }
            if include_roots:
                base["roots"] = [
                    {
                        "root_id": root.root_id,
                        "path": root.path,
                        "book_count": root.book_count,
                        "exists": Path(root.path).is_dir(),
                        "authorized": True,
                    }
                    for root in roots
                ]
            if include_recent_failures:
                try:
                    base["recent_failures"] = [dict(row) for row in connection.execute(
                        "SELECT root,status,error,finished_at FROM scan_runs"
                        " WHERE status NOT IN ('done','complete') OR failed>0"
                        " ORDER BY finished_at DESC LIMIT 10"
                    )]
                except sqlite3.Error:
                    base["recent_failures"] = []
        if include_backends:
            semantic = semantic_status()
            hardware: list[dict[str, Any]] = []
            if accel.probed():
                hardware = [
                    {
                        "kind": item.kind,
                        "name": item.name,
                        "hardware_detected": item.available,
                        "backend_registered": (
                            accel.search_kernel_ready(accel.GPU_RESIDENT)
                            if item.kind == "gpu" else False
                        ),
                        "backend_used": False,
                        "detail": item.detail,
                    }
                    for item in accel.accelerators()
                ]
            base["backends"] = {
                "lexical": {"available": True, "selected": True, "backend": "sqlite-fts5"},
                "regex": {
                    "available": regex_available(),
                    "selected": regex_available(),
                    "backend": "regex-timeout" if regex_available() else "none",
                },
                "semantic": semantic.__dict__ if hasattr(semantic, "__dict__") else {
                    "available": semantic.available,
                    "selected": semantic.selected,
                    "backend": semantic.backend,
                    "model_id": semantic.model_id,
                    "reason": semantic.reason,
                },
                "hardware": hardware,
                "probe_state": "complete" if accel.probed() else "running",
            }
        base["limits"] = {
            "max_results": MAX_LIMIT,
            "max_excerpt_chars": MAX_EXCERPT,
            "max_regex_candidates": MAX_REGEX_CANDIDATES,
            "max_regex_text_bytes": MAX_REGEX_TEXT,
            "max_connections": self.pool.max_connections,
        }
        base["health"] = "ready"
        return base

    def glob(
        self,
        pattern: str,
        *,
        target: str = "path",
        roots: Sequence[str] = (),
        formats: Sequence[str] = (),
        case_sensitive: str = "auto",
        include_sections: bool = False,
        sort: str = "path",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        limit = _limit(limit, 50)
        target = target.casefold()
        allowed_targets = {
            "path", "filename", "title", "author", "subject", "publisher", "any_metadata"
        }
        if target not in allowed_targets:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported glob target: {target}.")
        sensitive = case_sensitive == "true" or (case_sensitive == "auto" and os.name != "nt")
        matcher = compile_glob(pattern, case_sensitive=sensitive)
        format_values = _formats(formats)
        with self.pool.connection() as connection:
            selected = self._select_roots(connection, roots)
            ready = self._passage_index_ready(connection)
            revision = corpus_revision(connection) if ready else 0
            digest = _query_digest({
                "pattern": pattern, "target": target, "roots": roots,
                "formats": format_values, "case": case_sensitive, "sections": include_sections,
                "sort": sort,
            })
            root_digest = _root_digest(selected)
            offset = self._cursor_offset(cursor, "lumen_glob", digest, revision, root_digest)
            rows = self._glob_candidates(
                connection, selected, pattern, target, format_values, sort, offset,
                passage_ready=ready,
            )
            hits: list[dict[str, Any]] = []
            root_by_path = {root.path: root for root in selected}
            for row in rows:
                value, relative = self._glob_value(row, target)
                if not matcher.fullmatch(value):
                    continue
                scope = root_by_path.get(str(row["root"]))
                hits.append({
                    "rank": len(hits) + 1,
                    "resource_uri": f"lumen://book/{int(row['id'])}",
                    "book_id": int(row["id"]),
                    "root_id": scope.root_id if scope else "",
                    "relative_path": relative,
                    "path": str(row["path"]),
                    "name": str(row["name"]),
                    "title": str(row["title"]),
                    "author": str(row["author"]),
                    "format": str(row["ext"]).lstrip("."),
                    "language": str(row["language"]),
                    "subjects": str(row["subjects"]),
                    "publisher": str(row["publisher"]),
                    "size_bytes": int(row["size"]),
                    "modified_ns": int(row["mtime_ns"]),
                    "coverage": str(row["coverage"] or "metadata_only"),
                    "matched_value": value,
                })
                if len(hits) >= limit:
                    break
            warnings: list[str] = []
            if include_sections and not ready:
                warnings.append(
                    "Section globbing is unavailable until the passage schema is built."
                )
            elif include_sections and len(hits) < limit:
                section_rows = self._glob_section_candidates(
                    connection, selected, format_values, offset=offset
                )
                for row in section_rows:
                    matched_value = next(
                        (value for value in (str(row["section_title"]), str(row["href"]))
                         if matcher.fullmatch(value)),
                        "",
                    )
                    if not matched_value:
                        continue
                    hits.append({
                        "rank": len(hits) + 1,
                        "resource_uri": (
                            f"lumen://book/{int(row['book_id'])}/section/"
                            f"{int(row['section_ordinal'])}"
                        ),
                        "book_id": int(row["book_id"]),
                        "root_id": _root_id(str(row["root"])),
                        "relative_path": self._relative_path(row),
                        "path": str(row["path"]),
                        "name": str(row["name"]),
                        "title": str(row["title"]),
                        "author": str(row["author"]),
                        "format": str(row["ext"]).lstrip("."),
                        "language": str(row["language"]),
                        "subjects": str(row["subjects"]),
                        "publisher": str(row["publisher"]),
                        "size_bytes": int(row["size"]),
                        "modified_ns": int(row["mtime_ns"]),
                        "coverage": str(row["coverage"]),
                        "matched_value": matched_value,
                        "match_kind": "section",
                        "section": {
                            "ordinal": int(row["section_ordinal"]),
                            "title": str(row["section_title"]),
                            "href": str(row["href"]),
                        },
                    })
                    if len(hits) >= limit:
                        break
            next_cursor = None
            if len(rows) >= min(10_000, max(limit * 20, 500)):
                next_cursor = self.cursors.encode(
                    operation="lumen_glob", query_digest=digest, corpus_revision=revision,
                    root_digest=root_digest, offset=offset + len(rows),
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "operation": "lumen_glob",
                "request_id": _request_id(),
                "corpus_revision": revision,
                "backend": BackendReport("glob", ["sqlite-catalog", "glob-verifier"]).as_dict(),
                "partial": False,
                "warnings": warnings,
                "timing": {"total_ms": _elapsed_ms(started)},
                "hits": hits,
                "next_cursor": next_cursor,
            }

    def search(
        self,
        query: str,
        *,
        strategy: str = "auto",
        roots: Sequence[str] = (),
        formats: Sequence[str] = (),
        languages: Sequence[str] = (),
        book_ids: Sequence[int] = (),
        diversity: str = "book",
        max_per_book: int = 3,
        include_adjacent: bool = False,
        coverage: str = "include_partial",
        limit: int = 20,
        excerpt_chars: int = 700,
        cursor: str | None = None,
        _expression: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        limit = _limit(limit, 20)
        excerpt_chars = _excerpt(excerpt_chars)
        max_per_book = max(1, min(20, int(max_per_book)))
        strategy = strategy.casefold()
        if strategy not in {"auto", "lexical", "hybrid", "semantic"}:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported strategy: {strategy}.")
        semantic = semantic_status()
        if strategy == "semantic" and not semantic.available:
            raise RetrievalError(
                "BACKEND_UNAVAILABLE", semantic.reason,
                suggested_action="Use strategy='lexical' or strategy='auto'.",
            )
        expression = _expression or safe_fts_query(query)
        semantic_terms: list[str] = []
        if _expression is None and strategy in {"auto", "hybrid", "semantic"} and semantic.available:
            semantic_terms = semantic_expand_terms(query)
            if semantic_terms:
                expansions = [safe_fts_query(term, phrase=True) for term in semantic_terms]
                expression = f"({expression}) OR " + " OR ".join(f"({item})" for item in expansions)
        format_values = _formats(formats)
        book_values = _book_ids(book_ids)
        language_values = [str(value).strip().casefold()[:32] for value in languages if str(value).strip()]
        with self.pool.connection() as connection:
            selected = self._select_roots(connection, roots)
            ready = self._passage_index_ready(connection)
            revision = corpus_revision(connection) if ready else 0
            digest = _query_digest({
                "query": query, "expression": expression, "strategy": strategy,
                "roots": roots, "formats": format_values,
                "languages": language_values, "books": book_values, "diversity": diversity,
                "max_per_book": max_per_book, "coverage": coverage,
            })
            root_digest = _root_digest(selected)
            offset = self._cursor_offset(cursor, "lumen_search", digest, revision, root_digest)
            raw_rows = self._passage_search_rows(
                connection, expression, selected, format_values, language_values, book_values,
                coverage, max(limit * 8, 80), offset,
            ) if ready else []
            hits: list[dict[str, Any]] = []
            per_book: dict[int, int] = defaultdict(int)
            for row in raw_rows:
                book_id = int(row["book_id"])
                if diversity == "book" and per_book[book_id] >= max_per_book:
                    continue
                per_book[book_id] += 1
                hits.append(self._passage_hit(row, len(hits) + 1, excerpt_chars))
                if include_adjacent:
                    hits[-1]["adjacent_resource_uri"] = (
                        f"lumen://passage/{int(row['passage_id'])}/context?before=1&after=1"
                    )
                if len(hits) >= limit:
                    break
            fallback_used = False
            if not hits:
                bootstrap = self._bootstrap_search_rows(
                    connection, expression, selected, format_values, book_values,
                    max(limit * 4, 40), offset,
                )
                for row in bootstrap[:limit]:
                    hits.append(self._bootstrap_hit(row, len(hits) + 1, excerpt_chars))
                fallback_used = bool(bootstrap)
            backend = BackendReport(requested=strategy)
            backend.used = ["sqlite-fts5-passages" if raw_rows else "sqlite-fts5-book-head"]
            if semantic_terms:
                backend.used.append("wordnet-query-expansion")
                backend.model_id = semantic.model_id
            if strategy in {"auto", "hybrid"} and not semantic.available:
                backend.fallback_from = ["semantic"] if strategy == "hybrid" else []
            warnings: list[str] = []
            if fallback_used or not ready:
                warnings.append("Passage coverage was unavailable for these hits; using capped book-head text.")
            if strategy == "hybrid" and not semantic.available:
                warnings.append(semantic.reason)
            coverage_summary = self._coverage(connection, selected)
            next_cursor = None
            if len(raw_rows) >= max(limit * 8, 80):
                next_cursor = self.cursors.encode(
                    operation="lumen_search", query_digest=digest, corpus_revision=revision,
                    root_digest=root_digest, offset=offset + len(raw_rows),
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "operation": "lumen_search",
                "request_id": _request_id(),
                "corpus_revision": revision,
                "backend": backend.as_dict(),
                "coverage": coverage_summary,
                "partial": False,
                "warnings": warnings,
                "timing": {"total_ms": _elapsed_ms(started)},
                "hits": hits,
                "next_cursor": next_cursor,
            }

    def grep(
        self,
        query: str,
        *,
        mode: str = "literal",
        case_sensitive: bool = False,
        whole_word: bool = False,
        roots: Sequence[str] = (),
        book_ids: Sequence[int] = (),
        formats: Sequence[str] = (),
        max_matches_per_book: int = 3,
        context_chars: int = 480,
        fallback: str = "none",
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        query = query.strip()
        mode = mode.casefold()
        fallback = fallback.casefold()
        if not query or len(query) > (2048 if mode == "regex" else 4096):
            raise RetrievalError("INVALID_ARGUMENT", "Grep query length is outside the allowed range.")
        if mode not in {"literal", "phrase", "fts", "regex"}:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported grep mode: {mode}.")
        if fallback not in {"none", "literal", "fts"}:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported grep fallback: {fallback}.")
        if mode == "fts" and case_sensitive:
            raise RetrievalError("INVALID_ARGUMENT", "FTS mode cannot promise case-sensitive matching.")
        limit = _limit(limit, 30)
        context_chars = max(80, min(2000, int(context_chars)))
        maximum = max(1, min(20, int(max_matches_per_book)))
        format_values = _formats(formats)
        book_values = _book_ids(book_ids)
        effective_mode = mode
        if mode == "regex" and not regex_available():
            if fallback == "literal":
                effective_mode = "literal"
            elif fallback == "fts":
                effective_mode = "fts"
            else:
                raise RetrievalError("BACKEND_UNAVAILABLE", "The bounded regex backend is not installed.")
        candidate_query = query
        if effective_mode == "regex":
            candidate_query = required_literal(query)
            if not candidate_query and not book_values:
                raise RetrievalError(
                    "REGEX_TOO_BROAD",
                    "The expression has no indexable literal and no explicit book scope.",
                    suggested_action="Add a literal of at least three characters or restrict book_ids.",
                    details={"candidate_cap": MAX_REGEX_CANDIDATES},
                )
        expression = None
        try:
            if candidate_query:
                expression = safe_fts_query(candidate_query, phrase=effective_mode == "phrase")
        except RetrievalError:
            if effective_mode in {"fts", "phrase"}:
                raise
        with self.pool.connection() as connection:
            selected = self._select_roots(connection, roots)
            ready = self._passage_index_ready(connection)
            revision = corpus_revision(connection) if ready else 0
            digest = _query_digest({
                "query": query, "mode": effective_mode, "case": case_sensitive,
                "word": whole_word, "roots": roots, "books": book_values,
                "formats": format_values,
            })
            root_digest = _root_digest(selected)
            offset = self._cursor_offset(cursor, "lumen_grep", digest, revision, root_digest)
            candidates = self._grep_candidates(
                connection, expression, selected, format_values, book_values,
                ready=ready, offset=offset,
            )
            hits: list[dict[str, Any]] = []
            per_book: dict[int, int] = defaultdict(int)
            inspected = 0
            partial = False
            for row in candidates:
                body = str(row["body"] or "")
                inspected += len(body.encode("utf-8"))
                if inspected > MAX_REGEX_TEXT:
                    partial = True
                    break
                book_id = int(row["book_id"])
                if per_book[book_id] >= maximum:
                    continue
                if effective_mode == "regex":
                    ranges = regex_ranges(
                        body, query, case_sensitive=case_sensitive, maximum=maximum,
                    )
                elif effective_mode in {"literal", "phrase"}:
                    ranges = exact_ranges(
                        body, query, case_sensitive=case_sensitive,
                        whole_word=whole_word, maximum=maximum,
                    )
                else:
                    ranges = []
                if effective_mode != "fts" and not ranges:
                    continue
                excerpt, mapped = excerpt_for_ranges(
                    body, ranges, context_chars=context_chars,
                )
                if "passage_id" in row.keys() and row["passage_id"] is not None:
                    hit = self._passage_hit(row, len(hits) + 1, context_chars, excerpt=excerpt)
                else:
                    hit = self._bootstrap_hit(row, len(hits) + 1, context_chars, excerpt=excerpt)
                hit["match_ranges"] = mapped
                hit["matches_in_passage"] = len(ranges) if ranges else None
                hits.append(hit)
                per_book[book_id] += 1
                if len(hits) >= limit:
                    break
            backend_used = (
                "regex-timeout" if effective_mode == "regex" else
                "sqlite-fts5+exact-verifier" if effective_mode != "fts" else "sqlite-fts5"
            )
            warnings: list[str] = []
            if effective_mode != mode:
                warnings.append(f"Requested {mode} was unavailable; explicit {effective_mode} fallback used.")
            if not ready:
                warnings.append("Exact locations are limited to capped book-head text until passages are built.")
            if partial:
                warnings.append("Verification stopped at the configured inspected-text budget.")
            return {
                "schema_version": SCHEMA_VERSION,
                "operation": "lumen_grep",
                "request_id": _request_id(),
                "corpus_revision": revision,
                "backend": BackendReport(mode, [backend_used], [mode] if effective_mode != mode else []).as_dict(),
                "coverage": self._coverage(connection, selected),
                "partial": partial,
                "warnings": warnings,
                "timing": {"total_ms": _elapsed_ms(started)},
                "hits": hits,
                "next_cursor": None,
            }

    def related(
        self,
        *,
        passage_id: int | None = None,
        book_id: int | None = None,
        citation_id: str | None = None,
        text: str | None = None,
        relationship: str = "conceptual",
        exclude_same_book: bool = False,
        strategy: str = "auto",
        limit: int = 20,
    ) -> dict[str, Any]:
        seeds = [passage_id is not None, book_id is not None, bool(citation_id), bool(text)]
        if sum(seeds) != 1:
            raise RetrievalError("INVALID_ARGUMENT", "Exactly one related-content seed is required.")
        relationship = relationship.casefold()
        if relationship not in {"adjacent", "conceptual", "same_subject", "same_author", "contrasting"}:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported relationship: {relationship}.")
        limit = _limit(limit, 20)
        seed_book: int | None = book_id
        seed_text = (text or "")[:16_384]
        seed_author = ""
        seed_subjects = ""
        metadata_book_ids: list[int] = []
        metadata_truncated = False
        with self.pool.connection() as connection:
            ready = self._passage_index_ready(connection)
            if not ready and (citation_id or passage_id is not None):
                self._require_passages(connection)
            if citation_id:
                payload = self.citations.decode(citation_id)
                passage_id = int(payload["p"])
            if passage_id is not None:
                row = self._passage_row(connection, int(passage_id), None)
                seed_book = int(row["book_id"])
                seed_author = str(row["author"] or "")
                seed_subjects = str(row["subjects"] or "")
                seed_text = (
                    seed_subjects
                    or str(row["title"] or "")
                    or str(row["section_title"] or "")
                    or str(row["body"] or "")
                )
                if relationship == "adjacent":
                    neighbors = list(connection.execute(
                        self._passage_select_sql() +
                        " WHERE p.book_id=? AND p.revision=? AND d.active_revision=p.revision"
                        " AND p.ordinal BETWEEN ? AND ? AND p.id<>? ORDER BY p.ordinal LIMIT ?",
                        (seed_book, int(row["revision"]), max(0, int(row["ordinal"]) - limit),
                         int(row["ordinal"]) + limit, int(passage_id), limit),
                    ))
                    return self._related_envelope(
                        relationship, [self._passage_hit(item, index + 1, 700)
                                       for index, item in enumerate(neighbors)],
                    )
            if book_id is not None:
                book = connection.execute("SELECT * FROM books WHERE id=?", (int(book_id),)).fetchone()
                if book is None:
                    raise RetrievalError("BOOK_NOT_FOUND", "The requested seed book is not indexed.")
                if relationship == "adjacent":
                    raise RetrievalError(
                        "INVALID_ARGUMENT",
                        "Adjacent relationships require passage_id or citation_id so source order is exact.",
                    )
                seed_author = str(book["author"] or "")
                seed_subjects = str(book["subjects"] or "")
                seed_text = (
                    seed_subjects
                    or str(book["title"] or "")
                    or str(book["description"] or "")
                    or seed_author
                )
                first = connection.execute(
                    "SELECT f.body FROM rag_passages p JOIN rag_documents d ON d.book_id=p.book_id"
                    " AND d.active_revision=p.revision JOIN rag_fts_rowid m ON m.passage_id=p.id"
                    " JOIN rag_passages_fts f ON f.rowid=m.fts_row WHERE p.book_id=?"
                    " ORDER BY p.ordinal LIMIT 1",
                    (int(book_id),),
                ).fetchone() if ready else None
                if first is not None:
                    seed_text = seed_text or str(first[0])[:4000]
            if text is not None and relationship in {"adjacent", "same_author", "same_subject"}:
                raise RetrievalError(
                    "INVALID_ARGUMENT",
                    f"The {relationship} relationship requires an indexed book or passage seed.",
                )
            if relationship in {"same_author", "same_subject"}:
                metadata_value = seed_author if relationship == "same_author" else seed_subjects
                if not _metadata_terms(metadata_value, relationship):
                    label = "author" if relationship == "same_author" else "subject"
                    raise RetrievalError(
                        "METADATA_UNAVAILABLE",
                        f"The seed has no usable {label} metadata for {relationship} retrieval.",
                        retryable=False,
                    )
                metadata_book_ids, metadata_truncated = self._metadata_related_book_ids(
                    connection,
                    relationship=relationship,
                    seed_value=metadata_value,
                )
                if exclude_same_book and seed_book is not None:
                    metadata_book_ids = [value for value in metadata_book_ids if value != seed_book]
                if not ready:
                    rows = self._book_head_rows(connection, metadata_book_ids, limit)
                    hits = [
                        self._bootstrap_hit(row, index + 1, 700)
                        for index, row in enumerate(rows)
                    ]
                    contributor = (
                        "metadata-author-identity"
                        if relationship == "same_author"
                        else "metadata-subject-overlap"
                    )
                    for hit in hits:
                        hit["contributors"].append(contributor)
                    warnings = [
                        "Passage coverage is unavailable; returning metadata-filtered book-head results."
                    ]
                    if metadata_truncated:
                        warnings.append("Metadata candidate set was capped at 500 books.")
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "lumen_related",
                        "request_id": _request_id(),
                        "corpus_revision": 0,
                        "relationship": relationship,
                        "backend": BackendReport(
                            relationship,
                            ["sqlite-catalog-metadata", "sqlite-fts5-book-head", contributor],
                        ).as_dict(),
                        "coverage": self._coverage(connection, self._root_scopes(connection)),
                        "partial": metadata_truncated,
                        "warnings": warnings,
                        "hits": hits,
                        "next_cursor": None,
                    }
        if relationship == "same_author":
            query = _related_seed_query(seed_author)
        elif relationship == "same_subject":
            query = _related_seed_query(seed_subjects)
        else:
            query = _related_seed_query(seed_text)
        if relationship == "contrasting":
            query = "however not contrary " + query
        search_books: Sequence[int] = metadata_book_ids if relationship in {
            "same_author", "same_subject"
        } else ()
        expression_override = (
            _metadata_any_expression(seed_subjects)
            if relationship == "same_subject"
            else None
        )
        if relationship in {"same_author", "same_subject"} and not search_books:
            result = self.search(
                query,
                strategy=strategy,
                book_ids=[int(seed_book or 1)],
                limit=1,
                _expression=expression_override,
            )
            result["hits"] = []
        else:
            result = self.search(
                query,
                strategy=strategy,
                book_ids=search_books,
                limit=min(MAX_LIMIT, limit * 2),
                _expression=expression_override,
            )
        if exclude_same_book and seed_book is not None:
            result["hits"] = [hit for hit in result["hits"] if hit["book"]["id"] != seed_book][:limit]
        else:
            result["hits"] = result["hits"][:limit]
        result["operation"] = "lumen_related"
        result["relationship"] = relationship
        if relationship in {"same_author", "same_subject"}:
            contributor = (
                "metadata-author-identity"
                if relationship == "same_author"
                else "metadata-subject-overlap"
            )
            result["backend"]["used"].append(contributor)
            for hit in result["hits"]:
                hit["contributors"].append(contributor)
            if metadata_truncated:
                result["partial"] = True
                result["warnings"].append("Metadata candidate set was capped at 500 books.")
        if relationship == "contrasting":
            result["warnings"].append(
                "Contrasting hits are retrieval candidates; Lumen does not assert logical contradiction."
            )
        return result

    def get_book(
        self,
        book_id: int,
        *,
        include_toc: bool = True,
        include_coverage: bool = True,
        include_representative_passages: bool = False,
    ) -> dict[str, Any]:
        with self.pool.connection() as connection:
            ready = passage_schema_available(connection)
            if ready:
                sql = (
                    "SELECT b.*,d.active_revision,d.coverage,d.coverage_reason,d.status,"
                    "d.section_count,d.passage_count,d.char_count,d.indexed_at FROM books b"
                    " LEFT JOIN rag_documents d ON d.book_id=b.id WHERE b.id=?"
                )
            else:
                sql = (
                    "SELECT b.*,NULL AS active_revision,'metadata_only' AS coverage,"
                    "'Passage schema is not built.' AS coverage_reason,'catalog' AS status,"
                    "0 AS section_count,0 AS passage_count,0 AS char_count,NULL AS indexed_at"
                    " FROM books b WHERE b.id=?"
                )
            row = connection.execute(sql, (int(book_id),)).fetchone()
            if row is None:
                raise RetrievalError("BOOK_NOT_FOUND", "The requested book is not indexed.")
            result: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "operation": "lumen_get_book",
                "request_id": _request_id(),
                "book": self._book_dict(row),
                "resource_uri": f"lumen://book/{int(row['id'])}",
                "can_open_in_lumen": False,
            }
            if include_coverage:
                result["coverage"] = {
                    "status": str(row["coverage"] or "metadata_only"),
                    "reason": str(row["coverage_reason"] or ""),
                    "active_revision": row["active_revision"],
                    "sections": int(row["section_count"] or 0),
                    "passages": int(row["passage_count"] or 0),
                    "characters": int(row["char_count"] or 0),
                    "indexed_at": row["indexed_at"],
                }
            if include_toc and row["active_revision"] is not None:
                result["toc"] = [
                    {
                        "ordinal": int(item["ordinal"]),
                        "title": str(item["title"]),
                        "kind": str(item["section_kind"]),
                        "href": str(item["href"]),
                        "page_start": item["page_start"],
                        "page_end": item["page_end"],
                        "resource_uri": f"lumen://book/{int(row['id'])}/section/{int(item['ordinal'])}",
                    }
                    for item in connection.execute(
                        "SELECT * FROM rag_sections WHERE book_id=? AND revision=? ORDER BY ordinal",
                        (int(row["id"]), int(row["active_revision"])),
                    )
                ]
            if include_representative_passages and row["active_revision"] is not None:
                result["representative_passages"] = [
                    f"lumen://passage/{int(item[0])}?revision={int(row['active_revision'])}"
                    for item in connection.execute(
                        "SELECT id FROM rag_passages WHERE book_id=? AND revision=?"
                        " ORDER BY ordinal LIMIT 5",
                        (int(row["id"]), int(row["active_revision"])),
                    )
                ]
            return result

    def explain_query(self, operation: str, query: str, strategy: str = "auto") -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "lumen_explain_query",
            "request_id": _request_id(),
            "plan": explain_plan(operation, query, strategy),
            "limits": {
                "results": MAX_LIMIT,
                "excerpt_chars": MAX_EXCERPT,
                "regex_candidates": MAX_REGEX_CANDIDATES,
                "regex_text_bytes": MAX_REGEX_TEXT,
            },
        }

    # ── resources ────────────────────────────────────────────────────────

    def read_passage(
        self,
        passage_id: int,
        *,
        revision: int | None = None,
        before: int = 0,
        after: int = 0,
    ) -> str:
        before = max(0, min(5, int(before)))
        after = max(0, min(5, int(after)))
        with self.pool.connection() as connection:
            self._require_passages(connection)
            row = self._passage_row(connection, int(passage_id), revision)
            rows = [row]
            if before or after:
                rows = list(connection.execute(
                    self._passage_select_sql() +
                    " WHERE p.book_id=? AND p.revision=? AND p.ordinal BETWEEN ? AND ?"
                    " ORDER BY p.ordinal",
                    (int(row["book_id"]), int(row["revision"]),
                     max(0, int(row["ordinal"]) - before), int(row["ordinal"]) + after),
                ))
            blocks: list[str] = []
            total = 0
            for item in rows:
                body = str(item["body"])
                block = (
                    f"Source: {item['title']} — {item['author']}\n"
                    f"Location: {self._location(item)} · passage {int(item['ordinal'])}\n"
                    f"Revision: {int(item['revision'])} · SHA-256: {item['content_sha256']}\n"
                    f"Coverage: {item['coverage']}\n\n{body}"
                )
                if total + len(block.encode("utf-8")) > 65_536:
                    break
                blocks.append(block)
                total += len(block.encode("utf-8"))
            return "\n\n---\n\n".join(blocks)

    def read_book_resource(self, book_id: int) -> str:
        return json.dumps(self.get_book(book_id), ensure_ascii=False, indent=2)

    def read_section(self, book_id: int, section_ordinal: int) -> str:
        with self.pool.connection() as connection:
            self._require_passages(connection)
            rows = list(connection.execute(
                self._passage_select_sql() +
                " WHERE p.book_id=? AND d.active_revision=p.revision AND p.section_ordinal=?"
                " ORDER BY p.ordinal",
                (int(book_id), int(section_ordinal)),
            ))
            if not rows:
                raise RetrievalError("BOOK_NOT_FOUND", "The requested section is not indexed.")
            text = "\n\n".join(str(row["body"]) for row in rows)
            return text[:65_536]

    def read_citation(self, citation_id: str) -> str:
        payload = self.citations.decode(citation_id)
        with self.pool.connection() as connection:
            self._require_passages(connection)
            row = self._passage_row(connection, int(payload["p"]), int(payload["r"]))
            if str(row["content_sha256"])[:16] != str(payload["h"]):
                raise RetrievalError("STALE_RESOURCE", "Citation hash no longer resolves.")
        return self.read_passage(int(payload["p"]), revision=int(payload["r"]))

    def read_uri(self, uri: str) -> str:
        parsed = urlsplit(uri)
        if parsed.scheme != "lumen":
            raise RetrievalError("INVALID_ARGUMENT", "Only lumen:// resources are accepted.")
        parts = [part for part in (parsed.netloc, *parsed.path.split("/")) if part]
        query = parse_qs(parsed.query)
        if parts == ["corpus", "status"]:
            return json.dumps(self.status(), ensure_ascii=False, indent=2)
        if len(parts) >= 2 and parts[0] == "book":
            book_id = int(parts[1])
            if len(parts) == 4 and parts[2] == "section":
                return self.read_section(book_id, int(parts[3]))
            return self.read_book_resource(book_id)
        if len(parts) >= 2 and parts[0] == "passage":
            revision = int(query["revision"][0]) if query.get("revision") else None
            before = int(query.get("before", [0])[0])
            after = int(query.get("after", [0])[0])
            return self.read_passage(int(parts[1]), revision=revision, before=before, after=after)
        if len(parts) >= 2 and parts[0] == "citation":
            return self.read_citation(parts[1])
        raise RetrievalError("INVALID_ARGUMENT", "Unknown Lumen resource URI.")

    # ── SQL/query helpers ─────────────────────────────────────────────────

    def _root_scopes(self, connection: sqlite3.Connection) -> list[RootScope]:
        return [
            RootScope(_root_id(str(row[0])), str(row[0]), int(row[1]))
            for row in connection.execute(
                "SELECT root,COUNT(*) FROM books GROUP BY root ORDER BY root"
            )
        ]

    def _select_roots(
        self, connection: sqlite3.Connection, requested: Sequence[str]
    ) -> list[RootScope]:
        scopes = self._root_scopes(connection)
        if not requested:
            return scopes
        mapping = {scope.root_id: scope for scope in scopes}
        missing = [value for value in requested if value not in mapping]
        if missing:
            raise RetrievalError(
                "ROOT_NOT_AUTHORIZED",
                "One or more root IDs are unknown or unauthorized.",
                retryable=False,
                details={"root_ids": missing[:32]},
            )
        return [mapping[value] for value in requested]

    def _glob_candidates(
        self,
        connection: sqlite3.Connection,
        roots: Sequence[RootScope],
        pattern: str,
        target: str,
        formats: Sequence[str],
        sort: str,
        offset: int,
        *,
        passage_ready: bool,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT b.*,COALESCE(d.coverage,'metadata_only') AS coverage"
            " FROM books b LEFT JOIN rag_documents d ON d.book_id=b.id WHERE 1=1"
            if passage_ready else
            "SELECT b.*,'metadata_only' AS coverage FROM books b WHERE 1=1"
        )
        parameters: list[Any] = []
        sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
        sql, parameters = _add_in(sql, parameters, "b.ext", formats)
        prefix = fixed_prefix(pattern)
        column = {"filename": "b.name", "title": "b.title", "author": "b.author",
                  "subject": "b.subjects", "publisher": "b.publisher"}.get(target)
        if prefix and column:
            sql += f" AND {column} LIKE ? ESCAPE '\\'"
            parameters.append(_like_escape(prefix) + "%")
        order = {
            "title": "b.title COLLATE NOCASE,b.id",
            "modified": "b.mtime_ns DESC,b.id",
            "size": "b.size DESC,b.id",
        }.get(sort, "b.path COLLATE NOCASE,b.id")
        cap = min(10_000, max(500, MAX_LIMIT * 20))
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        parameters.extend([cap, max(0, int(offset))])
        return list(connection.execute(sql, parameters))

    def _glob_section_candidates(
        self,
        connection: sqlite3.Connection,
        roots: Sequence[RootScope],
        formats: Sequence[str],
        *,
        offset: int,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT b.id AS book_id,b.*,d.coverage,s.ordinal AS section_ordinal,"
            "s.title AS section_title,s.href FROM rag_sections s"
            " JOIN rag_documents d ON d.book_id=s.book_id AND d.active_revision=s.revision"
            " JOIN books b ON b.id=s.book_id WHERE 1=1"
        )
        parameters: list[Any] = []
        sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
        sql, parameters = _add_in(sql, parameters, "b.ext", formats)
        sql += " ORDER BY b.path COLLATE NOCASE,s.ordinal LIMIT 10000 OFFSET ?"
        parameters.append(max(0, int(offset)))
        return list(connection.execute(sql, parameters))

    @staticmethod
    def _glob_value(row: sqlite3.Row, target: str) -> tuple[str, str]:
        relative = RetrievalService._relative_path(row)
        values = {
            "path": relative,
            "filename": str(row["name"]),
            "title": str(row["title"]),
            "author": str(row["author"]),
            "subject": str(row["subjects"]),
            "publisher": str(row["publisher"]),
        }
        if target == "any_metadata":
            return "/".join(values[key] for key in ("title", "author", "subject", "publisher", "filename")), relative
        return values[target], relative

    @staticmethod
    def _relative_path(row: sqlite3.Row) -> str:
        try:
            relative = Path(str(row["path"])).relative_to(Path(str(row["root"]))).as_posix()
        except (ValueError, OSError):
            relative = str(row["name"])
        return relative

    @staticmethod
    def _passage_index_ready(connection: sqlite3.Connection) -> bool:
        """True only when the optional passage schema has searchable content.

        ``LibraryIndex`` installs the additive schema when it opens a database,
        so schema existence alone cannot mean that the separate MCP builder has
        run.  Treating an empty schema as ready bypassed the legacy FTS fallback
        and made grep/related return no hits after restoring the fast sweep.
        """
        if not passage_schema_available(connection):
            return False
        return connection.execute(
            "SELECT 1 FROM rag_documents"
            " WHERE active_revision IS NOT NULL AND passage_count>0 LIMIT 1"
        ).fetchone() is not None

    @staticmethod
    def _require_passages(connection: sqlite3.Connection) -> None:
        if not RetrievalService._passage_index_ready(connection):
            raise RetrievalError(
                "PASSAGE_INDEX_UNAVAILABLE",
                "The optional MCP passage index has not been built yet.",
                suggested_action="Run a Lumen sweep, then `lumen-mcp index build` for complete coverage.",
            )

    def _passage_search_rows(
        self,
        connection: sqlite3.Connection,
        expression: str,
        roots: Sequence[RootScope],
        formats: Sequence[str],
        languages: Sequence[str],
        book_ids: Sequence[int],
        coverage: str,
        limit: int,
        offset: int,
    ) -> list[sqlite3.Row]:
        sql = self._passage_select_sql(ranked=True) + (
            " WHERE rag_passages_fts MATCH ? AND d.active_revision=p.revision"
        )
        parameters: list[Any] = [expression]
        sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
        sql, parameters = _add_in(sql, parameters, "b.ext", formats)
        sql, parameters = _add_in(sql, parameters, "LOWER(b.language)", languages)
        sql, parameters = _add_in(sql, parameters, "b.id", book_ids)
        if coverage == "complete_only":
            sql += " AND d.coverage='complete'"
        sql += " ORDER BY rank,p.id LIMIT ? OFFSET ?"
        parameters.extend([min(1000, int(limit)), max(0, int(offset))])
        try:
            return list(connection.execute(sql, parameters))
        except sqlite3.OperationalError:
            return []

    def _grep_candidates(
        self,
        connection: sqlite3.Connection,
        expression: str | None,
        roots: Sequence[RootScope],
        formats: Sequence[str],
        book_ids: Sequence[int],
        *,
        ready: bool,
        offset: int,
    ) -> list[sqlite3.Row]:
        parameters: list[Any] = []
        if ready:
            sql = self._passage_select_sql() + " WHERE d.active_revision=p.revision"
            if expression:
                sql += " AND rag_passages_fts MATCH ?"
                parameters.append(expression)
            sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
            sql, parameters = _add_in(sql, parameters, "b.ext", formats)
            sql, parameters = _add_in(sql, parameters, "b.id", book_ids)
            sql += " ORDER BY p.id LIMIT ? OFFSET ?"
            parameters.extend([MAX_REGEX_CANDIDATES, max(0, int(offset))])
            try:
                return list(connection.execute(sql, parameters))
            except sqlite3.OperationalError:
                pass
        sql = (
            "SELECT b.id AS book_id,b.*,content_fts.body AS body,NULL AS passage_id,"
            "0 AS revision,0 AS ordinal,'book_head' AS section_kind,'' AS section_title,"
            "'' AS href,NULL AS page_start,NULL AS page_end,'' AS content_sha256,"
            "'capped' AS coverage,0.0 AS rank FROM content_fts JOIN books b"
            " ON b.id=content_fts.book_id WHERE 1=1"
        )
        parameters = []
        if expression:
            sql += " AND content_fts MATCH ?"
            parameters.append(expression)
        sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
        sql, parameters = _add_in(sql, parameters, "b.ext", formats)
        sql, parameters = _add_in(sql, parameters, "b.id", book_ids)
        sql += " ORDER BY b.id LIMIT ? OFFSET ?"
        parameters.extend([MAX_REGEX_CANDIDATES, max(0, int(offset))])
        return list(connection.execute(sql, parameters))

    def _bootstrap_search_rows(
        self,
        connection: sqlite3.Connection,
        expression: str,
        roots: Sequence[RootScope],
        formats: Sequence[str],
        book_ids: Sequence[int],
        limit: int,
        offset: int,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT b.id AS book_id,b.*,content_fts.body AS body,"
            "snippet(content_fts,0,'','',' … ',40) AS snippet,bm25(content_fts) AS rank"
            " FROM content_fts JOIN books b ON b.id=content_fts.book_id"
            " WHERE content_fts MATCH ?"
        )
        parameters: list[Any] = [expression]
        sql, parameters = _add_in(sql, parameters, "b.root", [root.path for root in roots])
        sql, parameters = _add_in(sql, parameters, "b.ext", formats)
        sql, parameters = _add_in(sql, parameters, "b.id", book_ids)
        sql += " ORDER BY rank,b.id LIMIT ? OFFSET ?"
        parameters.extend([min(1000, int(limit)), max(0, int(offset))])
        try:
            return list(connection.execute(sql, parameters))
        except sqlite3.OperationalError:
            return []

    @staticmethod
    def _metadata_related_book_ids(
        connection: sqlite3.Connection,
        *,
        relationship: str,
        seed_value: str,
    ) -> tuple[list[int], bool]:
        seed_terms = _metadata_terms(seed_value, relationship)
        field = "author" if relationship == "same_author" else "subjects"
        matches: list[int] = []
        for row in connection.execute(f"SELECT id,{field} FROM books ORDER BY id"):
            candidate_terms = _metadata_terms(str(row[field] or ""), relationship)
            if relationship == "same_author":
                matched = bool(seed_terms and seed_terms == candidate_terms)
            else:
                matched = bool(seed_terms.intersection(candidate_terms))
            if matched:
                matches.append(int(row["id"]))
                if len(matches) > 500:
                    return matches[:500], True
        return matches, False

    @staticmethod
    def _book_head_rows(
        connection: sqlite3.Connection,
        book_ids: Sequence[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        if not book_ids:
            return []
        placeholders = ",".join("?" for _ in book_ids)
        return list(connection.execute(
            "SELECT b.id AS book_id,b.*,COALESCE(content_fts.body,'') AS body,"
            "0.0 AS rank FROM books b LEFT JOIN content_fts ON content_fts.book_id=b.id"
            f" WHERE b.id IN ({placeholders}) ORDER BY b.title COLLATE NOCASE,b.id LIMIT ?",
            [*book_ids, int(limit)],
        ))

    @staticmethod
    def _passage_select_sql(*, ranked: bool = False) -> str:
        rank = "bm25(rag_passages_fts)" if ranked else "0.0"
        return (
            "SELECT p.id AS passage_id,p.book_id,p.revision,p.ordinal,p.section_ordinal,"
            "p.char_start,p.char_end,p.page_start,p.page_end,p.content_sha256,"
            "s.section_kind,s.title AS section_title,s.href,s.fragment,"
            f"rag_passages_fts.body,b.*,d.coverage,d.coverage_reason,{rank} AS rank"
            " FROM rag_passages_fts JOIN rag_passages p"
            " ON p.id=rag_passages_fts.passage_id"
            " JOIN rag_documents d ON d.book_id=p.book_id"
            " JOIN rag_sections s ON s.id=p.section_id"
            " JOIN books b ON b.id=p.book_id"
        )

    def _passage_row(
        self, connection: sqlite3.Connection, passage_id: int, revision: int | None
    ) -> sqlite3.Row:
        sql = self._passage_select_sql() + " WHERE p.id=?"
        parameters: list[Any] = [int(passage_id)]
        if revision is None:
            sql += " AND d.active_revision=p.revision"
        else:
            sql += " AND p.revision=? AND rtrim(p.content_sha256)<>''"
            parameters.append(int(revision))
        row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise RetrievalError("STALE_RESOURCE", "Passage is missing, stale, or outside the active revision.")
        return row

    def _passage_hit(
        self,
        row: sqlite3.Row,
        rank_number: int,
        excerpt_chars: int,
        *,
        excerpt: str | None = None,
    ) -> dict[str, Any]:
        body = str(row["body"] or "")
        rendered = excerpt if excerpt is not None else _bounded_excerpt(body, excerpt_chars)
        raw_rank = float(row["rank"] or 0.0)
        score = 1.0 / (1.0 + abs(raw_rank))
        citation = self.citations.encode(
            book_id=int(row["book_id"]), revision=int(row["revision"]),
            passage_id=int(row["passage_id"]), content_hash=str(row["content_sha256"]),
        )
        return {
            "rank": rank_number,
            "score": score,
            "score_kind": "fts5_bm25_inverse",
            "contributors": ["sqlite-fts5"],
            "citation_id": citation,
            "resource_uri": (
                f"lumen://passage/{int(row['passage_id'])}?revision={int(row['revision'])}"
            ),
            "book": self._book_dict(row),
            "locator": {
                "kind": str(row["section_kind"]),
                "section_ordinal": int(row["section_ordinal"]),
                "section_title": str(row["section_title"]),
                "href": str(row["href"]),
                "fragment": str(row["fragment"]),
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "passage_ordinal": int(row["ordinal"]),
                "char_start": int(row["char_start"]),
                "char_end": int(row["char_end"]),
            },
            "excerpt": rendered,
            "match_ranges": [],
            "passage_sha256": str(row["content_sha256"]),
            "coverage": str(row["coverage"]),
            "precision": "passage",
            "modified_at_ns": int(row["mtime_ns"]),
        }

    def _bootstrap_hit(
        self,
        row: sqlite3.Row,
        rank_number: int,
        excerpt_chars: int,
        *,
        excerpt: str | None = None,
    ) -> dict[str, Any]:
        raw_body = row["body"] if "body" in row.keys() and row["body"] else (
            row["snippet"] if "snippet" in row.keys() else ""
        )
        body = str(raw_body or "")
        rendered = excerpt if excerpt is not None else _bounded_excerpt(body, excerpt_chars)
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {
            "rank": rank_number,
            "score": 1.0 / (1.0 + abs(float(row["rank"] or 0.0))),
            "score_kind": "fts5_bm25_inverse",
            "contributors": ["sqlite-fts5-book-head"],
            "citation_id": None,
            "resource_uri": f"lumen://book/{int(row['book_id'])}",
            "book": self._book_dict(row),
            "locator": {
                "kind": "book_head",
                "section_ordinal": None,
                "section_title": "",
                "href": "",
                "fragment": "",
                "page_start": None,
                "page_end": None,
                "passage_ordinal": None,
                "char_start": 0,
                "char_end": len(body),
            },
            "excerpt": rendered,
            "match_ranges": [],
            "passage_sha256": content_hash,
            "coverage": "capped",
            "precision": "book_level",
            "modified_at_ns": int(row["mtime_ns"]),
        }

    @staticmethod
    def _book_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["book_id"] if "book_id" in row.keys() else row["id"]),
            "title": str(row["title"]),
            "authors": [part.strip() for part in str(row["author"]).split(",") if part.strip()],
            "author": str(row["author"]),
            "format": str(row["ext"]).lstrip("."),
            "language": str(row["language"]),
            "publisher": str(row["publisher"]),
            "subjects": str(row["subjects"]),
            "description": str(row["description"]),
            "path": str(row["path"]),
            "name": str(row["name"]),
            "size_bytes": int(row["size"]),
            "modified_ns": int(row["mtime_ns"]),
            "pages": int(row["pages"]),
            "readable": bool(row["ok"]),
            "error": str(row["error"]),
        }

    @staticmethod
    def _location(row: sqlite3.Row) -> str:
        if row["page_start"] is not None:
            return f"page {int(row['page_start'])}"
        if row["section_title"]:
            return f"{row['section_title']} · {row['href']}"
        return f"section {int(row['section_ordinal'])}"

    @staticmethod
    def _coverage(connection: sqlite3.Connection, roots: Sequence[RootScope]) -> dict[str, Any]:
        root_paths = [root.path for root in roots]
        sql = "SELECT COUNT(*) FROM books b WHERE 1=1"
        parameters: list[Any] = []
        sql, parameters = _add_in(sql, parameters, "b.root", root_paths)
        documents = int(connection.execute(sql, parameters).fetchone()[0])
        try:
            sql = (
                "SELECT COALESCE(SUM(CASE WHEN d.coverage='complete' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN d.coverage<>'complete' THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(d.passage_count),0) FROM rag_documents d JOIN books b ON b.id=d.book_id"
                " WHERE d.active_revision IS NOT NULL"
            )
            parameters = []
            sql, parameters = _add_in(sql, parameters, "b.root", root_paths)
            row = connection.execute(sql, parameters).fetchone()
            complete, partial, passages = map(int, row)
        except sqlite3.Error:
            complete = partial = passages = 0
        unbuilt = max(0, documents - complete - partial)
        return {
            "documents_in_scope": documents,
            "documents_complete": complete,
            "documents_partial": partial + unbuilt,
            "passages_in_scope": passages,
            "is_complete_for_scope": documents > 0 and complete == documents,
        }

    def _cursor_offset(
        self,
        cursor: str | None,
        operation: str,
        query_digest: str,
        revision: int,
        root_digest: str,
    ) -> int:
        if not cursor:
            return 0
        payload = self.cursors.decode(
            cursor, operation=operation, query_digest=query_digest,
            corpus_revision=revision, root_digest=root_digest,
        )
        return max(0, int(payload.get("n", 0)))

    @staticmethod
    def _related_envelope(relationship: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "lumen_related",
            "request_id": _request_id(),
            "relationship": relationship,
            "backend": BackendReport("adjacent", ["passage-adjacency"]).as_dict(),
            "partial": False,
            "warnings": [],
            "hits": hits,
            "next_cursor": None,
        }


def _add_in(
    sql: str, parameters: list[Any], column: str, values: Sequence[Any]
) -> tuple[str, list[Any]]:
    if values:
        sql += f" AND {column} IN ({','.join('?' for _ in values)})"
        parameters.extend(values)
    return sql, parameters


def _root_id(path: str) -> str:
    return "root_" + hashlib.sha256(os.path.normcase(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _root_digest(roots: Sequence[RootScope]) -> str:
    return hashlib.sha256("\0".join(sorted(root.root_id for root in roots)).encode("utf-8")).hexdigest()


def _query_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _request_id() -> str:
    return uuid.uuid4().hex


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _limit(value: int, default: int) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError):
        return default


def _excerpt(value: int) -> int:
    try:
        return max(120, min(MAX_EXCERPT, int(value)))
    except (TypeError, ValueError):
        return 700


def _formats(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        normalized = str(value).strip().casefold().lstrip(".")
        if normalized not in {"epub", "pdf"}:
            raise RetrievalError("INVALID_ARGUMENT", f"Unsupported book format: {value}.")
        extension = "." + normalized
        if extension not in output:
            output.append(extension)
    return output


def _book_ids(values: Sequence[int]) -> list[int]:
    output = [int(value) for value in values]
    if len(output) > 500 or any(value <= 0 for value in output):
        raise RetrievalError("INVALID_ARGUMENT", "book_ids must contain 1–500 positive integers.")
    return output


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bounded_excerpt(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    end = text.rfind(" ", 0, maximum)
    if end < maximum // 2:
        end = maximum
    return text[:end].rstrip() + " …"


def _related_seed_query(text: str) -> str:
    """Select a small deterministic lexical signature instead of AND-ing a passage."""
    output: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"\w{3,}", text, flags=re.UNICODE):
        normalized = word.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(word)
        if len(output) >= 4:
            break
    return " ".join(output) or text.strip()[:128]


def _metadata_terms(value: str, relationship: str) -> set[str]:
    """Normalize exact author identities or individual subject labels for safe matching."""
    cleaned = " ".join(str(value).split()).casefold()
    if not cleaned or cleaned in {"unknown", "unknown author"}:
        return set()
    if relationship == "same_author":
        return {cleaned}
    return {
        " ".join(part.split()).casefold()
        for part in re.split(r"[,;]", cleaned)
        if " ".join(part.split())
    }


def _metadata_any_expression(value: str) -> str:
    """Build a server-owned FTS OR over normalized subject labels."""
    terms = sorted(_metadata_terms(value, "same_subject"))
    return " OR ".join(safe_fts_query(term, phrase=True) for term in terms)
