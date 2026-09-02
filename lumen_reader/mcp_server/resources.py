"""Bounded, opaque-ID MCP resources for indexed Lumen content."""

from __future__ import annotations

from typing import Any

from ..retrieval.service import RetrievalService


def register_resources(server: Any, retrieval: RetrievalService) -> None:
    @server.resource(
        "lumen://corpus/status",
        name="Lumen corpus status",
        description="Current roots, passage coverage, corpus revision, and retrieval backends.",
        mime_type="application/json",
    )
    def corpus_status() -> str:
        return retrieval.read_uri("lumen://corpus/status")

    @server.resource(
        "lumen://book/{book_id}",
        name="Lumen book",
        description="Indexed book metadata and passage coverage resolved from an opaque book ID.",
        mime_type="application/json",
    )
    def book(book_id: int) -> str:
        return retrieval.read_book_resource(book_id)

    @server.resource(
        "lumen://book/{book_id}/section/{section_ordinal}",
        name="Lumen book section",
        description="A bounded indexed EPUB spine section or PDF page.",
        mime_type="text/plain",
    )
    def section(book_id: int, section_ordinal: int) -> str:
        return retrieval.read_section(book_id, section_ordinal)

    @server.resource(
        "lumen://passage/{passage_id}",
        name="Lumen passage",
        description="An exact active passage with source locator, revision, hash, and coverage.",
        mime_type="text/plain",
    )
    def passage(passage_id: int) -> str:
        return retrieval.read_passage(passage_id)

    @server.resource(
        "lumen://citation/{citation_id}",
        name="Lumen citation",
        description="Resolve a signed citation token to its exact retained passage revision.",
        mime_type="text/plain",
    )
    def citation(citation_id: str) -> str:
        return retrieval.read_citation(citation_id)
