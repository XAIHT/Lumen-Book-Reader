"""Bounded, query-only SQLite connections for concurrent MCP calls."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from .contracts import RetrievalError


class QueryPool:
    def __init__(self, database: str | Path, max_connections: int = 8):
        self.path = Path(database)
        self.max_connections = max(1, min(32, int(max_connections)))
        self._slots = threading.BoundedSemaphore(self.max_connections)

    @contextmanager
    def connection(self, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise RetrievalError(
                "INDEX_NOT_FOUND",
                "Lumen's library index does not exist yet.",
                suggested_action="Open Lumen, choose the library folder, and run a sweep.",
            )
        if not self._slots.acquire(timeout=max(0.05, timeout)):
            raise RetrievalError("INDEX_BUSY", "All bounded index readers are busy.")
        connection: sqlite3.Connection | None = None
        try:
            uri_path = quote(self.path.resolve().as_posix(), safe="/:")
            connection = sqlite3.connect(
                f"file:{uri_path}?mode=ro", uri=True, timeout=timeout, check_same_thread=False
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
            yield connection
        except sqlite3.DatabaseError as exception:
            raise RetrievalError(
                "INDEX_BUSY" if "locked" in str(exception).casefold() else "INDEX_CORRUPT",
                f"The Lumen index could not answer safely: {type(exception).__name__}.",
                retryable="locked" in str(exception).casefold(),
            ) from exception
        finally:
            if connection is not None:
                connection.close()
            self._slots.release()
