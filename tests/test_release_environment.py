"""Regression coverage for the shared MCP downgrade that broke releases."""

from pathlib import Path
from types import SimpleNamespace
import runpy

import pytest

import build
import build_complete_release as release
import release_environment as environment


def test_release_environment_removes_foreign_python_and_pip_paths(monkeypatch):
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV",
                "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_REQUIREMENT", "PIP_CONSTRAINT"):
        monkeypatch.setenv(key, "foreign-install")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    env = environment.release_env()
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert "foreign-install" not in env.values()
    assert env["PIP_CONFIG_FILE"] == environment.os.devnull


def test_explicit_python_is_never_provisioned(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "_run", lambda *a: pytest.fail("unexpected installation"))
    assert environment.prepare_release_python(tmp_path, "custom/python.exe") == "custom/python.exe"
    assert not (tmp_path / ".venv-release").exists()


def _fake_venv(tmp_path: Path, system_site_packages: bool = False) -> Path:
    directory = tmp_path / ".venv-release"
    python = directory / ("Scripts/python.exe" if environment.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    (directory / "pyvenv.cfg").write_text(
        f"include-system-site-packages = {str(system_site_packages).lower()}\n", encoding="utf-8"
    )
    return python


def test_reused_environment_reconciles_pins_and_checks_dependencies(tmp_path, monkeypatch):
    python = _fake_venv(tmp_path)
    calls = []
    monkeypatch.setattr(environment, "_run", lambda cmd, root, env: calls.append(cmd))
    assert environment.prepare_release_python(tmp_path) == str(python)
    assert all(cmd[:2] == [str(python), "-I"] for cmd in calls)
    assert calls[1][2:] == ["-m", "pip", "--require-virtualenv", "install", "-r",
                           str(tmp_path / "requirements-release.txt")]
    assert calls[2][2:] == ["-m", "pip", "check"]


def test_contaminated_existing_venv_fails_before_pip(tmp_path, monkeypatch):
    _fake_venv(tmp_path, system_site_packages=True)
    monkeypatch.setattr(environment, "_run", lambda *a: pytest.fail("pip must not run"))
    with pytest.raises(SystemExit, match="system site packages disabled"):
        environment.prepare_release_python(tmp_path)


def test_failed_environment_preparation_never_starts_freeze(monkeypatch):
    def fail(*args):
        raise SystemExit("preparation failed")
    monkeypatch.setattr(release, "prepare_release_python", fail)
    monkeypatch.setattr(release, "run_stage", lambda *a: pytest.fail("freeze started"))
    with pytest.raises(SystemExit, match="preparation failed"):
        release.main(["--version", "1.7.2"])


def test_all_release_stages_use_managed_python(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "prepare_release_python", lambda *a: "managed/python.exe")
    monkeypatch.setattr(release, "run_stage", lambda *args: calls.append(args))
    monkeypatch.setattr(release, "newest_release_dir", lambda *a: tmp_path)
    monkeypatch.setattr(release, "write_checksums", lambda *a: {})
    monkeypatch.setattr(release, "write_manifest", lambda *a: None)
    assert release.main(["--version", "1.7.2", "--no-archive"]) == 0
    assert len(calls) == 3
    assert all(call[-1] == "managed/python.exe" for call in calls)


def test_app_cleanup_preserves_previous_releases_and_build_scripts(tmp_path, monkeypatch):
    scratch, dist = tmp_path / "build", tmp_path / "dist"
    monkeypatch.setattr(build, "BUILD_DIR", scratch)
    monkeypatch.setattr(build, "DIST_DIR", dist)
    monkeypatch.setattr(build, "PAYLOAD_DIR", dist / "payload")
    owned = [scratch / "Lumen", scratch / "LumenMCP", dist / "Lumen", dist / "payload"]
    for folder in owned:
        folder.mkdir(parents=True)
        (folder / "intermediate.bin").write_bytes(b"scratch")
    wrapper = scratch / "_complete" / "_release.py"
    wrapper.parent.mkdir()
    wrapper.write_text("# user's wrapper", encoding="utf-8")
    archive = dist / "Lumen_Release_v1.7.2.zip"
    archive.write_bytes(b"previous archive")
    previous = dist / "Lumen_Release_v1.7.2"
    previous.mkdir()
    (previous / "pkg.zip").write_bytes(b"previous payload")
    (dist / "LumenMCP.exe").write_bytes(b"scratch")
    build.clean_app_intermediates()
    assert not any(path.exists() for path in owned)
    assert archive.read_bytes() == b"previous archive"
    assert (previous / "pkg.zip").read_bytes() == b"previous payload"
    assert wrapper.is_file()
    assert not (dist / "LumenMCP.exe").exists()


def test_missing_release_cannot_silently_use_another_version(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "DIST", tmp_path)
    (tmp_path / "Lumen_Release_v1.7.0").mkdir()
    assert release.newest_release_dir("1.7.2") is None


def test_preflight_reports_actual_mismatched_sdk(monkeypatch):
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    versions = {"mcp": "1.28.1", "mcp-types": "2.2.0"}
    monkeypatch.setattr(build.importlib.metadata, "version", versions.__getitem__)
    with pytest.raises(SystemExit, match="mcp=1.28.1, mcp-types=2.2.0"):
        build.assert_dependencies()


def test_mcp_freezer_hook_collects_transports_without_optional_cli():
    pytest.importorskip("PyInstaller")
    values = runpy.run_path(str(build.ROOT / "packaging_hooks" / "hook-mcp.py"))
    modules = values["hiddenimports"]
    assert "mcp.server.stdio" in modules
    assert "mcp.server.streamable_http" in modules
    assert "mcp.client.stdio" in modules
    assert not any(name == "mcp.cli" or name.startswith("mcp.cli.") for name in modules)
