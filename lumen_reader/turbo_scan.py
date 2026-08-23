# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The Turbo Sweep: a fully concurrent, self-reporting library scanner.

The previous indexer was correct but strictly sequential in its *phases*: it
walked the entire tree into a dictionary, then diffed that dictionary, then
handed the whole batch to a process pool.  On a local shelf that is invisible.
On a NAS holding millions of books it is fatal twice over - nothing is extracted
until the last directory has been listed, and the complete file list must fit in
memory before any work starts.

This module replaces the phases with a *pipeline*.  Every stage runs at the same
time, connected by bounded queues, so the first book is being parsed while the
far end of the tree is still being listed and memory stays flat no matter how
large the corpus is:

    ┌────────────────────┐  paths   ┌──────────┐  new/changed  ┌──────────────┐
    │  WALKER FLEET      │ ───────▶ │  TRIAGE  │ ────────────▶ │  EXTRACTOR   │
    │  N threads,        │          │  1 thread│               │  FLEET       │
    │  work-stealing     │          │  batched │               │  1 OS process│
    │  over a shared     │          │  SQLite  │               │  per core, at │
    │  directory deque   │          │  lookup  │               │  HIGH priority│
    └────────────────────┘          └────┬─────┘               └──────┬───────┘
                                unchanged│                     records│
                                         ▼                            ▼
                                    ┌───────────────────────────────────────┐
                                    │  WRITER  ·  1 thread, batched trans-  │
                                    │  actions into SQLite + FTS5           │
                                    └───────────────────────────────────────┘

Three properties make this safe to point at a network datalake:

* **Bounded memory.**  Every queue has a ceiling.  When the extractors fall
  behind, triage blocks; when triage blocks, the walkers block.  Back-pressure
  travels up the pipeline instead of the file list travelling down into RAM.

* **No deadlock.**  The writer is the only stage that never blocks on a full
  queue, so results always drain, which always unblocks the extractors, which
  always unblocks triage.  The cycle is broken by construction.

* **Generation marking instead of set difference.**  Finding deleted books used
  to need the whole indexed path set *and* the whole found path set in memory at
  once.  Each scan now stamps the rows it sees with a generation number and
  sweeps ``seen_gen <> current`` at the end, which costs one indexed DELETE.

Everything the fleet is doing is published through shared memory - one small
ctypes block per worker - so the monitor can paint a live per-core view at 10 Hz
without adding a single message to the hot path.

Nothing here may import Qt: these functions are re-imported inside every worker
process that Windows spawns.
"""

from __future__ import annotations

import ctypes
import fnmatch
import multiprocessing as mp
import os
import queue
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

if sys.platform == "win32":          # ctypes.wintypes does not exist elsewhere
    from ctypes import wintypes

from .library_index import (
    BOOK_SUFFIXES,
    DEFAULT_TEXT_BUDGET,
    SKIP_DIRECTORIES,
    LibraryCounts,
    LibraryIndex,
    build_fts_map,
    drop_fts_rows,
    extract_book,
    fts_map_ready,
    normalize_root,
)

# ───────────────────────────── process priority ────────────────────────────
#
# Angela's requirement is explicit: one *ultra priority* process per core.  On
# Windows that is a priority class on the process plus a thread priority on the
# thread that actually parses, applied by the worker to itself the moment it
# starts - a parent cannot reliably raise a child that has not spawned yet.

#: Windows ``SetPriorityClass`` constants, lowest to highest.
WINDOWS_PRIORITY_CLASSES: dict[str, int] = {
    "idle": 0x00000040,
    "below": 0x00004000,
    "normal": 0x00000020,
    "above": 0x00008000,
    "high": 0x00000080,
    "realtime": 0x00000100,
}

#: POSIX ``nice`` values for the same names.  Negative values need privilege;
#: failing to get them is reported, never fatal.
POSIX_NICE_LEVELS: dict[str, int] = {
    "idle": 19, "below": 10, "normal": 0, "above": -5, "high": -10, "realtime": -20,
}

PRIORITY_LABELS: dict[str, str] = {
    "idle": "Idle (only spare cycles)",
    "below": "Below normal (stay out of the way)",
    "normal": "Normal",
    "above": "Above normal",
    "high": "High  —  recommended, one per core",
    "realtime": "Realtime  —  maximum, can starve the desktop",
}

PRIORITY_ORDER: tuple[str, ...] = ("idle", "below", "normal", "above", "high", "realtime")

_THREAD_PRIORITY_HIGHEST = 2
_THREAD_PRIORITY_TIME_CRITICAL = 15


def _kernel32():
    """``kernel32`` with its signatures declared.

    Declaring these is not tidiness, it is correctness.  ``GetCurrentProcess``
    returns the pseudo-handle ``(HANDLE)-1``; with no ``restype`` ctypes hands
    it back as a 32-bit int and then passes it on as ``0x00000000FFFFFFFF``
    rather than ``0xFFFFFFFFFFFFFFFF``.  Windows rejects that as an invalid
    handle, ``SetPriorityClass`` quietly returns 0, and a fleet that reports
    itself as HIGH runs at Normal - which is exactly the kind of confident,
    wrong status line this whole rewrite exists to stop shipping.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.GetCurrentThread.argtypes = []
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetPriorityClass.restype = wintypes.DWORD
    kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel32.SetThreadPriority.restype = wintypes.BOOL
    kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    return kernel32


def current_priority() -> str:
    """The priority class this process is *actually* running at."""
    if sys.platform != "win32":
        try:
            return f"nice {os.nice(0)}"
        except (OSError, AttributeError):
            return "normal"
    try:
        kernel32 = _kernel32()
        value = kernel32.GetPriorityClass(kernel32.GetCurrentProcess())
    except Exception:
        return "unknown"
    for name, code in WINDOWS_PRIORITY_CLASSES.items():
        if code == value:
            return name
    return "unknown"


