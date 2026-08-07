from __future__ import annotations

import json

from lumen_reader.smart_definition import (
    build_ollama_chat_payload,
    infer_contextual_entries,
    normalized_ollama_url,
    parse_ollama_chat_response,
    parse_tlamatini_results,
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
