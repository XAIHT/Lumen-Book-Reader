"""Safe indexed-library glob compilation and verification."""

from __future__ import annotations

import re

from .contracts import RetrievalError


def compile_glob(pattern: str, *, case_sensitive: bool) -> re.Pattern[str]:
    value = pattern.replace("\\", "/").strip()
    if not value or len(value) > 2048:
        raise RetrievalError("INVALID_ARGUMENT", "Glob pattern must be 1–2,048 characters.")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value) or value.startswith("//"):
        raise RetrievalError("INVALID_ARGUMENT", "Glob patterns must be relative to a configured root.")
    if any(part == ".." for part in value.split("/")):
        raise RetrievalError("INVALID_ARGUMENT", "Glob patterns cannot traverse above a configured root.")

    output = ["^"]
    index = 0
    while index < len(value):
        character = value[index]
        if character == "*":
            if index + 1 < len(value) and value[index + 1] == "*":
                index += 2
                if index < len(value) and value[index] == "/":
                    output.append("(?:.*/)?")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif character == "?":
            output.append("[^/]")
        elif character == "[":
            end = value.find("]", index + 1)
            if end < 0:
                output.append(r"\[")
            else:
                content = value[index + 1:end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                output.append("[" + content.replace("\\", r"\\") + "]")
                index = end
        else:
            output.append(re.escape(character))
        index += 1
    output.append("$")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile("".join(output), flags)
    except re.error as exception:
        raise RetrievalError("INVALID_ARGUMENT", f"Invalid glob character class: {exception}.") from exception


def fixed_prefix(pattern: str) -> str:
    normalized = pattern.replace("\\", "/").strip()
    match = re.search(r"[*?[]", normalized)
    return normalized[:match.start()] if match else normalized
