"""Strict, deterministic LumenBookReader.json generation and validation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVER_ID = "lumen-book-reader"
SAFE_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
}
MAX_CONFIG_BYTES = 65_536


class ConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LaunchModel:
    mode: str
    command: Path

    @property
    def args(self) -> list[str]:
        if self.mode == "installed":
            return ["serve", "--stdio"]
        return ["-m", "lumen_reader.mcp_server", "serve", "--stdio"]

    def document(self) -> dict[str, Any]:
        return {
            "mcpServers": {
                SERVER_ID: {
                    "command": str(self.command),
                    "args": self.args,
                    "env": dict(SAFE_ENV),
                }
            }
        }


@dataclass(frozen=True, slots=True)
class ConfigReport:
    status: str
    path: str
    mode: str
    command: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "mode": self.mode,
            "command": self.command,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def resolve_model(
    mode: str,
    *,
    executable: str | Path | None = None,
    checkout: str | Path | None = None,
) -> LaunchModel:
    mode = mode.casefold()
    if mode not in {"installed", "development"}:
        raise ConfigError("CONFIG_MODE_INVALID", "Mode must be installed or development.")
    if executable is not None:
        command = Path(executable).expanduser()
    elif mode == "installed":
        adjacent = Path(os.path.abspath(os.sys.executable)).with_name("LumenMCP.exe")
        command = adjacent
    else:
        if checkout is None:
            raise ConfigError(
                "CONFIG_EXECUTABLE_NOT_FOUND",
                "Development mode requires --checkout or --executable.",
            )
        command = Path(checkout).expanduser() / ".venv" / "Scripts" / "python.exe"
    command = command.resolve(strict=False)
    _validate_command(command, mode)
    return LaunchModel(mode=mode, command=command)


def canonical_bytes(model: LaunchModel) -> bytes:
    encoded = (
        json.dumps(model.document(), ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    validate_bytes(encoded, expected_mode=model.mode, expected_command=model.command)
    return encoded


def emit(
    output: str | Path,
    model: LaunchModel,
    *,
    force: bool = False,
    backup: bool = False,
) -> ConfigReport:
    destination = Path(output).expanduser().resolve(strict=False)
    if destination.is_symlink():
        raise ConfigError("CONFIG_OUTPUT_UNSAFE", "A symlink/reparse output is not accepted.")
    data = canonical_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    if destination.exists():
        try:
            current = destination.read_bytes()
        except OSError as exception:
            raise ConfigError("CONFIG_OUTPUT_DENIED", str(exception)) from exception
        if current == data:
            return ConfigReport("unchanged", str(destination), model.mode, str(model.command), digest, len(data))
        if not (force and backup):
            raise ConfigError(
                "CONFIG_OUTPUT_EXISTS",
                "Destination exists; replacement requires both --force and --backup.",
            )
        backup_path = _backup_name(destination)
        try:
            backup_path.write_bytes(current)
        except OSError as exception:
            raise ConfigError("CONFIG_BACKUP_FAILED", str(exception)) from exception
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        temporary = Path(temp_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exception:
        raise ConfigError("CONFIG_OUTPUT_DENIED", str(exception)) from exception
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return ConfigReport("created", str(destination), model.mode, str(model.command), digest, len(data))


def validate_file(
    source: str | Path,
    *,
    expected_mode: str | None = None,
    check_command: bool = True,
) -> ConfigReport:
    path = Path(source).expanduser().resolve(strict=False)
    try:
        data = path.read_bytes()
    except OSError as exception:
        raise ConfigError("CONFIG_INPUT_UNREADABLE", str(exception)) from exception
    document, mode, command = validate_bytes(data, expected_mode=expected_mode)
    del document
    if check_command:
        _validate_command(command, mode)
    return ConfigReport(
        "valid",
        str(path),
        mode,
        str(command),
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


def validate_bytes(
    data: bytes,
    *,
    expected_mode: str | None = None,
    expected_command: Path | None = None,
) -> tuple[dict[str, Any], str, Path]:
    if not data or len(data) > MAX_CONFIG_BYTES or data.startswith(b"\xef\xbb\xbf") or b"\0" in data:
        raise ConfigError("CONFIG_ENCODING_INVALID", "Config bytes violate size/UTF-8/BOM/NUL policy.")
    try:
        text = data.decode("utf-8")
        document = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ConfigError) as exception:
        if isinstance(exception, ConfigError):
            raise
        raise ConfigError("CONFIG_JSON_INVALID", str(exception)) from exception
    if not isinstance(document, dict) or set(document) != {"mcpServers"}:
        raise ConfigError("CONFIG_SCHEMA_INVALID", "Top level must contain only mcpServers.")
    servers = document["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != {SERVER_ID}:
        raise ConfigError("CONFIG_SCHEMA_INVALID", f"mcpServers must contain only {SERVER_ID}.")
    entry = servers[SERVER_ID]
    if not isinstance(entry, dict) or set(entry) != {"command", "args", "env"}:
        raise ConfigError("CONFIG_SCHEMA_INVALID", "Server entry must contain command, args, and env only.")
    command_text = entry["command"]
    args = entry["args"]
    env = entry["env"]
    if not isinstance(command_text, str) or not command_text or not Path(command_text).is_absolute():
        raise ConfigError("CONFIG_SCHEMA_INVALID", "command must be a non-empty absolute path.")
    if env != SAFE_ENV:
        raise ConfigError("CONFIG_SCHEMA_INVALID", "env must contain only the two safe UTF-8 variables.")
    if args == ["serve", "--stdio"]:
        mode = "installed"
    elif args == ["-m", "lumen_reader.mcp_server", "serve", "--stdio"]:
        mode = "development"
    else:
        raise ConfigError("CONFIG_SCHEMA_INVALID", "args do not match an allowed STDIO launch sequence.")
    if expected_mode is not None and mode != expected_mode:
        raise ConfigError("CONFIG_MODE_MISMATCH", "Serialized arguments do not match the expected mode.")
    command = Path(command_text).resolve(strict=False)
    if expected_command is not None and os.path.normcase(str(command)) != os.path.normcase(str(expected_command)):
        raise ConfigError("CONFIG_COMMAND_IDENTITY_FAILED", "Serialized command changed during rendering.")
    lowered = text.casefold()
    if any(marker in lowered for marker in ("<user>", "todo", "authorization", "bearer", "api_key")):
        raise ConfigError("CONFIG_SENSITIVE_VALUE", "Config contains a placeholder or forbidden secret-like field.")
    return document, mode, command


def _validate_command(command: Path, mode: str) -> None:
    if not command.is_absolute() or not command.is_file():
        raise ConfigError("CONFIG_EXECUTABLE_NOT_FOUND", "The selected command is not an existing regular file.")
    name = command.name.casefold()
    if mode == "installed" and name != "lumenmcp.exe":
        raise ConfigError("CONFIG_MODE_MISMATCH", "Installed mode requires LumenMCP.exe.")
    if mode == "development" and name not in {"python.exe", "python", "python3", "python3.exe"}:
        raise ConfigError("CONFIG_MODE_MISMATCH", "Development mode requires a Python executable.")
    text = str(command)
    if text.startswith("\\\\") or text.startswith("\\?\\"):
        raise ConfigError("CONFIG_COMMAND_UNTRUSTED", "UNC/device executables are rejected by default.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("CONFIG_JSON_INVALID", f"Duplicate key: {key}")
        result[key] = value
    return result


def _backup_name(path: Path) -> Path:
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak{index}")
        if not candidate.exists():
            return candidate
        index += 1
