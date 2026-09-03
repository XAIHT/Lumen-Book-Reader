from __future__ import annotations

import json
import subprocess
from pathlib import Path

import lumen_reader.smart_definition as smart_definition

from lumen_reader.smart_definition import (
    build_ollama_chat_payload,
    infer_contextual_entries,
    normalized_ollama_url,
    parse_ollama_chat_response,
    parse_tlamatini_results,
    resolve_tlamatini_python,
    run_tlamatini_googler,
)


def test_copykittens_is_resolved_as_contextual_copycat_wordplay() -> None:
    context = (
        "China's early copycat internet companies were cute. "
        "During China's first internet boom, they copied Silicon Valley products."
    )
    entries = infer_contextual_entries("copykittens", context)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.part_of_speech == "coined noun · plural"
    assert "copycats" in entry.definition
    assert "early-stage imitators" in entry.definition
    assert entry.synonyms == ("copycats", "imitators")
    assert "inferred" in entry.source.casefold()
    assert "copycat internet companies" in entry.example


def test_ollama_payload_supports_local_and_cloud_models() -> None:
    cloud = build_ollama_chat_payload(
        "copykittens",
        "Early copycat internet companies.",
        "glm-5.2:cloud",
        book_title="AI Superpowers",
        chapter_title="Copykittens",
    )
    assert cloud["stream"] is False
    assert "format" not in cloud
    assert "Early copycat internet companies" in cloud["messages"][0]["content"]

    local = build_ollama_chat_payload("sprang", "He sprang up.", "qwen3:8b")
    assert local["format"] == "json"
    assert local["options"]["temperature"] == 0


def test_ollama_response_is_validated_and_labeled() -> None:
    content = {
        "definition": "A playful label for small or early-stage copycat companies.",
        "part_of_speech": "plural noun",
        "synonyms": ["copycats", "imitators", "copycats"],
        "base_form": "copycat",
        "confidence": 0.94,
    }
    payload = {"message": {"role": "assistant", "content": json.dumps(content)}}
    entries = parse_ollama_chat_response(
        json.dumps(payload), "copykittens", "glm-5.2:cloud"
    )
    assert len(entries) == 1
    assert entries[0].definition.startswith("Base expression: “copycat”.")
    assert entries[0].synonyms == ("copycats", "imitators")
    assert "glm-5.2:cloud" in entries[0].source


def test_tlamatini_results_become_transparent_web_evidence() -> None:
    results = [
        {
            "title": "Copykittens and the rise of China's internet",
            "url": "https://example.test/copykittens",
        },
        {"title": "Unrelated", "url": "https://example.test/other"},
    ]
    entries = parse_tlamatini_results("copykittens", results)
    assert len(entries) == 1
    assert "1 exact web reference" in entries[0].definition
    assert "Tlamatini Googler" in entries[0].source


def test_ollama_url_normalization_accepts_host_or_api_url() -> None:
    assert normalized_ollama_url("http://localhost:11434", "tags") == (
        "http://localhost:11434/api/tags"
    )
    assert normalized_ollama_url("http://localhost:11434/api/", "/chat") == (
        "http://localhost:11434/api/chat"
    )


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_frozen_reader_uses_tlamatini_python_never_lumen_exe(
    tmp_path: Path, monkeypatch
) -> None:
    agent_dir = tmp_path / "Tlamatini" / "agents" / "googler"
    _touch(agent_dir / "googler.py")
    bundled_python = _touch(tmp_path / "Tlamatini" / "python" / "python.exe")
    lumen_executable = _touch(tmp_path / "Lumen.exe")
    monkeypatch.setattr(smart_definition.sys, "frozen", True, raising=False)
    monkeypatch.setattr(smart_definition.sys, "executable", str(lumen_executable))

    assert resolve_tlamatini_python(agent_dir) == bundled_python.resolve()


def test_frozen_reader_fails_closed_without_a_tlamatini_python(
    tmp_path: Path, monkeypatch
) -> None:
    agent_dir = tmp_path / "Tlamatini" / "agents" / "googler"
    _touch(agent_dir / "googler.py")
    lumen_executable = _touch(tmp_path / "Lumen.exe")
    monkeypatch.setattr(smart_definition.sys, "frozen", True, raising=False)
    monkeypatch.setattr(smart_definition.sys, "executable", str(lumen_executable))

    assert resolve_tlamatini_python(agent_dir) is None

    def unexpected_launch(*args, **kwargs):
        raise AssertionError("No process may be launched without a trusted Python runtime")

    monkeypatch.setattr(smart_definition.subprocess, "run", unexpected_launch)
    assert run_tlamatini_googler("selected text", agent_dir) == []


def test_googler_launch_keeps_selected_text_out_of_code_and_shell(
    tmp_path: Path, monkeypatch
) -> None:
    agent_dir = tmp_path / "Tlamatini" / "agents" / "googler"
    script_path = _touch(agent_dir / "googler.py")
    bundled_python = _touch(tmp_path / "Tlamatini" / "python" / "python.exe")
    lumen_executable = _touch(tmp_path / "Lumen.exe")
    monkeypatch.setattr(smart_definition.sys, "frozen", True, raising=False)
    monkeypatch.setattr(smart_definition.sys, "executable", str(lumen_executable))
    captured: dict[str, object] = {}

    def fake_run(arguments, **options):
        captured["arguments"] = arguments
        captured["options"] = options
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                '__LUMEN_GOOGLER_JSON__[{"title":"danger & calc definition",'
                '"url":"https://example.test/danger"}]\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(smart_definition.subprocess, "run", fake_run)
    entries = run_tlamatini_googler("  danger   &   calc  ", agent_dir)

    arguments = captured["arguments"]
    options = captured["options"]
    assert arguments[0] == str(bundled_python.resolve())
    assert arguments[0] != str(lumen_executable.resolve())
    assert arguments[1] == "-c"
    assert arguments[3] == str(script_path.resolve())
    assert arguments[4] == '"danger & calc" meaning OR definition OR slang'
    assert options["shell"] is False
    assert options["stdin"] is subprocess.DEVNULL
    assert len(entries) == 1
