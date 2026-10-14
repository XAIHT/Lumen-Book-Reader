"""Visible native installer and frozen-MCP integration, using only temporary data."""
from __future__ import annotations

import hashlib
import json
import os
import tkinter as tk
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import install
from lumen_reader.library_index import LibraryIndex
from lumen_reader.mcp_server.compat import field, structured_payload
from lumen_reader.mcp_server.config_export import validate_file
from test_library_index import make_epub

pytestmark = pytest.mark.skipif(
    os.environ.get("LUMEN_VISIBLE_TESTS") != "1" or not os.environ.get("LUMEN_TEST_PACKAGE"),
    reason="Opt-in frozen integration: run tools/validate_mcp_install.py with --build or --package",
)


@pytest.fixture(scope="module")
def destination(tmp_path_factory):
    return tmp_path_factory.mktemp("MCP instalación 書籍 & (custom)") / "Lumen Book Reader"


@pytest.mark.parametrize("repair", [False, True], ids=["fresh-install", "repair-stale-config"])
def test_visible_installer_generates_handoff(destination, monkeypatch, repair):
    assert os.environ.get("LUMEN_VISIBLE_TESTS") == "1", "Use tools/validate_mcp_install.py"
    package = Path(os.environ["LUMEN_TEST_PACKAGE"])
    assert package.is_file(), f"Named prerequisite missing: {package}"
    if repair:
        previous = destination / install.MCP_CONFIG_NAME
        previous.write_bytes(b'{"old":"configuration from another installation"}')
    root = tk.Tk()
    root.attributes("-topmost", True)
    monkeypatch.setattr(install.LumenInstaller, "_find_beside",
                        staticmethod(lambda name: str(package) if name == "pkg.zip" else ""))
    monkeypatch.setattr(install.LumenInstaller, "_default_library",
                        staticmethod(lambda: str(destination.parent)))
    app = install.LumenInstaller(root)
    app.install_path.set(str(destination.parent))
    app.library_path.set(str(destination.parent))
    app.desktop_var.set(False)
    app.startmenu_var.set(False)
    for flag in app.assoc_vars.values():
        flag.set(False)
    # Only the Windows integrations are isolated. Actual buttons, progress,
    # extraction, manifest, sidecar subprocess and readback run unchanged.
    monkeypatch.setattr(app, "_validate", lambda: str(destination))
    monkeypatch.setattr(app, "_copy_uninstaller", lambda target: True)
    monkeypatch.setattr(app, "_register_programs_entry", lambda *a: True)
    monkeypatch.setattr(app, "_register_discovery", lambda *a: None)
    monkeypatch.setattr(app, "_run_ps1", lambda *a: (True, "test: external Windows integration isolated"))
    monkeypatch.setattr(app, "_refresh_shell", lambda: None)
    monkeypatch.setattr(app, "_archive_log", lambda *a: None)
    outcome = []

    def finish(ok, detail):
        outcome.append((ok, detail))
        print(f"Visible installer {'REPAIR' if repair else 'FRESH'}: {detail}", flush=True)
        root.update_idletasks()
        try:
            from tools.validate_mcp_install import capture
            capture("installer-repair" if repair else "installer-fresh")
        finally:
            root.destroy()

    monkeypatch.setattr(app, "_show_success", lambda target: root.after(
        500, finish, True, "\n".join(app._summary)))
    monkeypatch.setattr(app, "_show_error", lambda detail: finish(False, detail))
    root.after(180_000, finish, False, "Installer exceeded 180 seconds")
    root.after(800, app.install_btn.invoke)
    root.mainloop()
    assert outcome and outcome[0][0], outcome
    output = destination / install.MCP_CONFIG_NAME
    report = validate_file(output, expected_mode="installed")
    assert Path(report.command) == destination / install.MCP_EXE_NAME
    manifest = json.loads((destination / install.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["MCPConfig"] == output.name
    if repair:
        assert output.with_name(output.name + ".bak1").read_bytes() == b'{"old":"configuration from another installation"}'


def test_frozen_exporter_is_idempotent(destination):
    output = destination / install.MCP_CONFIG_NAME
    before = (output.read_bytes(), output.stat().st_mtime_ns)
    backups = list(destination.glob("*.bak*"))
    app = install.LumenInstaller.__new__(install.LumenInstaller)
    app._write_mcp_config(str(destination))
    assert before == (output.read_bytes(), output.stat().st_mtime_ns)
    assert list(destination.glob("*.bak*")) == backups


def test_generated_json_drives_real_frozen_mcp(destination, tmp_path, monkeypatch):
    # Exercise normal per-user discovery, NOT a hand-written --index argument.
    roaming = tmp_path / "Roaming profile"
    data = roaming / "Lumen Reader" / "Lumen"
    index_path = data / "library-index.db"
    books = tmp_path / "Books"
    make_epub(books / "Radio á.epub", "Radio Systems", "Ada Spectrum",
              "Frequency hopping protects a radio link against narrowband interference. " * 40,
              published="2024-02")
    with LibraryIndex(index_path) as index:
        index.scan(books, workers=1)
    fingerprint = hashlib.sha256(index_path.read_bytes()).hexdigest()
    entry = json.loads((destination / install.MCP_CONFIG_NAME).read_text(encoding="utf-8"))["mcpServers"]["lumen-book-reader"]
    environment = dict(os.environ, **entry["env"])
    environment["APPDATA"] = str(roaming)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in ("LUMEN_DATA_DIR", "LUMEN_INDEX_PATH", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    unrelated = tmp_path / "Unrelated client cwd"
    unrelated.mkdir()

    async def exercise():
        with anyio.fail_after(60):
            parameters = StdioServerParameters(command=entry["command"], args=entry["args"],
                                               env=environment, cwd=str(unrelated))
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    assert field(initialized, "server_info").name == "Lumen Book Reader"
                    discovered = await session.list_tools()
                    assert len(discovered.tools) == 7
                    print("LIVE frozen MCP: initialize + seven-tool discovery OK", flush=True)
                    for tool, arguments in (
                        ("lumen_status", {}),
                        ("lumen_glob", {"pattern": "*.epub"}),
                        ("lumen_search", {"query": "frequency hopping", "strategy": "lexical"}),
                        ("lumen_grep", {"query": "narrowband interference"}),
                    ):
                        result = await session.call_tool(tool, arguments)
                        assert not field(result, "is_error"), result
                        payload = structured_payload(result)
                        if tool == "lumen_status":
                            assert payload["health"] == "ready"
                            assert payload["corpus"]["books"] == 1
                        else:
                            assert payload["hits"], payload
                        print(f"LIVE frozen MCP: {tool} OK", flush=True)

    anyio.run(exercise)
    assert hashlib.sha256(index_path.read_bytes()).hexdigest() == fingerprint