def apply_process_priority(level: str) -> str:
    """Raise the calling process to *level*.  Returns the level actually taken.

    The return value is read back from the operating system rather than assumed
    from a successful call, so the monitor shows what the fleet really got.
    Realtime is genuinely dangerous - a realtime process that spins can lock a
    desktop out of its own input queue - and needs a privilege an ordinary user
    does not have, so a refused request steps down instead of failing the sweep.
    """
    level = level if level in WINDOWS_PRIORITY_CLASSES else "high"
    if sys.platform == "win32":
        try:
            kernel32 = _kernel32()
            handle = kernel32.GetCurrentProcess()
        except Exception:
            return current_priority()
        # Step *down* from what was asked for, never up, and never below normal:
        # a refused realtime request should become high, not idle, and a refused
        # "above" must not be answered with something higher than the reader
        # asked for.
        wanted = PRIORITY_ORDER.index(level)
        floor = PRIORITY_ORDER.index("normal")
        ladder = ([PRIORITY_ORDER[step] for step in range(wanted, floor - 1, -1)]
                  if wanted >= floor else [level])
        for candidate in ladder:
            if kernel32.SetPriorityClass(handle, WINDOWS_PRIORITY_CLASSES[candidate]):
                thread_priority = (_THREAD_PRIORITY_TIME_CRITICAL
                                   if candidate == "realtime" else _THREAD_PRIORITY_HIGHEST)
                kernel32.SetThreadPriority(kernel32.GetCurrentThread(), thread_priority)
                return current_priority()
        return current_priority()
    try:
        os.nice(POSIX_NICE_LEVELS.get(level, 0))
        return level
    except (OSError, AttributeError, PermissionError):
        return "normal"


