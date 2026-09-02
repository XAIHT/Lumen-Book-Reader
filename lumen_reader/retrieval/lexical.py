"""Safe FTS query normalization shared by lexical search and grep."""

from __future__ import annotations

from ..library_index import build_match_expression
from .contracts import RetrievalError


def safe_fts_query(query: str, *, phrase: bool = False) -> str:
    value = query.strip()
    if not value or len(value) > 4096:
        raise RetrievalError("INVALID_ARGUMENT", "Query must be 1–4,096 characters.")
    expression, _extensions = build_match_expression(f'"{value}"' if phrase else value)
    if not expression:
        raise RetrievalError("INVALID_ARGUMENT", "Query contains no searchable words.")
    return expression
