"""Privacy-safe non-mutating doctor checks for the MCP sidecar."""

from __future__ import annotations

import importlib.metadata
import sqlite3
from pathlib import Path
from typing import Any

from ..passage_index import passage_schema_available
from ..retrieval.service import RetrievalService
from ..runtime_paths import RuntimePaths
from ..version import get_version
from .compat import SDK_GENERATION


def doctor(paths: RuntimePaths | None = None) -> dict[str, Any]:
    runtime = paths or RuntimePaths.discover()
    checks: list[dict[str, Any]] = []
    index_exists = runtime.index_file.is_file()
    checks.append(_check("runtime_directory", runtime.data_dir.is_dir(), str(runtime.data_dir)))
    checks.append(_check("index_exists", index_exists, str(runtime.index_file)))
    if index_exists:
        try:
            connection = sqlite3.connect(f"file:{runtime.index_file.as_posix()}?mode=ro", uri=True)
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            required = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    " AND name IN ('books','books_fts','content_fts')"
                )
            }
            passages = passage_schema_available(connection)
            connection.close()
            checks.append(_check(
                "sqlite_catalog",
                required == {"books", "books_fts", "content_fts"},
                f"schema_version={schema_version}; required={','.join(sorted(required))}",
            ))
            checks.append(_check("passage_schema", passages, "ready" if passages else "not built"))
        except sqlite3.Error as exception:
            checks.append(_check("sqlite_open", False, type(exception).__name__))
    try:
        sdk_version = importlib.metadata.version("mcp")
        checks.append(_check("mcp_sdk", True, f"{sdk_version} (generation {SDK_GENERATION})"))
    except importlib.metadata.PackageNotFoundError:
        checks.append(_check("mcp_sdk", False, "not installed"))
    status: dict[str, Any] | None = None
    if index_exists:
        try:
            status = RetrievalService(runtime).status(include_roots=False)
            checks.append(_check("retrieval_status", status.get("health") == "ready", str(status.get("health"))))
        except Exception as exception:
            checks.append(_check("retrieval_status", False, type(exception).__name__))
    failed = [item for item in checks if not item["ok"] and item["name"] != "passage_schema"]
    return {
        "application": "Lumen Book Reader MCP",
        "version": get_version(),
        "healthy": not failed,
        "checks": checks,
        "status": status,
        "repairs_performed": [],
    }


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}
