"""Durable reading sessions, independent of the disposable library catalog."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLockFile


HISTORY_FILENAME = "reading-history.json"


def path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


class ReadingHistoryStore:
    """Keep every completed session, including books outside the current library.

    Paths identify books without depending on catalog IDs, file timestamps, or
    the installation folder. Reading this file never adds books to the shelf.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._last_reads: dict[str, int] = {}

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "sessions": []}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OSError(f"Reading history is damaged; the original was kept: {self.path}") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sessions"), list):
            raise OSError(f"Unsupported reading history; the original was kept: {self.path}")
        return data

    def last_reads(self) -> dict[str, int]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._last_reads = None, {}
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return dict(self._last_reads)
        result: dict[str, int] = {}
        for session in self.read()["sessions"]:
            if not isinstance(session, dict):
                continue
            path, closed = session.get("path"), session.get("closed_ns")
            if not isinstance(path, str) or not path or type(closed) is not int or not 0 < closed < 2**63:
                continue
            try:
                if not Path(path).is_absolute():
                    continue
                key = path_key(path)
            except (OSError, ValueError):
                continue
            result[key] = max(result.get(key, 0), closed)
        self._stamp, self._last_reads = stamp, result
        return result

    def record(self, session: dict) -> None:
        """Merge under a process lock, then atomically replace the durable file.

        A unique session ID makes retries safe. Rereading under the lock keeps
        two reader windows from overwriting one another's completed sessions.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.path) + ".lock")
        if not lock.tryLock(1500):
            raise OSError("Reading history is busy in another Lumen window. Please try again.")
        pending = self.path.with_name(self.path.name + "." + uuid4().hex + ".pending")
        try:
            data = self.read()
            if any(isinstance(item, dict) and item.get("id") == session["id"] for item in data["sessions"]):
                return
            data["sessions"].append(dict(session))
            with pending.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(self.path)
            self._stamp = None
        finally:
            pending.unlink(missing_ok=True)
            lock.unlock()
