"""Exercise all seven read tools through real STDIO against source or a release.

Run with the release Python and --executable path/to/LumenMCP.exe to check the
actual frozen artifact. Queries use the existing configured index; no sweep,
book edit, installation, or GUI launch is requested.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lumen_reader.mcp_server.compat import field, structured_payload


async def exercise(executable: str | None, query: str) -> dict:
    expected = {
        "lumen_status", "lumen_glob", "lumen_grep", "lumen_search",
        "lumen_related", "lumen_get_book", "lumen_explain_query",
    }
    parameters = StdioServerParameters(
        command=str(Path(executable).resolve()) if executable else sys.executable,
        args=(["serve", "--stdio"] if executable else
              ["-m", "lumen_reader.mcp_server", "serve", "--stdio"]),
        cwd=Path(__file__).resolve().parent,
        # Match the exported MCP descriptor. The development interpreter may
        # deliberately use system packages; release isolation is tested by the
        # build preflight, not imposed on this external-client smoke check.
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    report: dict = {"executable": parameters.command, "calls": {}}
    with anyio.fail_after(90):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                identity = field(initialized, "server_info")
                assert identity.name == "Lumen Book Reader", identity
                report["version"] = identity.version
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == expected
                assert all(field(tool.annotations, "read_only_hint") for tool in tools)
                report["tools"] = len(tools)
                templates = field(await session.list_resource_templates(), "resource_templates")
                report["resource_templates"] = len(templates)
                report["prompts"] = len((await session.list_prompts()).prompts)

                async def call(name: str, arguments: dict) -> dict:
                    result = await session.call_tool(name, arguments)
                    payload = structured_payload(result)
                    assert not field(result, "is_error"), (name, payload)
                    assert isinstance(payload, dict), (name, payload)
                    report["calls"][name] = {"ok": True}
                    if "hits" in payload:
                        report["calls"][name]["hits"] = len(payload["hits"])
                    return payload

                status = await call("lumen_status", {})
                assert status["health"] == "ready", status["health"]
                assert status["catalog"]["query_only"] is True
                report["corpus"] = status["corpus"]
                glob = await call("lumen_glob", {"pattern": "*", "limit": 3})
                assert glob["hits"], "Smoke check needs at least one indexed book."
                await call("lumen_explain_query", {"operation": "search", "query": query})
                search = await call("lumen_search", {"query": query, "limit": 3, "excerpt_chars": 240})
                assert search["hits"], "Choose --query with a known match in this library."
                book_id = search["hits"][0]["book"]["id"]
                book = await call("lumen_get_book", {"book_id": book_id})
                assert book["book"]["id"] == book_id
                grep = await call("lumen_grep", {"query": query, "mode": "fts", "limit": 2})
                assert grep["hits"], "Expected an FTS match for the chosen query."
                await call("lumen_related", {"book_id": book_id, "relationship": "same_author", "limit": 2})
                assert set(report["calls"]) == expected
    report["result"] = "PASS"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", help="frozen LumenMCP.exe; omitted means source server")
    parser.add_argument("--query", default="radio frequency", help="known topic in the configured library")
    args = parser.parse_args()
    report = anyio.run(exercise, args.executable, args.query)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