def boost_current_thread() -> None:
    """Nudge a walker thread up a notch on Windows; a no-op everywhere else.

    Directory listing on a high-latency share is dominated by waiting, so the
    walkers are cheap to prioritise and expensive to leave behind a busy pool.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = _kernel32()
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_PRIORITY_HIGHEST)
    except Exception:
        pass


def cpu_topology() -> tuple[int, int]:
    """``(physical cores, logical processors)``, each at least 1.

    ``os.cpu_count`` reports logical processors.  The physical count is only
    used to explain the machine in the settings window, so an unavailable value
    degrades to the logical one rather than failing.
    """
    logical = os.cpu_count() or 4
    physical = logical
    try:  # Python 3.13+ knows the affinity-limited count
        affinity = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        logical = affinity or logical
    except (AttributeError, OSError):
        pass
    if sys.platform == "win32":
        try:
            import subprocess

            output = subprocess.run(
                ["wmic", "cpu", "get", "NumberOfCores"],
                capture_output=True, text=True, timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            numbers = [int(part) for part in output.split() if part.isdigit()]
            if numbers:
                physical = sum(numbers)
        except Exception:
            physical = max(1, logical // 2)
    return max(1, physical), max(1, logical)


# ───────────────────────────────── configuration ───────────────────────────

#: Windows will not wait on more than 64 handles at once, and the pool needs a
#: few for itself.  Beyond this the fleet stops being faster anyway.
MAX_PROCESSES = 61

#: The longest triage will sit on a partly-filled batch before handing it to the
#: fleet.  Without a time bound, a library smaller than one batch - or a slow
#: share where a batch takes minutes to fill - would leave every core idle until
#: the walk finished, which is the exact serialisation this design exists to end.
_TRIAGE_MAX_HOLD = 0.15

#: How long the whole fleet may take to finish the books already in its hands
#: once the sweep is otherwise done.  Generous, because the last book a worker
#: draws can be a 200 MB scan, and killing it loses that book.
_SHUTDOWN_GRACE = 900.0

#: A cancelled sweep is in a hurry: the reader asked it to stop.
_CANCEL_GRACE = 25.0


@dataclass(slots=True)
class ScanConfig:
    """Every knob the sweep exposes.  Serialised straight into reader state."""

    # ── what to sweep ──────────────────────────────────────────────────────
    extensions: tuple[str, ...] = tuple(sorted(BOOK_SUFFIXES))
    skip_directories: tuple[str, ...] = tuple(sorted(SKIP_DIRECTORIES))
    exclude_globs: tuple[str, ...] = ()
    max_depth: int = 0                 # 0 = unlimited
    follow_symlinks: bool = False
    min_bytes: int = 0
    max_bytes: int = 0                 # 0 = unlimited

    # ── how hard to sweep ──────────────────────────────────────────────────
    processes: int = 0                 # 0 = one per logical processor
    priority: str = "high"
    walkers: int = 0                   # 0 = auto from the core count
    walk_queue_depth: int = 0          # 0 = auto
    job_queue_depth: int = 0           # 0 = auto
    result_queue_depth: int = 0        # 0 = auto
    triage_batch: int = 512
    write_batch: int = 400

    # ── how deeply to read each book ───────────────────────────────────────
    with_text: bool = True
    text_budget: int = DEFAULT_TEXT_BUDGET
    pdf_page_cap: int = 0              # 0 = every page until the budget runs out

    # ── which engine does the reading ──────────────────────────────────────
    #: ``auto`` asks the machine at run time and takes the best path that is
    #: actually present, so one build runs on a workstation with an RTX card and
    #: on a laptop with no GPU at all, with nothing to switch and no second
    #: version to maintain.  See :mod:`lumen_reader.accel`.
    extraction_backend: str = "auto"

    # ── behaviour ──────────────────────────────────────────────────────────
    scan_on_startup: bool = True
    prune_missing: bool = True

    # ── derived sizing ─────────────────────────────────────────────────────

    def resolved_processes(self) -> int:
        wanted = self.processes if self.processes > 0 else (os.cpu_count() or 4)
        return max(1, min(MAX_PROCESSES, int(wanted)))

    def resolved_walkers(self) -> int:
        if self.walkers > 0:
            return max(1, min(256, int(self.walkers)))
        # A share is mostly latency, so oversubscribing the walk pays for itself
        # long before it costs anything: these threads sit in ``scandir``.
        return max(4, min(64, (os.cpu_count() or 4) * 2))

    def resolved_walk_queue(self) -> int:
        return self.walk_queue_depth if self.walk_queue_depth > 0 else 20_000

    def resolved_job_queue(self) -> int:
        return self.job_queue_depth if self.job_queue_depth > 0 else self.resolved_processes() * 64

    def resolved_result_queue(self) -> int:
        return (self.result_queue_depth if self.result_queue_depth > 0
                else self.resolved_processes() * 64)

    def effective_text_budget(self) -> int:
        return max(0, int(self.text_budget)) if self.with_text else 0

    def suffix_set(self) -> set[str]:
        return {e if e.startswith(".") else f".{e}" for e in
                (x.strip().casefold() for x in self.extensions) if e}

    # ── persistence ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "extensions": list(self.extensions),
            "skip_directories": list(self.skip_directories),
            "exclude_globs": list(self.exclude_globs),
            "max_depth": self.max_depth,
            "follow_symlinks": self.follow_symlinks,
            "min_bytes": self.min_bytes,
            "max_bytes": self.max_bytes,
            "processes": self.processes,
            "priority": self.priority,
            "walkers": self.walkers,
            "walk_queue_depth": self.walk_queue_depth,
            "job_queue_depth": self.job_queue_depth,
            "result_queue_depth": self.result_queue_depth,
            "triage_batch": self.triage_batch,
            "write_batch": self.write_batch,
            "with_text": self.with_text,
            "text_budget": self.text_budget,
            "pdf_page_cap": self.pdf_page_cap,
            "extraction_backend": self.extraction_backend,
            "scan_on_startup": self.scan_on_startup,
            "prune_missing": self.prune_missing,
        }

    @classmethod
    def from_mapping(cls, data: Any) -> "ScanConfig":
        """Rebuild from stored state, ignoring anything malformed.

        Settings files outlive the code that wrote them, so every field is
        coerced individually and a bad one falls back to its default instead of
        refusing to start the scanner.
        """
        config = cls()
        if not isinstance(data, dict):
            return config

        def text_tuple(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            value = data.get(key)
            if not isinstance(value, (list, tuple)):
                return fallback
            cleaned = tuple(str(item).strip() for item in value if str(item).strip())
            return cleaned or fallback

        def whole(key: str, fallback: int, low: int = 0, high: int = 1 << 62) -> int:
            try:
                return max(low, min(high, int(data.get(key, fallback))))
            except (TypeError, ValueError):
                return fallback

        def flag(key: str, fallback: bool) -> bool:
            value = data.get(key, fallback)
            return bool(value) if isinstance(value, (bool, int)) else fallback

        config.extensions = text_tuple("extensions", config.extensions)
        config.skip_directories = text_tuple("skip_directories", config.skip_directories)
        config.exclude_globs = text_tuple("exclude_globs", ())
        config.max_depth = whole("max_depth", 0, 0, 512)
        config.follow_symlinks = flag("follow_symlinks", False)
        config.min_bytes = whole("min_bytes", 0)
        config.max_bytes = whole("max_bytes", 0)
        config.processes = whole("processes", 0, 0, MAX_PROCESSES)
        priority = str(data.get("priority", "high")).strip().casefold()
        config.priority = priority if priority in WINDOWS_PRIORITY_CLASSES else "high"
        config.walkers = whole("walkers", 0, 0, 256)
        config.walk_queue_depth = whole("walk_queue_depth", 0, 0, 10_000_000)
        config.job_queue_depth = whole("job_queue_depth", 0, 0, 1_000_000)
        config.result_queue_depth = whole("result_queue_depth", 0, 0, 1_000_000)
        config.triage_batch = whole("triage_batch", 512, 16, 100_000)
        config.write_batch = whole("write_batch", 400, 16, 100_000)
        config.with_text = flag("with_text", True)
        config.text_budget = whole("text_budget", DEFAULT_TEXT_BUDGET, 0, 1 << 30)
        config.pdf_page_cap = whole("pdf_page_cap", 0, 0, 1_000_000)
        backend = str(data.get("extraction_backend", "auto")).strip().casefold()
        config.extraction_backend = backend or "auto"
        config.scan_on_startup = flag("scan_on_startup", True)
        config.prune_missing = flag("prune_missing", True)
        return config


# ─────────────────────────────── live telemetry ────────────────────────────
#
# One shared ctypes block per worker.  Six integers and a path buffer, written
# by the worker and read by the monitor.  Deliberately lock-free: the only
# consumer is a display, and a torn read costs one frame of a filename rather
# than a stalled extractor.

_V_PID, _V_STATE, _V_DONE, _V_FAILED, _V_BYTES, _V_STARTED_MS = range(6)
_VITALS_SLOTS = 6
_PATH_BYTES = 512

_STATE_IDLE, _STATE_BUSY, _STATE_STOPPED = 0, 1, 2
_STATE_NAMES = {_STATE_IDLE: "idle", _STATE_BUSY: "busy", _STATE_STOPPED: "stopped"}


@dataclass(slots=True)
class WorkerSnapshot:
    """What one extractor process is doing, right now."""

    index: int
    pid: int
    state: str
    done: int
    failed: int
    bytes_done: int
    current: str
    busy_seconds: float


@dataclass(slots=True)
class ScanSnapshot:
    """A complete, consistent picture of the sweep for one repaint."""

    phase: str = "idle"
    root: str = ""
    started_at: float = 0.0
    elapsed: float = 0.0
    paused: bool = False
    walk_complete: bool = False
    #: Seconds from the start of the sweep, proving the stages overlap: the
    #: fleet gets its first book at ``first_dispatch_after`` and the tree is not
    #: fully listed until ``walk_finished_after``.  Whenever listing costs
    #: anything at all - any share, any deep tree - the first is the smaller
    #: number, which is the whole reason this scanner replaced the old one.
    #: Measured with ``perf_counter``: Windows' monotonic clock ticks every
    #: 15.6 ms and would round both of them to the same instant on a local disk.
    first_dispatch_after: float = 0.0
    walk_finished_after: float = 0.0

    dirs_swept: int = 0
    dirs_pending: int = 0
    entries_seen: int = 0
    books_found: int = 0
    books_unchanged: int = 0
    books_indexed: int = 0
    books_failed: int = 0
    bytes_found: int = 0
    bytes_indexed: int = 0

    books_per_second: float = 0.0
    bytes_per_second: float = 0.0
    eta_seconds: float = -1.0

    processes: int = 0
    walkers: int = 0
    priority: str = ""
    backend: str = ""
    backend_reason: str = ""
    workers: list[WorkerSnapshot] = field(default_factory=list)
    history: list[float] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str = ""
    counts: LibraryCounts | None = None

    @property
    def books_pending(self) -> int:
        """Books found but not yet resolved one way or the other."""
        settled = self.books_unchanged + self.books_indexed + self.books_failed
        return max(0, self.books_found - settled)

    @property
    def active_workers(self) -> int:
        return sum(1 for worker in self.workers if worker.state == "busy")

    @property
    def running(self) -> bool:
        return self.phase in {"starting", "sweeping", "finishing"}


# ─────────────────────────── the extractor process ─────────────────────────


def _write_current(buffer: Any, text: str) -> None:
    raw = text.encode("utf-8", "replace")[:_PATH_BYTES - 1]
    buffer[:len(raw)] = raw
    buffer[len(raw)] = 0


def _read_current(buffer: Any) -> str:
    raw = bytes(buffer)
    end = raw.find(b"\x00")
    return raw[:end if end >= 0 else len(raw)].decode("utf-8", "replace")


def extractor_main(
    worker_index: int,
    jobs: Any,
    results: Any,
    vitals: Any,
    path_buffer: Any,
    abort: Any,
    priority: str,
) -> None:
    """One ultra-priority extractor.  Runs in its own OS process, forever.

    Lives at module level because Windows spawns workers by re-importing this
    module and looking the target up by name.  It must never raise: an extractor
    that dies leaves its share of the library unindexed and no message behind,
    which is precisely the silent failure this rewrite exists to end.
    """
    taken = apply_process_priority(priority)
    vitals[_V_PID] = os.getpid()
    vitals[_V_STATE] = _STATE_IDLE
    try:
        results.put(("hello", worker_index, os.getpid(), taken))
    except Exception:
        pass

    while True:
        try:
            job = jobs.get()
        except (EOFError, OSError, KeyboardInterrupt):
            break
        if job is None or abort.is_set():
            break

        path_text, suffix, text_budget, page_cap, size, mtime_ns = job
        vitals[_V_STATE] = _STATE_BUSY
        vitals[_V_STARTED_MS] = int(time.monotonic() * 1000)
        _write_current(path_buffer, path_text)

        try:
            record = extract_book((path_text, suffix, text_budget, page_cap))
        except BaseException as exception:   # a worker must outlive any book
            record = {
                "path": path_text, "ok": False, "title": Path(path_text).stem,
                "error": f"{type(exception).__name__}: {exception}"[:400],
                "author": "", "publisher": "", "language": "", "subjects": "",
                "description": "", "pages": 0, "body": "",
            }

        # The walk already paid for these.  Carrying them back means the writer
        # never has to stat the file again - on a share that would be one extra
        # network round trip per book, on the one thread that must never stall.
        record["ext"] = suffix
        record["size"] = size
        record["mtime_ns"] = mtime_ns

        vitals[_V_DONE] += 1
        vitals[_V_BYTES] += int(size)
        if not record.get("ok", True):
            vitals[_V_FAILED] += 1
        vitals[_V_STATE] = _STATE_IDLE
        _write_current(path_buffer, "")

        try:
            results.put(("book", worker_index, record))
        except Exception:
            break

    vitals[_V_STATE] = _STATE_STOPPED
    _write_current(path_buffer, "")


# ──────────────────────────────── the scanner ──────────────────────────────


class TurboScanner:
    """Runs one sweep of one root, and reports on itself while it does.

    The object is single-use: ``start()`` once, watch it through ``snapshot()``,
    then let it finish or ``cancel()`` it.  Starting a second sweep means a
    second scanner, which keeps the shared-memory blocks unambiguous.
    """

    def __init__(
        self,
        database: str | Path,
        root: str | Path,
        config: ScanConfig | None = None,
        *,
        on_message: Callable[[str], None] | None = None,
    ):
        self.database = Path(database)
        self.root = Path(root).expanduser()
        self.root_key = normalize_root(self.root)
        self.config = config or ScanConfig()
        self._on_message = on_message

        self._processes = self.config.resolved_processes()
        self._walker_count = self.config.resolved_walkers()
        self._suffixes = self.config.suffix_set() or set(BOOK_SUFFIXES)
        self._skip = {name.casefold() for name in self.config.skip_directories}
        self._globs = tuple(pattern.casefold() for pattern in self.config.exclude_globs)
        self._text_budget = self.config.effective_text_budget()

        # ── counters, all guarded by one lock so a snapshot is consistent ──
        self._lock = threading.Lock()
        self._phase = "idle"
        self._error = ""
        self._started_at = 0.0
        self._finished_at = 0.0
        self._perf_zero = 0.0
        self._first_dispatch_after = 0.0
        self._walk_finished_after = 0.0
        self._walk_complete = False
        self._dirs_swept = 0
        self._dirs_pending = 0
        self._entries_seen = 0
        self._books_found = 0
        self._books_unchanged = 0
        self._books_indexed = 0
        self._books_failed = 0
        self._bytes_found = 0
        self._bytes_indexed = 0
        self._counts: LibraryCounts | None = None
        self._messages: deque[str] = deque(maxlen=400)
        self._history: deque[float] = deque(maxlen=240)
        self._last_sample = (0.0, 0)
        self._rate = 0.0
        self._byte_rate = 0.0
        self._priority_taken = self.config.priority

        # ── control ────────────────────────────────────────────────────────
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._done = threading.Event()
        self._generation = int(time.time())

        # ── stage plumbing, created in start() ─────────────────────────────
        self._context = mp.get_context("spawn")
        self._walk_queue: queue.Queue = queue.Queue(maxsize=self.config.resolved_walk_queue())
        self._touch_queue: queue.Queue = queue.Queue()
        self._jobs: Any = None
        self._results: Any = None
        self._abort: Any = None
        self._vitals: list[Any] = []
        self._paths: list[Any] = []
        self._worker_pids: list[int] = [0] * self._processes
        self._worker_processes: list[Any] = []
        self.backend = self.config.extraction_backend
        self.backend_reason = ""
        self._threads: list[threading.Thread] = []
        self._inline = self._processes <= 1

        # ── walker bookkeeping ─────────────────────────────────────────────
        self._dir_stack: deque[tuple[str, int]] = deque()
        self._dir_lock = threading.Lock()
        self._dir_outstanding = 0
        self._dir_wake = threading.Condition(self._dir_lock)
        self._visited: set[tuple[int, int]] = set()

    # ── public surface ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Bring the whole pipeline up and return immediately."""
        if self._phase != "idle":
            raise RuntimeError("a TurboScanner runs exactly one sweep")
        self._started_at = time.monotonic()
        self._perf_zero = time.perf_counter()
        self._phase = "starting"

        # Resolve the engine before anything spawns, and say which one won.  A
        # sweep that quietly ran on the CPU while the reader believed the GPU
        # was working would be the same class of lie this rewrite exists to end.
        from .accel import resolve_extraction_backend

        self.backend, self.backend_reason = resolve_extraction_backend(
            self.config.extraction_backend
        )
        self._say(
            f"Sweep armed on {self.root}  ·  {self._processes} extractor "
            f"{'process' if self._processes == 1 else 'processes'} at "
            f"{self.config.priority.upper()} priority  ·  {self._walker_count} walker threads"
        )
        self._say(f"Engine: {self.backend}  —  {self.backend_reason}")
        control = threading.Thread(target=self._control, name="lumen-sweep", daemon=True)
        control.start()
        self._control_thread = control

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()
        if self._abort is not None:
            self._abort.set()
        with self._dir_lock:
            self._dir_wake.notify_all()
        self._say("Stop requested — draining the fleet.")

    def pause(self) -> None:
        if not self._pause.is_set():
            self._pause.set()
            self._say("Paused. The fleet is holding; nothing new is being dispatched.")

    def resume(self) -> None:
        if self._pause.is_set():
            self._pause.clear()
            self._say("Resumed.")

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def is_running(self) -> bool:
        return self._phase in {"starting", "sweeping", "finishing"}

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # ── the live picture ───────────────────────────────────────────────────

    def snapshot(self) -> ScanSnapshot:
        """A consistent copy of every counter plus the per-worker vitals."""
        now = time.monotonic()
        with self._lock:
            elapsed = ((self._finished_at or now) - self._started_at) if self._started_at else 0.0
            settled = self._books_indexed + self._books_failed
            snapshot = ScanSnapshot(
                phase=self._phase,
                root=str(self.root),
                started_at=self._started_at,
                elapsed=elapsed,
                paused=self._pause.is_set(),
                walk_complete=self._walk_complete,
                first_dispatch_after=self._first_dispatch_after,
                walk_finished_after=self._walk_finished_after,
                dirs_swept=self._dirs_swept,
                dirs_pending=self._dirs_pending,
                entries_seen=self._entries_seen,
                books_found=self._books_found,
                books_unchanged=self._books_unchanged,
                books_indexed=self._books_indexed,
                books_failed=self._books_failed,
                bytes_found=self._bytes_found,
                bytes_indexed=self._bytes_indexed,
                books_per_second=self._rate,
                bytes_per_second=self._byte_rate,
                processes=self._processes,
                walkers=self._walker_count,
                priority=self._priority_taken,
                backend=self.backend,
                backend_reason=self.backend_reason,
                history=list(self._history),
                messages=list(self._messages),
                error=self._error,
                counts=self._counts,
            )
            stale_total = self._books_found - self._books_unchanged

        remaining = max(0, stale_total - settled)
        if snapshot.walk_complete and snapshot.books_per_second > 0.01 and remaining:
            snapshot.eta_seconds = remaining / snapshot.books_per_second
        elif snapshot.walk_complete and not remaining:
            snapshot.eta_seconds = 0.0

        snapshot.workers = self._worker_snapshots(now)
        return snapshot

    def _worker_snapshots(self, now: float) -> list[WorkerSnapshot]:
        workers: list[WorkerSnapshot] = []
        for position, vitals in enumerate(self._vitals):
            state_code = int(vitals[_V_STATE])
            started_ms = int(vitals[_V_STARTED_MS])
            busy = 0.0
            if state_code == _STATE_BUSY and started_ms:
                busy = max(0.0, now - started_ms / 1000.0)
            workers.append(WorkerSnapshot(
                index=position,
                pid=int(vitals[_V_PID]),
                state=_STATE_NAMES.get(state_code, "idle"),
                done=int(vitals[_V_DONE]),
                failed=int(vitals[_V_FAILED]),
                bytes_done=int(vitals[_V_BYTES]),
                current=_read_current(self._paths[position]),
                busy_seconds=busy,
            ))
        return workers

    # ── internals ──────────────────────────────────────────────────────────

    def _say(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        with self._lock:
            self._messages.append(line)
        if self._on_message is not None:
            try:
                self._on_message(line)
            except Exception:
                pass

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        """A connection owned by whichever thread calls this.

        Each stage opens its own handle: WAL gives the triage reader a stable
        snapshot while the writer commits, and never sharing a handle across
        threads removes a whole class of SQLite misuse errors.
        """
        connection = sqlite3.connect(str(self.database), timeout=60.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-131072")
        if not read_only:
            connection.execute("PRAGMA wal_autocheckpoint=2000")
        return connection

    # ── stage 0: the conductor ─────────────────────────────────────────────

    def _control(self) -> None:
        try:
            self._run_pipeline()
        except BaseException as exception:      # the sweep reports, never dies
            with self._lock:
                self._error = f"{type(exception).__name__}: {exception}"[:400]
                self._phase = "error"
            self._say(f"SWEEP FAILED: {self._error}")
        finally:
            with self._lock:
                self._finished_at = time.monotonic()
            self._done.set()

    def _run_pipeline(self) -> None:
        # The schema (and any migration) must exist before a worker touches it.
        with LibraryIndex(self.database, text_budget=self._text_budget) as index:
            self._generation = index.next_generation(self.root_key)

        with self._lock:
            self._phase = "sweeping"

        self._start_extractors()
        sampler = threading.Thread(target=self._sample_rates, name="lumen-rates", daemon=True)
        sampler.start()

        writer = threading.Thread(target=self._writer, name="lumen-writer", daemon=True)
        triage = threading.Thread(target=self._triage, name="lumen-triage", daemon=True)
        writer.start()
        triage.start()

        walkers = self._start_walkers()
        for walker in walkers:
            walker.join()
        with self._lock:
            self._walk_complete = True
            self._walk_finished_after = time.perf_counter() - self._perf_zero
            self._phase = "finishing"
        self._say(
            f"Walk complete: {self._dirs_swept:,} directories, "
            f"{self._books_found:,} books, {self._entries_seen:,} entries examined."
        )

        self._walk_queue.put(None)              # triage drains, then dismisses the fleet
        triage.join()
        self._stop_extractors()
        self._touch_queue.put(None)             # writer drains, then commits and exits
        writer.join()
        sampler.join(timeout=1.0)

        self._finalise()

    def _finalise(self) -> None:
        cancelled = self._cancel.is_set()
        with LibraryIndex(self.database, text_budget=self._text_budget) as index:
            if self.config.prune_missing and not cancelled:
                removed = index.prune_generation(self.root_key, self._generation)
                if removed:
                    self._say(f"{removed:,} book(s) no longer on disk were removed from the index.")
            counts = index.counts(self.root_key)
            index.record_scan(
                self.root_key,
                generation=self._generation,
                seconds=max(0.0, time.monotonic() - self._started_at),
                found=self._books_found,
                indexed=self._books_indexed,
                skipped=self._books_unchanged,
                failed=self._books_failed,
                cancelled=cancelled,
            )
        with self._lock:
            self._counts = counts
            self._phase = "cancelled" if cancelled else "done"
            settled = self._books_indexed + self._books_unchanged + self._books_failed
            unaccounted = max(0, self._books_found - settled)
        self._say(
            ("Sweep stopped by request. " if cancelled else "Sweep complete. ")
            + f"{counts.total:,} books in the index  ·  "
            f"{self._books_indexed:,} newly read  ·  {self._books_unchanged:,} already current  ·  "
            f"{self._books_failed:,} unreadable  ·  {time.monotonic() - self._started_at:,.1f}s"
        )
        # Every book found must be accounted for one way or another.  Saying so
        # out loud is the difference between a sweep that quietly lost work and
        # one the reader can trust; they are re-read on the next sweep, because
        # nothing was written for them.
        if unaccounted and not cancelled:
            self._say(
                f"WARNING: {unaccounted:,} book(s) were found but never finished reading. "
                f"They are not in the index and the next sweep will pick them up."
            )

    # ── stage 1: the walker fleet ──────────────────────────────────────────

    def _start_walkers(self) -> list[threading.Thread]:
        with self._dir_lock:
            self._dir_stack.append((str(self.root), 0))
            self._dir_outstanding = 1
        walkers = [
            threading.Thread(target=self._walk, name=f"lumen-walk-{position}", daemon=True)
            for position in range(self._walker_count)
        ]
        for walker in walkers:
            walker.start()
        return walkers

    def _claim_directory(self) -> tuple[str, int] | None:
        """Take the next directory, or block until one appears or the walk ends.

        A plain queue cannot answer "is the walk finished?", because empty only
        means *right now*: another walker may be about to push ten children.  The
        outstanding counter closes that race - the walk is over when the stack is
        empty and no walker is still inside ``scandir``.
        """
        with self._dir_lock:
            while True:
                if self._cancel.is_set():
                    return None
                if self._dir_stack:
                    return self._dir_stack.pop()
                if self._dir_outstanding <= 0:
                    return None
                self._dir_wake.wait(0.2)

    def _walk(self) -> None:
        boost_current_thread()
        suffixes = self._suffixes
        skip = self._skip
        globs = self._globs
        max_depth = self.config.max_depth
        follow = self.config.follow_symlinks
        min_bytes, max_bytes = self.config.min_bytes, self.config.max_bytes

        while True:
            claimed = self._claim_directory()
            if claimed is None:
                break
            directory, depth = claimed
            children: list[tuple[str, int]] = []
            found: list[tuple[str, int, int, str]] = []
            examined = 0

            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if self._cancel.is_set():
                            break
                        examined += 1
                        try:
                            if entry.is_dir(follow_symlinks=follow):
                                name = entry.name.casefold()
                                if name in skip or entry.name.startswith("$"):
                                    continue
                                if max_depth and depth + 1 > max_depth:
                                    continue
                                if globs and self._excluded(entry.path, globs):
                                    continue
                                if follow and not self._first_visit(entry):
                                    continue
                                children.append((entry.path, depth + 1))
                                continue
                            suffix = os.path.splitext(entry.name)[1].casefold()
                            if suffix not in suffixes:
                                continue
                            if globs and self._excluded(entry.path, globs):
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size < min_bytes:
                                continue
                            if max_bytes and stat.st_size > max_bytes:
                                continue
                            found.append((entry.path, stat.st_size, stat.st_mtime_ns, suffix))
                        except OSError:
                            continue
            except (OSError, PermissionError) as exception:
                self._say(f"Skipped {directory} — {type(exception).__name__}")

            with self._lock:
                self._dirs_swept += 1
                self._entries_seen += examined
                self._books_found += len(found)
                self._bytes_found += sum(item[1] for item in found)

            for item in found:
                while True:
                    try:
                        self._walk_queue.put(item, timeout=0.25)
                        break
                    except queue.Full:
                        if self._cancel.is_set():
                            break
                if self._cancel.is_set():
                    break

            with self._dir_lock:
                self._dir_stack.extend(children)
                self._dir_outstanding += len(children) - 1
                self._dirs_pending = len(self._dir_stack)
                self._dir_wake.notify_all()

    @staticmethod
    def _excluded(path: str, globs: Sequence[str]) -> bool:
        lowered = path.casefold()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in globs)

    def _first_visit(self, entry: os.DirEntry) -> bool:
        """Guard against symlink loops when the reader asks us to follow them."""
        try:
            stat = entry.stat(follow_symlinks=True)
        except OSError:
            return False
        key = (stat.st_dev, stat.st_ino)
        with self._dir_lock:
            if key in self._visited:
                return False
            self._visited.add(key)
        return True

    # ── stage 2: triage ────────────────────────────────────────────────────

    def _triage(self) -> None:
        """Decide, in batches, which books actually need to be read again.

        The lookup is chunked rather than "load every indexed path into a dict"
        so the memory cost is the chunk, not the library.  A hit whose size and
        mtime still match is stamped with this scan's generation and never
        reaches an extractor.
        """
        connection = self._connect(read_only=True)
        batch: list[tuple[str, int, int, str]] = []
        budget = self._text_budget
        page_cap = self.config.pdf_page_cap
        chunk = max(16, self.config.triage_batch)
        last_flush = time.monotonic()

        def flush() -> None:
            nonlocal last_flush
            if batch:
                self._triage_batch(connection, batch, budget, page_cap)
                batch.clear()
            last_flush = time.monotonic()

        try:
            while True:
                try:
                    item = self._walk_queue.get(timeout=0.05)
                except queue.Empty:
                    # The walk has gone quiet - a deep directory on a slow share,
                    # or simply a small library.  Whatever is already in hand goes
                    # to the fleet now.  Waiting for a full batch here is what
                    # would leave every core idle for the length of a NAS walk.
                    flush()
                    if self._cancel.is_set():
                        break
                    continue
                if item is None:
                    break
                batch.append(item)
                if len(batch) >= chunk or time.monotonic() - last_flush >= _TRIAGE_MAX_HOLD:
                    flush()
                if self._cancel.is_set():
                    break
            if not self._cancel.is_set():
                flush()
        finally:
            connection.close()

    def _triage_batch(
        self,
        connection: sqlite3.Connection,
        batch: Sequence[tuple[str, int, int, str]],
        budget: int,
        page_cap: int,
    ) -> None:
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.1)

        marks = ",".join("?" * len(batch))
        known: dict[str, tuple[int, int, int, int]] = {}
        try:
            rows = connection.execute(
                f"SELECT id, path, size, mtime_ns, has_text FROM books"
                f" WHERE root = ? AND path IN ({marks})",
                [self.root_key, *(item[0] for item in batch)],
            )
            for row in rows:
                known[row["path"]] = (row["id"], row["size"], row["mtime_ns"], row["has_text"])
        except sqlite3.Error:
            known = {}

        want_text = budget > 0
        unchanged: list[int] = []
        for path_text, size, mtime_ns, suffix in batch:
            previous = known.get(path_text)
            if previous is not None:
                book_id, old_size, old_mtime, has_text = previous
                if old_size == size and old_mtime == mtime_ns and (has_text == 1 or not want_text):
                    unchanged.append(book_id)
                    continue
            self._dispatch((path_text, suffix, budget, page_cap, size, mtime_ns))

        if unchanged:
            with self._lock:
                self._books_unchanged += len(unchanged)
            self._touch_queue.put(("touch", unchanged))

    def _dispatch(self, job: tuple[str, str, int, int, int, int]) -> None:
        """Hand one book to the fleet, waiting for a free slot if need be."""
        if not self._first_dispatch_after:
            with self._lock:
                self._first_dispatch_after = time.perf_counter() - self._perf_zero
        while not self._cancel.is_set():
            try:
                self._jobs.put(job, timeout=0.25)
                return
            except queue.Full:
                continue
            except (ValueError, OSError):
                return

    # ── stage 3: the extractor fleet ───────────────────────────────────────

    def _start_extractors(self) -> None:
        context = self._context
        self._abort = context.Event()
        self._jobs = queue.Queue(maxsize=self.config.resolved_job_queue()) if self._inline \
            else context.Queue(maxsize=self.config.resolved_job_queue())
        self._results = queue.Queue() if self._inline \
            else context.Queue(maxsize=self.config.resolved_result_queue())

        for _ in range(self._processes):
            if self._inline:
                self._vitals.append((ctypes.c_int64 * _VITALS_SLOTS)())
                self._paths.append((ctypes.c_char * _PATH_BYTES)())
            else:
                self._vitals.append(context.RawArray(ctypes.c_int64, _VITALS_SLOTS))
                self._paths.append(context.RawArray(ctypes.c_char, _PATH_BYTES))

        if self._inline:
            # One worker needs no IPC at all: a thread in this process reads the
            # books directly.  Identical bookkeeping, none of the spawn cost.
            thread = threading.Thread(
                target=extractor_main,
                args=(0, self._jobs, self._results, self._vitals[0], self._paths[0],
                      self._abort, "normal"),
                name="lumen-extract-0", daemon=True,
            )
            thread.start()
            self._worker_processes.append(thread)
            self._priority_taken = "normal"
            self._say("Single-worker mode: extraction runs in-process, no fleet spawned.")
            return

        for position in range(self._processes):
            process = context.Process(
                target=extractor_main,
                args=(position, self._jobs, self._results, self._vitals[position],
                      self._paths[position], self._abort, self.config.priority),
                name=f"lumen-extract-{position}", daemon=True,
            )
            process.start()
            self._worker_processes.append(process)
        self._say(f"{self._processes} extractor processes launched.")

    def _stop_extractors(self) -> None:
        """Dismiss the fleet, and wait properly for the book each one is on.

        The dismissal sentinels sit behind every real job, so a worker reaches
        one the moment it finishes the book in its hands - there is nothing to
        gain by cutting it short, and a great deal to lose.  A 20-second join
        used to kill whichever workers were unlucky enough to draw a 200 MB
        scanned PDF last: on a real 9,335-book shelf that silently dropped 26
        books while the sweep reported success.  Only a cancelled sweep is in
        any hurry, and even then a killed worker is reported rather than hidden.
        """
        for _ in range(self._processes):
            try:
                self._jobs.put(None, timeout=5.0)
            except Exception:
                break

        cancelled = self._cancel.is_set()
        deadline = time.monotonic() + (_CANCEL_GRACE if cancelled else _SHUTDOWN_GRACE)
        for worker in self._worker_processes:
            worker.join(timeout=max(0.5, deadline - time.monotonic()))
            if worker.is_alive():
                self._say(
                    f"{worker.name} did not stop in time and was terminated; the book "
                    f"it was reading is not indexed and will be picked up next sweep."
                )
                if hasattr(worker, "terminate"):
                    worker.terminate()
        try:
            self._results.put(None)
        except Exception:
            pass

    # ── stage 4: the writer ────────────────────────────────────────────────

    def _writer(self) -> None:
        """The only stage allowed to write.  Batches everything into one txn.

        It is deliberately the stage that never blocks on a full queue: results
        always drain here, which is what guarantees the pipeline above can never
        wedge itself.
        """
        connection = self._connect()
        cursor = connection.cursor()
        self._ensure_fts_map(connection)
        pending: list[dict[str, Any]] = []
        touches: list[int] = []
        results_open = True
        touches_open = True
        batch_size = max(16, self.config.write_batch)
        last_commit = time.monotonic()

        def flush() -> None:
            nonlocal last_commit
            if pending:
                self._commit_records(cursor, pending)
                pending.clear()
            if touches:
                cursor.executemany(
                    "UPDATE books SET seen_gen = ? WHERE id = ?",
                    [(self._generation, book_id) for book_id in touches],
                )
                touches.clear()
            connection.commit()
            last_commit = time.monotonic()

        try:
            while results_open or touches_open:
                worked = False

                if results_open:
                    try:
                        message = self._results.get(timeout=0.05)
                        worked = True
                        if message is None:
                            results_open = False
                        else:
                            kind = message[0]
                            if kind == "hello":
                                _, index, pid, taken = message
                                if 0 <= index < len(self._worker_pids):
                                    self._worker_pids[index] = pid
                                self._priority_taken = taken
                            elif kind == "book":
                                record = message[2]
                                pending.append(record)
                                with self._lock:
                                    if record.get("ok", True):
                                        self._books_indexed += 1
                                    else:
                                        self._books_failed += 1
                                    self._bytes_indexed += int(record.get("size") or 0)
                    except queue.Empty:
                        pass

                if touches_open:
                    try:
                        while True:
                            item = self._touch_queue.get_nowait()
                            worked = True
                            if item is None:
                                touches_open = False
                                break
                            touches.extend(item[1])
                    except queue.Empty:
                        pass

                if len(pending) + len(touches) >= batch_size or (
                    (pending or touches) and time.monotonic() - last_commit > 1.5
                ):
                    flush()
                if not worked:
                    time.sleep(0.01)
            flush()
        except Exception as exception:
            with self._lock:
                self._error = f"writer: {type(exception).__name__}: {exception}"[:400]
            self._say(f"Index write failed: {self._error}")
        finally:
            try:
                connection.commit()
            except sqlite3.Error:
                pass
            connection.close()

    def _ensure_fts_map(self, connection: sqlite3.Connection) -> None:
        """Give an index written before the rowid map one, before writing to it.

        This runs once in the life of an index, at the start of the first sweep
        after the upgrade, where the monitor's log makes it visible.  Skipping it
        would not be wrong, only slow - the deletes fall back to scanning - but
        slow here means ten seconds a book on a large index, which is what made
        a sweep look like it had hung.
        """
        try:
            if fts_map_ready(connection):
                return
            self._say("Upgrading the index: building the full-text rowid map…")
            started = time.monotonic()
            report = build_fts_map(connection, self._say)
            self._say(
                f"Index upgraded in {time.monotonic() - started:.1f}s: "
                f"{report['mapped']:,} books mapped — re-indexing a book no "
                f"longer scans the whole index."
            )
        except sqlite3.Error as exception:
            # Not fatal: without the map the deletes fall back to scanning, which
            # is correct and slow rather than wrong and fast.  Say so plainly.
            self._say(f"Could not build the full-text map ({exception}); "
                      f"this sweep will re-index the slow way.")

    def _commit_records(self, cursor: sqlite3.Cursor, records: Sequence[dict[str, Any]]) -> None:
        generation = self._generation
        for record in records:
            path_text = record["path"]
            name = os.path.basename(path_text)
            has_text = 1 if record.get("body") else 0
            size = int(record.get("size") or 0)
            mtime_ns = int(record.get("mtime_ns") or 0)

            existing = cursor.execute("SELECT id FROM books WHERE path = ?", (path_text,)).fetchone()
            values = (
                self.root_key, name, record.get("ext") or os.path.splitext(name)[1].casefold(),
                size, mtime_ns, record.get("title", ""), record.get("author", ""),
                record.get("publisher", ""), record.get("language", ""), record.get("subjects", ""),
                record.get("description", ""), int(record.get("pages") or 0), has_text,
                1 if record.get("ok", True) else 0, record.get("error", ""), generation,
            )
            if existing is not None:
                book_id = existing[0]
                # By rowid, through the map.  This single line is the difference
                # between a re-sweep that finishes and one that appears to hang:
                # the old `WHERE book_id = ?` had to scan the entire full-text
                # index for every book it replaced.
                drop_fts_rows(cursor, (book_id,))
                cursor.execute(
                    """UPDATE books SET root=?, name=?, ext=?, size=?, mtime_ns=?, title=?,
                       author=?, publisher=?, language=?, subjects=?, description=?, pages=?,
                       has_text=?, ok=?, error=?, seen_gen=? WHERE id=?""",
                    (*values, book_id),
                )
            else:
                cursor.execute(
                    """INSERT INTO books (root, name, ext, size, mtime_ns, title, author,
                       publisher, language, subjects, description, pages, has_text, ok, error,
                       seen_gen, path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, path_text),
                )
                book_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO books_fts (title, author, name, subjects, publisher, book_id)"
                " VALUES (?,?,?,?,?,?)",
                (record.get("title", ""), record.get("author", ""), name,
                 record.get("subjects", ""), record.get("publisher", ""), book_id),
            )
            meta_row = cursor.lastrowid
            content_row = None
            if has_text:
                cursor.execute(
                    "INSERT INTO content_fts (body, book_id) VALUES (?,?)",
                    (record.get("body", ""), book_id),
                )
                content_row = cursor.lastrowid
            # Remember where both rows landed, so the next sweep that replaces
            # this book can find them without reading the index end to end.
            cursor.execute(
                "INSERT INTO fts_rowid (book_id, meta_row, content_row) VALUES (?,?,?)"
                " ON CONFLICT(book_id) DO UPDATE SET meta_row = excluded.meta_row,"
                " content_row = excluded.content_row",
                (book_id, meta_row, content_row),
            )

    # ── the rate sampler ───────────────────────────────────────────────────

    def _sample_rates(self) -> None:
        """Four samples a second, exponentially smoothed.

        A raw books-per-second reading swings wildly - one 900-page PDF beside a
        thousand small EPUBs - and an ETA computed from it is noise.  The EMA is
        what makes the throughput graph and the estimate readable.
        """
        interval = 0.25
        previous_books, previous_bytes = 0, 0
        previous_time = time.monotonic()
        while not self._done.is_set():
            time.sleep(interval)
            now = time.monotonic()
            with self._lock:
                books = self._books_indexed + self._books_failed
                data = self._bytes_indexed
            span = max(1e-6, now - previous_time)
            instant = (books - previous_books) / span
            instant_bytes = (data - previous_bytes) / span
            previous_books, previous_bytes, previous_time = books, data, now
            with self._lock:
                self._rate = self._rate * 0.7 + instant * 0.3
                self._byte_rate = self._byte_rate * 0.7 + instant_bytes * 0.3
                self._history.append(self._rate)


# ─────────────────────────────── convenience ───────────────────────────────


def sweep(
    database: str | Path,
    root: str | Path,
    config: ScanConfig | None = None,
    *,
    on_progress: Callable[[ScanSnapshot], None] | None = None,
    poll: float = 0.25,
) -> ScanSnapshot:
    """Run one sweep to completion.  Used by the headless paths and the tests."""
    scanner = TurboScanner(database, root, config)
    scanner.start()
    while not scanner.wait(poll):
        if on_progress is not None:
            on_progress(scanner.snapshot())
    final = scanner.snapshot()
    if on_progress is not None:
        on_progress(final)
    return final


def describe_fleet(config: ScanConfig) -> str:
    """One sentence describing exactly what pressing Sweep will launch."""
    processes = config.resolved_processes()
    physical, logical = cpu_topology()
    priority = config.priority.upper()
    per_core = processes / max(1, logical)
    return (
        f"{processes} extractor process{'' if processes == 1 else 'es'} at {priority} priority "
        f"({per_core:.2f} per logical processor; this machine has {physical} cores / "
        f"{logical} logical processors) and {config.resolved_walkers()} walker threads."
    )
