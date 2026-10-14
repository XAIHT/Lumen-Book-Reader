"""Installer contract tests; run through the visible validation runner."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import install
import uninstall
from lumen_reader.mcp_server.config_export import ConfigError, emit, resolve_model


def wizard():
    return install.LumenInstaller.__new__(install.LumenInstaller)


def test_installer_uses_exact_installed_exporter_without_a_shell(tmp_path, monkeypatch):
    target = tmp_path / "Lectores á & (TODO)"
    target.mkdir()
    executable = target / install.MCP_EXE_NAME
    executable.touch()
    output = target / install.MCP_CONFIG_NAME
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        emit(output, resolve_model("installed", executable=executable), force=True, backup=True)
        return subprocess.CompletedProcess(command, 0, '{"status":"created"}', "")

    monkeypatch.setattr(install.subprocess, "run", run)
    assert wizard()._write_mcp_config(str(target)) == str(output)
    command, options = calls[0]
    assert command == [str(executable), "config", "emit", "--mode", "installed",
                       "--executable", str(executable), "--output", str(output),
                       "--force", "--backup", "--report", "json"]
    assert not options.get("shell")
    assert options["cwd"] == str(target)
    assert options["timeout"] == 60
    assert options["stdin"] == subprocess.DEVNULL
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert options["encoding"] == "utf-8"
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_missing_sidecar_never_falls_back_to_gui(tmp_path, monkeypatch):
    (tmp_path / "Lumen.exe").touch()
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: pytest.fail("GUI launched"))
    with pytest.raises(OSError, match="LumenMCP.exe is missing"):
        wizard()._write_mcp_config(str(tmp_path))


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "wrong_path", "invalid", "extra"])
def test_export_failure_cannot_be_reported_as_success(tmp_path, monkeypatch, failure):
    executable = tmp_path / install.MCP_EXE_NAME
    executable.touch()
    output = tmp_path / install.MCP_CONFIG_NAME

    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if failure == "exit":
            return subprocess.CompletedProcess(command, 4, "", "CONFIG_OUTPUT_DENIED")
        if failure in {"wrong_path", "extra"}:
            model = resolve_model("installed", executable=executable)
            document = model.document()
            if failure == "wrong_path":
                document["mcpServers"]["lumen-book-reader"]["command"] = str(tmp_path / "Lumen.exe")
            else:
                document["mcpServers"]["lumen-book-reader"]["env"]["SECRET"] = "unexpected"
            output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        elif failure == "invalid":
            output.write_text("not JSON", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "success claimed", "")

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(OSError):
        wizard()._write_mcp_config(str(tmp_path))


def test_manifest_names_the_generated_handoff(tmp_path):
    app = wizard()
    value = lambda x: SimpleNamespace(get=lambda: x)
    app.library_path = value(str(tmp_path / "books"))
    app.set_default_var = app.desktop_var = app.startmenu_var = value(False)
    app.version = "1.8.0"
    app._write_manifest(str(tmp_path), [])
    manifest = json.loads((tmp_path / install.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["MCPConfig"] == "LumenBookReader.json"
    assert manifest["MCPExecutable"] == "LumenMCP.exe"


def test_uninstall_removes_handoff_and_backups_but_keeps_books(tmp_path):
    target = tmp_path / "isolated-install"
    target.mkdir()
    assert target.resolve().is_relative_to(tmp_path.resolve())
    for name in (install.MCP_CONFIG_NAME, install.MCP_CONFIG_NAME + ".bak1"):
        (target / name).write_text("fixture", encoding="utf-8")
    library = target / "library"
    library.mkdir()
    (library / "keep.epub").write_bytes(b"user book")
    app = uninstall.LumenUninstaller.__new__(uninstall.LumenUninstaller)
    app._set_progress = lambda *args: None
    assert app._remove_files(str(target), 0, 1) == (2, 0)
    assert (library / "keep.epub").read_bytes() == b"user book"


def test_atomic_failure_keeps_original_and_backup(tmp_path, monkeypatch):
    executable = tmp_path / "LumenMCP.exe"
    executable.touch()
    output = tmp_path / install.MCP_CONFIG_NAME
    output.write_bytes(b"user customization")
    monkeypatch.setattr("lumen_reader.mcp_server.config_export.os.replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(ConfigError, match="locked"):
        emit(output, resolve_model("installed", executable=executable), force=True, backup=True)
    assert output.read_bytes() == b"user customization"
    assert output.with_name(output.name + ".bak1").read_bytes() == b"user customization"
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_failure_does_not_touch_original(tmp_path, monkeypatch):
    executable = tmp_path / "LumenMCP.exe"
    executable.touch()
    output = tmp_path / install.MCP_CONFIG_NAME
    output.write_bytes(b"original")
    occupied = tmp_path / "occupied.bak"
    occupied.write_bytes(b"another backup")
    monkeypatch.setattr("lumen_reader.mcp_server.config_export._backup_name", lambda _: occupied)
    with pytest.raises(ConfigError) as error:
        emit(output, resolve_model("installed", executable=executable), force=True, backup=True)
    assert error.value.code == "CONFIG_BACKUP_FAILED"
    assert output.read_bytes() == b"original"
    assert occupied.read_bytes() == b"another backup"


def test_symlink_is_rejected_before_resolving(tmp_path, monkeypatch):
    output = tmp_path / install.MCP_CONFIG_NAME
    executable = tmp_path / "LumenMCP.exe"
    executable.touch()
    # Model a Windows symlink without requiring the machine's symlink privilege.
    real_check = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == output or real_check(p))
    model = resolve_model("installed", executable=executable)
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("followed unsafe leaf"))
    with pytest.raises(ConfigError) as error:
        emit(output, model)
    assert error.value.code == "CONFIG_OUTPUT_UNSAFE"
    with pytest.raises(OSError, match="not a regular file"):
        wizard()._write_mcp_config(str(tmp_path))


def test_pipeline_fails_before_manifest_or_registration(tmp_path, monkeypatch):
    import zipfile

    package = tmp_path / "pkg.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Lumen.exe", "fixture")
    app = wizard()
    app.zip_path = str(package)
    app._preserved_names = lambda: set()
    app._set_progress = app._activate_step = app._mark_step = lambda *a, **k: None
    errors = []
    app._show_error = errors.append
    app.root = SimpleNamespace(after=lambda _ms, callback, *args: callback(*args))
    app._write_manifest = lambda *a: pytest.fail("manifest written despite failed config")
    app._register_discovery = lambda *a: pytest.fail("registration despite failed config")
    app._run_install(str(tmp_path / "destination"))
    assert len(errors) == 1 and "LumenMCP.exe is missing" in errors[0]


@pytest.mark.parametrize("reveal", [False, True])
def test_completion_reveals_only_after_confirmation(tmp_path, monkeypatch, reveal):
    app = wizard()
    app.step_label = SimpleNamespace(config=lambda **kwargs: None)
    closed = []
    app.root = SimpleNamespace(destroy=lambda: closed.append(True))
    app._summary = [str(tmp_path / install.MCP_CONFIG_NAME)]
    prompts, launches = [], []

    def confirm(title, body):
        prompts.append(body)
        return reveal

    monkeypatch.setattr(install.messagebox, "askyesno", confirm)
    monkeypatch.setattr(install.subprocess, "Popen", lambda *a, **k: launches.append((a, k)))
    app._show_success(str(tmp_path))
    assert closed == [True]
    assert "Tlamatini" in prompts[0] and "External" in prompts[0]
    assert bool(launches) == reveal
    if reveal:
        assert launches[0][0][0] == ["explorer.exe", "/select,", str(tmp_path / install.MCP_CONFIG_NAME)]


def test_package_verifier_rejects_a_prebuilt_descriptor(tmp_path):
    import zipfile
    from build import verify_pkg_zip

    package = tmp_path / "bad-package.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("LumenBookReader.json", "development paths must not ship")
    with pytest.raises(SystemExit, match="build-machine MCP descriptor"):
        verify_pkg_zip(package)


def test_cli_json_report_survives_an_ascii_pipe(monkeypatch):
    import io
    from lumen_reader.mcp_server.cli import _print_report

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii")
    monkeypatch.setattr("sys.stdout", stream)
    report = {"path": "C:/書籍/instalación/LumenBookReader.json"}
    assert _print_report(report, True) == 0
    stream.flush()
    assert json.loads(raw.getvalue().decode("ascii")) == report
    stream.detach()
