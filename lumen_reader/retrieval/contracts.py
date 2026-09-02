"""Stable retrieval errors and small immutable query records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "1.0"


class RetrievalError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        suggested_action: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.suggested_action = suggested_action
        self.details = details or {}

    def as_dict(self, request_id: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "suggested_action": self.suggested_action,
                "details": self.details,
            },
        }


@dataclass(frozen=True, slots=True)
class RootScope:
    root_id: str
    path: str
    book_count: int


@dataclass(slots=True)
class BackendReport:
    requested: str
    used: list[str] = field(default_factory=list)
    fallback_from: list[str] = field(default_factory=list)
    model_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "used": self.used,
            "fallback_from": self.fallback_from,
            "model_id": self.model_id,
        }
