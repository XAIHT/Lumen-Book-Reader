"""Keep current release references aligned without rewriting historical versions."""

import importlib.metadata
import json
import os
from pathlib import Path
import sys
import zipfile

import anyio

from lumen_reader.version import declared_version, get_version, semver_to_win32_tuple


def test_current_release_identity_across_docs_runtime_and_mcp(tmp_path):
    root = Path(__file__).resolve().parents[1]
    expected = declared_version()
    assert expected and get_version() == expected
    checks = {
        "README.md": f"badge/LUMEN-v{expected}-",
        "CODEX.md": f"Current implementation release: **v{expected}**",
        "LumenBookReader-Spec.md": f"**Server:** Lumen Book Reader {expected}",
        "LumenBookReader-MCPDesign.md": f"**Implemented release:** Lumen Book Reader {expected}",
        "THIRD_PARTY_NOTICES.md": f"most recent tagged release is {expected}.",
        "BookDates.md": f"Current development release: **{expected}**",
        "RELEASING.md": f"## Current release: {expected}",
        "CHANGELOG.md": f"## [{expected}]",
    }
    for filename, marker in checks.items():
        assert marker in (root / filename).read_text(encoding="utf-8"), filename

    commands = [(sys.executable, ["-m", "lumen_reader.mcp_server", "serve", "--stdio"])]
    release_path = os.environ.get("LUMEN_VERIFY_RELEASE")
    if release_path:
        # Optional release gate: inspect fresh binaries, never installed user data.
        import win32api
        release = Path(release_path)
        manifest = json.loads((release / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        assert manifest["version"] == expected
        assert importlib.metadata.version("lumen-epub-reader") == expected
        with zipfile.ZipFile(release / "pkg.zip") as payload:
            assert payload.testzip() is None
            for name in ("Lumen.exe", "LumenMCP.exe"):
                payload.extract(name, tmp_path)
        for executable in (tmp_path / "Lumen.exe", tmp_path / "LumenMCP.exe",
                           release / "Installer.exe", release / "Uninstaller.exe"):
            info = win32api.GetFileVersionInfo(str(executable), "\\")
            version = (info["FileVersionMS"] >> 16, info["FileVersionMS"] & 65535,
                       info["FileVersionLS"] >> 16, info["FileVersionLS"] & 65535)
            assert version == semver_to_win32_tuple(expected), executable.name
        commands.append((str(tmp_path / "LumenMCP.exe"), ["serve", "--stdio"]))

    async def verify_mcp():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from lumen_reader.mcp_server.compat import field

        for command, args in commands:
            parameters = StdioServerParameters(command=command, args=args, cwd=root,
                env={**os.environ, "LUMEN_DATA_DIR": str(tmp_path / "state"),
                     "LUMEN_INDEX_PATH": str(tmp_path / "state" / "index.db")})
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    initialized = await client.initialize()
                    assert field(field(initialized, "server_info"), "version") == expected
                    assert len((await client.list_tools()).tools) == 7

    anyio.run(verify_mcp)
