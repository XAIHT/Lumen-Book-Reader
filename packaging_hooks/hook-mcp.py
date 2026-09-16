"""Collect the SDK runtime without executing its optional developer CLI.

mcp.cli exits during import when the typer extra is absent. Lumen has its own
CLI in lumen_reader.mcp_server.cli and never calls the SDK's developer CLI.
Filter before traversal, since --exclude-module alone cannot prevent imports
performed by an unrestricted --collect-submodules mcp.
"""

from PyInstaller.utils.hooks import collect_submodules, is_module_or_submodule

hiddenimports = collect_submodules(
    "mcp", filter=lambda name: not is_module_or_submodule(name, "mcp.cli")
)
excludedimports = ["mcp.cli"]
