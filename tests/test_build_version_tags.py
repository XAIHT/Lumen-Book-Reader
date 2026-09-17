"""Build-only tag refresh, offline fallback and baked runtime identity."""

import subprocess
from types import SimpleNamespace

import pytest

import versioning
from lumen_reader import version as runtime_version


@pytest.mark.parametrize("result", ["ok", "offline", "timeout"])
def test_tag_refresh_is_bounded_and_never_force_updates(tmp_path, monkeypatch, result):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(versioning, "REPO_ROOT", tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0 if result == "ok" else 1)

    monkeypatch.setattr(versioning.subprocess, "run", run)
    assert versioning.refresh_build_tags() is (result == "ok")
    command, options = calls[0]
    assert command[:2] == ["git", "fetch"]
    assert command[-1] == "refs/tags/*:refs/tags/*"
    assert "--force" not in command
    assert options["timeout"] == 20
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_version_selection_refreshes_before_reading_git_and_survives_offline(monkeypatch):
    monkeypatch.delenv("LUMEN_VERSION", raising=False)
    events = []
    monkeypatch.setattr(versioning, "refresh_build_tags", lambda: events.append("refresh") or False)
    monkeypatch.setattr(versioning, "derive_version_from_git", lambda: events.append("git") or "4.5.6")
    assert versioning.resolve_build_version() == "4.5.6"
    assert events == ["refresh", "git"]


def test_explicit_and_inherited_build_versions_do_not_contact_origin(monkeypatch):
    monkeypatch.setattr(versioning, "refresh_build_tags", lambda: pytest.fail("unexpected network"))
    monkeypatch.setenv("LUMEN_VERSION", "7.8.9")
    assert versioning.resolve_build_version("v3.2.1") == "3.2.1"
    assert versioning.resolve_build_version() == "7.8.9"


def test_generated_runtime_version_wins_without_git_or_network(monkeypatch):
    monkeypatch.setattr(runtime_version, "_CACHED_VERSION", None)
    monkeypatch.setattr(runtime_version, "_read_generated_module", lambda: "3.4.5")
    monkeypatch.setattr(runtime_version, "derive_version_from_git", lambda: pytest.fail("runtime git"))
    monkeypatch.setenv("LUMEN_VERSION", "9.9.9")
    assert runtime_version.get_version() == "3.4.5"
