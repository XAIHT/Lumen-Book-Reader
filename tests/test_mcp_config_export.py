from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lumen_reader.mcp_server.config_export import (
    ConfigError,
    canonical_bytes,
    emit,
    resolve_model,
    validate_bytes,
    validate_file,
)


def test_development_descriptor_is_canonical_strict_and_private(tmp_path: Path) -> None:
    model = resolve_model("development", executable=sys.executable)
    data = canonical_bytes(model)
    document = json.loads(data)
    entry = document["mcpServers"]["lumen-book-reader"]
    assert entry["command"] == str(Path(sys.executable).resolve())
    assert entry["args"] == ["-m", "lumen_reader.mcp_server", "serve", "--stdio"]
    assert entry["env"] == {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    assert data.endswith(b"\n") and not data.startswith(b"\xef\xbb\xbf")
    assert b"library-index" not in data and b"books" not in data


def test_emit_is_atomic_idempotent_and_refuses_unapproved_overwrite(tmp_path: Path) -> None:
    model = resolve_model("development", executable=sys.executable)
    output = tmp_path / "LumenBookReader.json"
    assert emit(output, model).status == "created"
    assert emit(output, model).status == "unchanged"
    output.write_text("foreign", encoding="utf-8")
    with pytest.raises(ConfigError, match="requires both"):
        emit(output, model)
    assert output.read_text(encoding="utf-8") == "foreign"
    assert emit(output, model, force=True, backup=True).status == "created"
    assert list(tmp_path.glob("LumenBookReader.json.bak*"))
    assert validate_file(output, expected_mode="development").status == "valid"


def test_validator_rejects_duplicate_or_extra_fields() -> None:
    duplicate = b'{"mcpServers":{},"mcpServers":{}}'
    with pytest.raises(ConfigError) as caught:
        validate_bytes(duplicate)
    assert caught.value.code == "CONFIG_JSON_INVALID"

    extra = b'{"mcpServers":{"lumen-book-reader":{"command":"C:/python.exe","args":["-m","lumen_reader.mcp_server","serve","--stdio"],"env":{"PYTHONUNBUFFERED":"1","PYTHONIOENCODING":"utf-8"},"token":"x"}}}'
    with pytest.raises(ConfigError) as caught:
        validate_bytes(extra)
    assert caught.value.code == "CONFIG_SCHEMA_INVALID"
