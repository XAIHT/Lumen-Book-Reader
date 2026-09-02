"""PyInstaller-safe console entry point for the headless Lumen MCP sidecar."""

from lumen_reader.mcp_server.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
