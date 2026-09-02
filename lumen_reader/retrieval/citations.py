"""Signed compact citation tokens that never contain book text or paths."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from .contracts import RetrievalError


class CitationCodec:
    def __init__(self, key_file: str | Path | None = None, secret: bytes | None = None):
        self.key_file = Path(key_file) if key_file is not None else None
        self.secret = secret or self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        if self.key_file is None:
            return os.urandom(32)
        try:
            existing = self.key_file.read_bytes()
            if len(existing) >= 32:
                return existing[:64]
        except OSError:
            pass
        try:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                key = os.urandom(32)
                os.write(descriptor, key)
            finally:
                os.close(descriptor)
            return key
        except FileExistsError:
            try:
                return self.key_file.read_bytes()[:64]
            except OSError:
                return os.urandom(32)
        except OSError:
            return os.urandom(32)

    def encode(self, *, book_id: int, revision: int, passage_id: int, content_hash: str) -> str:
        payload = {
            "v": 1,
            "b": int(book_id),
            "r": int(revision),
            "p": int(passage_id),
            "h": str(content_hash)[:16],
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self.secret, raw, hashlib.sha256).digest()[:16]
        return "lumencite:v1:" + _b64(raw + signature)

    def decode(self, token: str) -> dict[str, Any]:
        if not token.startswith("lumencite:v1:"):
            raise RetrievalError("INVALID_CITATION", "Citation token has an unknown format.")
        try:
            packed = _unb64(token.split(":", 2)[2])
            raw, signature = packed[:-16], packed[-16:]
            expected = hmac.new(self.secret, raw, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("v") != 1:
                raise ValueError("version")
            return payload
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exception:
            raise RetrievalError("INVALID_CITATION", "Citation token is malformed or invalid.") from exception


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
