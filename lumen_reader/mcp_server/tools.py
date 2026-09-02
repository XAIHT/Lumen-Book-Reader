"""Registration of Lumen's seven default read-only MCP tools."""

from __future__ import annotations

import uuid
from typing import Any

from ..retrieval.contracts import RetrievalError
from ..retrieval.service import RetrievalService
from .compat import READ_ONLY, error_result


def _call(operation: Any, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return operation(*args, **kwargs)
    except RetrievalError as exception:
        return error_result(exception.as_dict(uuid.uuid4().hex))  # type: ignore[return-value]
    except (TypeError, ValueError) as exception:
        domain = RetrievalError(
            "INVALID_ARGUMENT",
            str(exception) or "The request contains an invalid argument.",
            retryable=False,
        )
        return error_result(domain.as_dict(uuid.uuid4().hex))  # type: ignore[return-value]


def register_tools(server: Any, retrieval: RetrievalService) -> None:
    @server.tool(
        name="lumen_status",
        description="Report Lumen index health, authorized roots, passage coverage, limits, and truthful backend/hardware state.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_status(
        include_roots: bool = True,
        include_backends: bool = True,
        include_recent_failures: bool = False,
    ) -> dict[str, Any]:
        return _call(
            retrieval.status,
            include_roots=include_roots,
            include_backends=include_backends,
            include_recent_failures=include_recent_failures,
        )

    @server.tool(
        name="lumen_glob",
        description="Glob over indexed relative paths and book metadata; never traverses caller-supplied filesystem paths.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_glob(
        pattern: str,
        target: str = "path",
        roots: list[str] | None = None,
        formats: list[str] | None = None,
        case_sensitive: str = "auto",
        include_sections: bool = False,
        sort: str = "path",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _call(
            retrieval.glob,
            pattern,
            target=target,
            roots=roots or (),
            formats=formats or (),
            case_sensitive=case_sensitive,
            include_sections=include_sections,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )

    @server.tool(
        name="lumen_grep",
        description="Find exact literal, phrase, FTS, or bounded-regex matches with verified ranges and precise passage locators.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_grep(
        query: str,
        mode: str = "literal",
        case_sensitive: bool = False,
        whole_word: bool = False,
        roots: list[str] | None = None,
        book_ids: list[int] | None = None,
        formats: list[str] | None = None,
        max_matches_per_book: int = 3,
        context_chars: int = 480,
        fallback: str = "none",
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _call(
            retrieval.grep,
            query,
            mode=mode,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            roots=roots or (),
            book_ids=book_ids or (),
            formats=formats or (),
            max_matches_per_book=max_matches_per_book,
            context_chars=context_chars,
            fallback=fallback,
            limit=limit,
            cursor=cursor,
        )

    @server.tool(
        name="lumen_search",
        description="Rank topical passages using SQLite FTS5 plus bounded offline WordNet semantic expansion.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_search(
        query: str,
        strategy: str = "auto",
        roots: list[str] | None = None,
        formats: list[str] | None = None,
        languages: list[str] | None = None,
        book_ids: list[int] | None = None,
        diversity: str = "book",
        max_per_book: int = 3,
        include_adjacent: bool = False,
        coverage: str = "include_partial",
        limit: int = 20,
        excerpt_chars: int = 700,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _call(
            retrieval.search,
            query,
            strategy=strategy,
            roots=roots or (),
            formats=formats or (),
            languages=languages or (),
            book_ids=book_ids or (),
            diversity=diversity,
            max_per_book=max_per_book,
            include_adjacent=include_adjacent,
            coverage=coverage,
            limit=limit,
            excerpt_chars=excerpt_chars,
            cursor=cursor,
        )

    @server.tool(
        name="lumen_related",
        description="Find adjacent, conceptual, author, subject, or possible counterevidence passages from exactly one seed.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_related(
        passage_id: int | None = None,
        book_id: int | None = None,
        citation_id: str | None = None,
        text: str | None = None,
        relationship: str = "conceptual",
        exclude_same_book: bool = False,
        strategy: str = "auto",
        limit: int = 20,
    ) -> dict[str, Any]:
        return _call(
            retrieval.related,
            passage_id=passage_id,
            book_id=book_id,
            citation_id=citation_id,
            text=text,
            relationship=relationship,
            exclude_same_book=exclude_same_book,
            strategy=strategy,
            limit=limit,
        )

    @server.tool(
        name="lumen_get_book",
        description="Return safe indexed metadata, passage coverage, TOC links, and optional representative passage URIs for one book.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_get_book(
        book_id: int,
        include_toc: bool = True,
        include_coverage: bool = True,
        include_representative_passages: bool = False,
    ) -> dict[str, Any]:
        return _call(
            retrieval.get_book,
            book_id,
            include_toc=include_toc,
            include_coverage=include_coverage,
            include_representative_passages=include_representative_passages,
        )

    @server.tool(
        name="lumen_explain_query",
        description="Validate and explain a bounded Lumen retrieval plan without executing the full content query.",
        annotations=READ_ONLY,
        structured_output=True,
    )
    def lumen_explain_query(
        operation: str,
        query: str,
        strategy: str = "auto",
    ) -> dict[str, Any]:
        return _call(retrieval.explain_query, operation, query, strategy)
