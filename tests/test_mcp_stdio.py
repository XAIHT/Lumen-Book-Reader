from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lumen_reader.mcp_server.compat import field, structured_payload


def test_real_stdio_initialize_discovery_and_status(tmp_path: Path) -> None:
    async def exercise() -> None:
        environment = dict(os.environ)
        environment["LUMEN_DATA_DIR"] = str(tmp_path)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lumen_reader.mcp_server", "serve", "--stdio"],
            cwd=Path(__file__).parents[1],
            env=environment,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                assert field(initialized, "server_info").name == "Lumen Book Reader"
                tools = await session.list_tools()
                assert len(tools.tools) == 7
                assert all(field(tool.annotations, "read_only_hint") for tool in tools.tools)
                status = await session.call_tool("lumen_status", {})
                assert field(status, "is_error") is False
                assert structured_payload(status)["health"] == "not_indexed"
                failed = await session.call_tool("lumen_search", {"query": "radio"})
                assert field(failed, "is_error") is True
                assert structured_payload(failed)["error"]["code"] == "INDEX_NOT_FOUND"

    anyio.run(exercise)
