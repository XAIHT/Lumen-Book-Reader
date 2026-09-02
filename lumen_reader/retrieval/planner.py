"""Cheap query planning explanations with no SQL or content execution."""

from __future__ import annotations

import re
from typing import Any

from .glob_engine import fixed_prefix
from .lexical import safe_fts_query
from .semantic import status as semantic_status


def explain(operation: str, query: str, strategy: str = "auto") -> dict[str, Any]:
    operation = operation.strip().casefold()
    if operation == "glob":
        return {
            "operation": "lumen_glob",
            "fixed_prefix": fixed_prefix(query),
            "candidate_scope": "small" if fixed_prefix(query) else "broad",
            "backend": "sqlite-catalog+glob-verifier",
            "warnings": [],
        }
    if operation in {"search", "grep"}:
        expression = safe_fts_query(query)
        semantic = semantic_status()
        requested = strategy if operation == "search" else "exact"
        used = "lexical" if requested in {"auto", "hybrid", "semantic"} and not semantic.available else requested
        return {
            "operation": f"lumen_{operation}",
            "normalized_expression": expression,
            "candidate_scope": "small" if len(re.findall(r"\w+", query)) >= 2 else "medium",
            "backend_requested": requested,
            "backend_used": used,
            "fallback_reason": semantic.reason if used == "lexical" and requested != "lexical" else "",
            "warnings": [],
        }
    return {
        "operation": operation,
        "candidate_scope": "rejected",
        "warnings": ["Supported operations are glob, grep, and search."],
    }
