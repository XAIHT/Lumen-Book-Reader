"""Opaque signed search cursors bound to operation, query, and corpus revision."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from .citations import _b64, _unb64
from .contracts import RetrievalError


class CursorCodec:
    def __init__(self, secret: bytes, ttl_seconds: int = 3600):
        self.secret = secret
        self.ttl_seconds = max(60, int(ttl_seconds))

    def encode(
        self,
        *,
        operation: str,
        query_digest: str,
        corpus_revision: int,
        root_digest: str,
        offset: int,
    ) -> str:
        now = int(time.time())
        payload = {
            "v": 1,
            "o": operation,
            "q": query_digest,
            "c": int(corpus_revision),
            "s": root_digest,
            "n": max(0, int(offset)),
            "i": now,
            "e": now + self.ttl_seconds,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self.secret, raw, hashlib.sha256).digest()[:16]
        return _b64(raw + signature)

    def decode(
        self,
        value: str,
        *,
        operation: str,
        query_digest: str,
        corpus_revision: int,
        root_digest: str,
    ) -> dict[str, Any]:
        if not value or len(value) > 4096:
            raise RetrievalError("INVALID_CURSOR", "Cursor is missing or too large.")
        try:
            packed = _unb64(value)
            raw, signature = packed[:-16], packed[-16:]
            expected = hmac.new(self.secret, raw, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exception:
            raise RetrievalError("INVALID_CURSOR", "Cursor is malformed or has an invalid signature.") from exception
        if int(payload.get("e", 0)) < int(time.time()):
            raise RetrievalError("CURSOR_EXPIRED", "Cursor expired; repeat the query.")
        if payload.get("o") != operation or payload.get("q") != query_digest:
            raise RetrievalError("INVALID_CURSOR", "Cursor belongs to a different query.")
        if int(payload.get("c", -1)) != int(corpus_revision):
            raise RetrievalError("CURSOR_STALE", "The library changed; repeat the query.")
        if payload.get("s") != root_digest:
            raise RetrievalError("CURSOR_SCOPE_MISMATCH", "Cursor belongs to a different root scope.")
        return payload
