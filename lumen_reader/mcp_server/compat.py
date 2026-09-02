"""Compatibility boundary between the stable MCP SDK v2 and late v1 SDKs."""

from __future__ import annotations

import inspect
import json
from typing import Any


try:  # MCP Python SDK v2.
    from mcp.server.mcpserver import MCPServer as ServerClass

    SDK_GENERATION = 2
except ImportError:  # MCP Python SDK 1.28.x used by existing Lumen environments.
    from mcp.server.fastmcp import FastMCP as ServerClass

    SDK_GENERATION = 1

try:  # SDK v2 split protocol types into the mcp-types distribution.
    from mcp_types import CallToolResult, TextContent, ToolAnnotations
except ImportError:
    from mcp.types import CallToolResult, TextContent, ToolAnnotations


def make_server(*, name: str, version: str, instructions: str, **settings: Any) -> Any:
    """Construct a server without leaking SDK-version conditionals elsewhere."""
    parameters = inspect.signature(ServerClass).parameters
    values: dict[str, Any] = {"name": name, "instructions": instructions}
    if "version" in parameters:
        values["version"] = version
    for key, value in settings.items():
        if key in parameters:
            values[key] = value
    return ServerClass(**values)


def _read_only_annotations() -> ToolAnnotations:
    parameters = inspect.signature(ToolAnnotations).parameters
    names = (
        ("read_only_hint", "readOnlyHint", True),
        ("destructive_hint", "destructiveHint", False),
        ("idempotent_hint", "idempotentHint", True),
        ("open_world_hint", "openWorldHint", False),
    )
    values = {
        (snake if snake in parameters else camel): value
        for snake, camel, value in names
    }
    return ToolAnnotations(**values)


READ_ONLY = _read_only_annotations()


def error_result(payload: dict[str, Any]) -> CallToolResult:
    """Return a protocol-level error with both text and structured payloads."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    parameters = inspect.signature(CallToolResult).parameters
    values: dict[str, Any] = {
        "content": [TextContent(type="text", text=encoded)],
        "is_error" if "is_error" in parameters else "isError": True,
        "structured_content" if "structured_content" in parameters else "structuredContent": payload,
    }
    return CallToolResult(**values)


def field(model: Any, name: str) -> Any:
    """Read a protocol field named the v2 way off a model of either generation.

    SDK v2 renamed every wire field from camelCase to snake_case, and pydantic
    exposes only the declared name - `tool.annotations.readOnlyHint` raises
    AttributeError on v2, `read_only_hint` raises it on v1. Callers name the v2
    field; the camelCase spelling is derived only as the fallback.
    """
    try:
        return getattr(model, name)
    except AttributeError:
        head, *rest = name.split("_")
        return getattr(model, head + "".join(word.capitalize() for word in rest))


def structured_payload(result: Any) -> Any:
    """Decode the JSON payload of a low-level ``call_tool`` result.

    v1's FastMCP returned a ``(content_blocks, structured)`` tuple; v2 returns a
    ``CallToolResult``. Both carry the same payload twice - structured, and as
    JSON text for clients that cannot read structured output - so fall back to
    the text block rather than reporting the call as empty.
    """
    if isinstance(result, tuple):
        return result[1]
    blocks = result if isinstance(result, list) else field(result, "content")
    if not isinstance(result, list):
        structured = field(result, "structured_content")
        if structured is not None:
            return structured
    return json.loads(blocks[0].text)
