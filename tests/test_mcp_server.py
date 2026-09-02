from __future__ import annotations

import asyncio
from pathlib import Path

from lumen_reader.mcp_server.compat import field, structured_payload
from lumen_reader.mcp_server.server import INSTRUCTIONS, create_server
from lumen_reader.runtime_paths import RuntimePaths


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        data_dir=tmp_path,
        state_file=tmp_path / "reader-state.json",
        index_file=tmp_path / "library-index.db",
        logs_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
    )


def test_server_exposes_only_the_seven_default_read_tools(tmp_path: Path) -> None:
    server = create_server(_paths(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == [
        "lumen_status",
        "lumen_glob",
        "lumen_grep",
        "lumen_search",
        "lumen_related",
        "lumen_get_book",
        "lumen_explain_query",
    ]
    assert all(field(tool.annotations, "read_only_hint") is True for tool in tools)
    assert all(field(tool.annotations, "destructive_hint") is False for tool in tools)
    assert all(
        field(tool, "output_schema") and field(tool, "output_schema")["type"] == "object"
        for tool in tools
    )
    assert "untrusted quoted source content" in INSTRUCTIONS


def test_status_tool_starts_cleanly_without_an_index(tmp_path: Path) -> None:
    server = create_server(_paths(tmp_path))
    payload = structured_payload(asyncio.run(server.call_tool("lumen_status", {})))
    assert payload["health"] == "not_indexed"
    assert payload["server"]["read_only"] is True


def test_resource_templates_never_expose_file_uri(tmp_path: Path) -> None:
    server = create_server(_paths(tmp_path))
    templates = asyncio.run(server.list_resource_templates())
    assert templates
    assert all(field(item, "uri_template").startswith("lumen://") for item in templates)
