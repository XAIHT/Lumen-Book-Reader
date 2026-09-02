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
  queue, so results drain during normal operation.  If it fails, it publishes
  a fatal event and every producer's timed queue operation observes the abort;
  both the healthy and failed paths therefore break the wait cycle.

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

from . import machine_profile
from .library_index import (
    BOOK_SUFFIXES,
    DEFAULT_TEXT_BUDGET,
    JOURNAL_SIZE_LIMIT_BYTES,
    SKIP_DIRECTORIES,
    LibraryCounts,
    LibraryIndex,
    build_fts_map,
    drop_fts_rows,
    extract_book,
    fts_map_ready,
    normalize_root,
)
from .text_safety import clean_unicode_text, escaped_for_log, require_utf8

# ───────────────────────────── process priority ────────────────────────────
#
# Angela's requirement was explicit: one *ultra priority* process per core - and
# it still is, on a machine with cores to spare, which is where it was written.
# It is deliberately not honoured on a four-core laptop or a spinning disk,
# because there it starves the reader's own thread and indexes more slowly for
# the trouble; see ``auto_priority`` and ``ScanConfig.resolved_processes``.  On
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
    "high": "High  —  one per core, for a machine with cores to spare",
    "realtime": "Realtime  —  maximum, can starve the desktop",
}

PRIORITY_ORDER: tuple[str, ...] = ("idle", "below", "normal", "above", "high", "realtime")

#: ``auto`` is the default, and it is not one of the classes above - it is a
#: *decision* made against :mod:`lumen_reader.machine_profile` at sweep time.
#: It is kept out of ``PRIORITY_ORDER`` deliberately: that tuple is the step-down
#: ladder ``apply_process_priority`` walks, and a pseudo-level in it would make
#: a refused request able to step down into "auto", which is not a thing Windows
#: can be asked for.
AUTO_PRIORITY = "auto"

#: What the settings window offers, in order.  ``auto`` first because it is the
#: right answer for almost everyone, and the only one that is right on every
#: machine.
PRIORITY_CHOICES: tuple[str, ...] = (AUTO_PRIORITY, *PRIORITY_ORDER)

PRIORITY_LABELS[AUTO_PRIORITY] = (
    "Automatic  —  match this machine, and leave the reader responsive"
)


def auto_priority(root: Any = None) -> str:
    """What ``auto`` resolves to for the volume holding *root*.

    One function so the answer cannot drift: the settings window, the sweep and
    a worker that was handed ``auto`` directly all ask here.
    """
    machine = machine_profile.profile(root)
    if machine.seek_bound or machine.logical_cpus <= 4:
        return "normal"
    if machine.logical_cpus <= machine_profile.SMALL_CPU_CEILING:
        return "above"
    return "high"

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
    # A worker handed ``auto`` asks the machine; anything else unrecognised
    # settles at Normal rather than High.  Guessing high for a value we could
    # not parse is the fail-open version of this decision, and the machine it
    # would hurt is the one least able to absorb it.
    if level == AUTO_PRIORITY:
        level = auto_priority()
    level = level if level in WINDOWS_PRIORITY_CLASSES else "normal"
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
    #: 0 = decide from the machine.  Not "one per logical processor": that is
    #: right on a workstation and wrong on a four-core laptop with a spinning
    #: disk, where it takes every core the reader needs to repaint.  See
    #: :meth:`resolved_processes`.
    processes: int = 0
    priority: str = AUTO_PRIORITY
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

    def resolved_processes(self, root: Any = None) -> int:
        """How many extractor processes ``auto`` means on *this* machine.

        An explicit setting is obeyed exactly, including one that is bad for the
        machine - the user asked, and second-guessing a deliberate choice is how
        a settings window stops being trusted.  Everything below applies only to
        the ``0`` default.

        Three measurements move the number, in this order:

        * **A seek-bound volume caps the fleet at two.**  A 7200 rpm disk has one
          head and serves on the order of 100 random IOPS.  Four extractors do
          not read four books at once from it, they make the head travel between
          four regions, and the sweep gets *slower* than a single worker while
          burning four cores to do it.  Two keeps a read queued while the other
          worker is parsing, which is all a spindle can usefully absorb.
        * **A small machine keeps a core for the reader.**  At or below
          :data:`~lumen_reader.machine_profile.SMALL_CPU_CEILING` logical
          processors, one process per core leaves the Qt thread nothing, and an
          unrepainted window reads as a hung program rather than a busy one.
          Above that ceiling nothing changes: one per core, as specified.
        * **Little memory caps it again.**  Every worker holds a book and its
          extracted text; on an 8 GB machine a wide fleet swaps.
        """
        if self.processes > 0:
            return max(1, min(MAX_PROCESSES, int(self.processes)))

        machine = machine_profile.profile(root)
        logical = machine.logical_cpus

        if machine.seek_bound:
            wanted = 2
        elif machine.storage == machine_profile.STORAGE_NETWORK:
            # A share is latency, not seeks: extra workers wait in parallel
            # instead of queueing behind one another, but the link is still the
            # ceiling, so there is no point going wide.
            wanted = min(8, max(2, logical))
        elif logical <= machine_profile.SMALL_CPU_CEILING:
            wanted = logical - 1
        else:
            wanted = logical

        if machine.tight_memory:
            wanted = min(wanted, 2)
        elif machine.low_memory:
            wanted = min(wanted, 4)

        return max(1, min(MAX_PROCESSES, wanted))

    def resolved_priority(self, root: Any = None) -> str:
        """The priority class the fleet will actually ask Windows for.

        ``auto`` is not timidity.  On a machine with cores to spare, HIGH is
        free - the reader's thread still gets scheduled - and it is what Angela
        specified.  On a machine where the fleet occupies every processor, HIGH
        is the difference between "the library is indexing" and "Lumen has
        frozen", because the Qt thread runs at Normal and now never wins.

        A spinning disk drops it further, to Normal: that sweep is waiting on
        the head, not on the CPU, so raising its priority buys nothing at all
        and costs the desktop everything.
        """
        if self.priority != AUTO_PRIORITY:
            return self.priority if self.priority in WINDOWS_PRIORITY_CLASSES else "high"
        return auto_priority(root)

    def resolved_walkers(self, root: Any = None) -> int:
        if self.walkers > 0:
            return max(1, min(256, int(self.walkers)))
        machine = machine_profile.profile(root)
        if machine.seek_bound:
            # Concurrent ``scandir`` against one head is the same thrash as
            # concurrent reads, and directory metadata is scattered.
            return 2
        # A share is mostly latency, so oversubscribing the walk pays for itself
        # long before it costs anything: these threads sit in ``scandir``.
        return max(4, min(64, machine.logical_cpus * 2))

    def resolved_walk_queue(self) -> int:
        return self.walk_queue_depth if self.walk_queue_depth > 0 else 20_000

    def resolved_job_queue(self, root: Any = None) -> int:
        return (self.job_queue_depth if self.job_queue_depth > 0
                else self.resolved_processes(root) * 64)

    def resolved_result_queue(self, root: Any = None) -> int:
        """Depth of the finished-work queue, which is where the memory is.

        A queued *job* is a path.  A queued *result* is a whole book's extracted
        text - up to ``text_budget`` characters - so this queue, not the index,
        is what a small machine runs out of memory on.  The budget is left
        alone: shrinking it would silently make search worse, and a slower sweep
        is a fair trade where a quietly less searchable library is not.
        """
        if self.result_queue_depth > 0:
            return self.result_queue_depth
        machine = machine_profile.profile(root)
        depth = 8 if machine.tight_memory else 16 if machine.low_memory else 64
        return self.resolved_processes(root) * depth

    def tuning_notes(self, root: Any = None) -> list[str]:
        """Why ``auto`` chose what it chose, in words a user can check.

        Every automatic decision here is visible and reversible.  A program that
        quietly decides your computer is slow, and never says so, is a program
        you cannot tell apart from a broken one.
        """
        machine = machine_profile.profile(root)
        notes = [machine.summary(), machine.storage_detail]
        if self.processes == 0:
            if machine.seek_bound:
                notes.append(
                    f"Fleet held to {self.resolved_processes(root)} processes: this volume "
                    f"pays a seek penalty, and more readers on one head is slower, not faster."
                )
            elif machine.logical_cpus <= machine_profile.SMALL_CPU_CEILING:
                notes.append(
                    f"Fleet held to {self.resolved_processes(root)} of "
                    f"{machine.logical_cpus} processors, so the reader keeps one and the "
                    f"window still repaints while the sweep runs."
                )
        if self.priority == AUTO_PRIORITY:
            notes.append(
                f"Priority {self.resolved_priority(root).upper()}: "
                + ("the sweep waits on the disk here, so raising it would only "
                   "starve the desktop." if machine.seek_bound else
                   "this machine has processors to spare." if machine.logical_cpus
                   > machine_profile.SMALL_CPU_CEILING else
                   "high priority on a small machine costs more than it buys.")
            )
        if machine.low_memory:
            notes.append(
                f"Result queue held to {self.resolved_result_queue(root)} books in flight "
                f"to keep extracted text out of the page file.  Text budget unchanged: "
                f"search quality is not traded for speed."
            )
        return [note for note in notes if note]

    def effective_text_budget(self) -> int:
        return max(0, int(self.text_budget)) if self.with_text else 0

    def suffix_set(self) -> set[str]:
        requested = {
            e if e.startswith(".") else f".{e}"
            for e in (x.strip().casefold() for x in self.extensions)
            if e
        }
        return requested & set(BOOK_SUFFIXES)

    def unsupported_suffixes(self) -> set[str]:
        requested = {
            e if e.startswith(".") else f".{e}"
            for e in (x.strip().casefold() for x in self.extensions)
            if e
        }
        return requested - set(BOOK_SUFFIXES)

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

        stored_extensions = text_tuple("extensions", config.extensions)
        config.extensions = tuple(
            normalized for extension in stored_extensions
            if (normalized := (
                extension if extension.startswith(".") else f".{extension}"
            ).casefold()) in BOOK_SUFFIXES
        ) or tuple(sorted(BOOK_SUFFIXES))
        config.skip_directories = text_tuple("skip_directories", config.skip_directories)
        config.exclude_globs = text_tuple("exclude_globs", ())
        config.max_depth = whole("max_depth", 0, 0, 512)
        config.follow_symlinks = flag("follow_symlinks", False)
        config.min_bytes = whole("min_bytes", 0)
        config.max_bytes = whole("max_bytes", 0)
        config.processes = whole("processes", 0, 0, MAX_PROCESSES)
        # A malformed or unknown priority falls back to ``auto`` rather than to
        # ``high``: a settings file we cannot read is exactly the case where the
        # machine should be asked instead of assumed.
        priority = str(data.get("priority", AUTO_PRIORITY)).strip().casefold()
        config.priority = priority if priority in PRIORITY_CHOICES else AUTO_PRIORITY
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

_STATE_IDLE, _STATE_BUSY, _STATE_PUBLISHING, _STATE_STOPPED = 0, 1, 2, 3
_STATE_NAMES = {
    _STATE_IDLE: "idle",
    _STATE_BUSY: "busy",
    _STATE_PUBLISHING: "publishing",
    _STATE_STOPPED: "stopped",
}


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
    books_extracted: int = 0
    books_indexed: int = 0
    books_failed: int = 0
    books_rejected: int = 0
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
    def books_committed(self) -> int:
        return self.books_indexed + self.books_failed

    @property
    def active_workers(self) -> int:
        return sum(1 for worker in self.workers if worker.state in {"busy", "publishing"})

    @property
    def running(self) -> bool:
        return self.phase in {"starting", "sweeping", "finishing", "failing"}


# ─────────────────────────── the extractor process ─────────────────────────


def _write_current(buffer: Any, text: str) -> None:
    raw = text.encode("utf-8", "replace")[:_PATH_BYTES - 1]
    buffer[:len(raw)] = raw
    buffer[len(raw)] = 0


def _read_current(buffer: Any) -> str:
    raw = bytes(buffer)
    end = raw.find(b"\x00")
    return raw[:end if end >= 0 else len(raw)].decode("utf-8", "replace")


def _publish_result(results: Any, message: Any, abort: Any) -> bool:
    """Publish with a cancellation-aware wait instead of an immortal ``put``.

    A bounded result queue is the memory safety valve.  It must be allowed to
    fill, but a dead writer must not turn that safety valve into twenty immortal
    worker processes.  The writer's fatal path sets *abort*, which releases each
    publisher within one timeout interval.
    """

    while not abort.is_set():
        try:
            results.put(message, timeout=0.25)
            return True
        except queue.Full:
            continue
        except (EOFError, OSError, ValueError):
            return False
    return False


_INDEX_TEXT_FIELDS = (
    "title", "author", "publisher", "language", "subjects", "description", "body", "error"
)


def _sanitize_index_record(record: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Copy a worker record into the strict UTF-8 contract of SQLite/FTS5."""

    cleaned = dict(record)
    cleaned["path"] = require_utf8(cleaned.get("path", ""), label="book path")
    changed = set(str(field) for field in cleaned.get("sanitized_fields", ()))
    for field in _INDEX_TEXT_FIELDS:
        original = "" if cleaned.get(field) is None else str(cleaned.get(field))
        safe = clean_unicode_text(original)
        if safe != original:
            changed.add(field)
        cleaned[field] = safe
    cleaned["ext"] = clean_unicode_text(cleaned.get("ext", "")).casefold()
    cleaned["sanitized_fields"] = tuple(sorted(changed))
    return cleaned, cleaned["sanitized_fields"]


def _failed_index_record(record: dict[str, Any], exception: BaseException) -> dict[str, Any]:
    """A persistable replacement for one record with invalid data."""

    path_text = require_utf8(record.get("path", ""), label="book path")
    return {
        "path": path_text,
        "ext": clean_unicode_text(record.get("ext", "")).casefold(),
        "size": int(record.get("size") or 0),
        "mtime_ns": int(record.get("mtime_ns") or 0),
        "title": clean_unicode_text(Path(path_text).stem),
        "author": "Unknown author",
        "publisher": "",
        "language": "",
        "subjects": "",
        "description": "",
        "pages": 0,
        "body": "",
        "ok": False,
        "error": clean_unicode_text(
            f"IndexRecordError: {type(exception).__name__}: {exception}"
        )[:400],
        "sanitized_fields": (),
    }


def extractor_main(
    worker_index: int,
    jobs: Any,
    results: Any,
    vitals: Any,
    path_buffer: Any,
    abort: Any,
    priority: str,
) -> None:
    """One extractor, at whatever priority the sweep resolved.  Its own process.

    Lives at module level because Windows spawns workers by re-importing this
    module and looking the target up by name.  It must never raise: an extractor
    that dies leaves its share of the library unindexed and no message behind,
    which is precisely the silent failure this rewrite exists to end.
    """
    taken = apply_process_priority(priority)
    vitals[_V_PID] = os.getpid()
    vitals[_V_STATE] = _STATE_IDLE
    _publish_result(results, ("hello", worker_index, os.getpid(), taken), abort)

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
        vitals[_V_STATE] = _STATE_PUBLISHING
        if not _publish_result(results, ("book", worker_index, record), abort):
            break
        vitals[_V_STATE] = _STATE_IDLE
        _write_current(path_buffer, "")

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

        # Sized against the volume the *books* are on, not the one Lumen is
        # installed on: the sweep's cost is reading them, so a library on an
        # external disk must tune to that disk even when the program runs from
        # an NVMe.
        self._processes = self.config.resolved_processes(self.root)
        self._walker_count = self.config.resolved_walkers(self.root)
        self._priority = self.config.resolved_priority(self.root)
        self._suffixes = self.config.suffix_set()
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
        self._books_extracted = 0
        self._books_indexed = 0
        self._books_failed = 0
        self._books_rejected = 0
        self._bytes_found = 0
        self._bytes_indexed = 0
        self._counts: LibraryCounts | None = None
        self._messages: deque[str] = deque(maxlen=400)
        self._history: deque[float] = deque(maxlen=240)
        self._last_sample = (0.0, 0)
        self._rate = 0.0
        self._byte_rate = 0.0
        self._priority_taken = self._priority

        # ── control ────────────────────────────────────────────────────────
        self._cancel = threading.Event()
        self._fatal = threading.Event()
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
        self._walker_threads: list[threading.Thread] = []
        self._triage_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._sampler_thread: threading.Thread | None = None
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
            f"{self._priority.upper()} priority  ·  {self._walker_count} walker threads"
        )
        for note in self.config.tuning_notes(self.root):
            self._say(f"Machine: {note}")
        unsupported = sorted(self.config.unsupported_suffixes())
        if unsupported:
            self._say(
                "Ignored unsupported format setting(s): " + ", ".join(unsupported)
                + ". Lumen indexes EPUB and PDF only."
            )
        self._say(f"Engine: {self.backend}  —  {self.backend_reason}")
        control = threading.Thread(target=self._control, name="lumen-sweep", daemon=True)
        control.start()
        self._control_thread = control

    def cancel(self) -> None:
        self._request_abort()
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
        return self._phase in {"starting", "sweeping", "finishing", "failing"}

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
                books_extracted=self._books_extracted,
                books_indexed=self._books_indexed,
                books_failed=self._books_failed,
                books_rejected=self._books_rejected,
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
        snapshot.books_extracted = max(
            snapshot.books_extracted,
            sum(worker.done for worker in snapshot.workers),
        )
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

    def _request_abort(self) -> None:
        self._cancel.set()
        self._pause.clear()
        if self._abort is not None:
            self._abort.set()
        with self._dir_lock:
            self._dir_wake.notify_all()

    def _fail_stage(self, stage: str, exception: BaseException | str) -> None:
        """Publish one terminal stage error and release every producer."""

        detail = clean_unicode_text(
            exception if isinstance(exception, str)
            else f"{type(exception).__name__}: {exception}"
        )[:400]
        message = f"{stage}: {detail}" if detail else f"{stage}: unknown failure"
        first = False
        with self._lock:
            if not self._error:
                self._error = message
                first = True
            # Keep the monitor attached while the conductor drains queues and
            # workers.  ``error`` is terminal; publishing it here used to let
            # the UI start a second sweep while the failed one was still alive.
            self._phase = "failing"
        self._fatal.set()
        self._request_abort()
        if first:
            self._say(f"SWEEP FAILED: {message}")

    def _join_stage(self, stage: Any, label: str, normal_timeout: float) -> bool:
        """Join a thread/process with a deadline that reacts to late Stop."""

        normal_deadline = time.monotonic() + max(0.5, normal_timeout)
        abort_deadline: float | None = None
        while stage.is_alive():
            stage.join(timeout=0.1)
            now = time.monotonic()
            if self._cancel.is_set() or self._fatal.is_set():
                if abort_deadline is None:
                    abort_deadline = now + _CANCEL_GRACE
                if now >= abort_deadline:
                    return False
            elif now >= normal_deadline:
                self._fail_stage(label, TimeoutError(f"did not finish within {normal_timeout:.0f}s"))
                return False
        return True

    @staticmethod
    def _put_with_deadline(target: Any, value: Any, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.1, seconds)
        while time.monotonic() < deadline:
            try:
                target.put(value, timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                return True
            except queue.Full:
                continue
            except (EOFError, OSError, ValueError):
                return False
        return False

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        """A connection owned by whichever thread calls this.

        Each stage opens its own handle: WAL gives the triage reader a stable
        snapshot while the writer commits, and never sharing a handle across
        threads removes a whole class of SQLite misuse errors.
        """
        connection = sqlite3.connect(str(self.database), timeout=60.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-131072")
        if not read_only:
            connection.execute("PRAGMA wal_autocheckpoint=2000")
            connection.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES}")
        return connection

    # ── stage 0: the conductor ─────────────────────────────────────────────

    def _control(self) -> None:
        try:
            self._run_pipeline()
        except BaseException as exception:      # the sweep reports, never dies
            self._fail_stage("sweep", exception)
        finally:
            try:
                self._finalise()
            except BaseException as exception:
                self._fail_stage("finalise", exception)
            with self._lock:
                self._finished_at = time.monotonic()
                if self._fatal.is_set() and self._phase == "failing":
                    self._phase = "error"
            self._done.set()

    def _run_pipeline(self) -> None:
        # The schema (and any migration) must exist before a worker touches it.
        with LibraryIndex(self.database, text_budget=self._text_budget) as index:
            index.recover_wal(self._say)
            self._generation = index.next_generation(self.root_key)

        with self._lock:
            self._phase = "sweeping"

        self._start_extractors()
        sampler = threading.Thread(target=self._sample_rates, name="lumen-rates", daemon=True)
        self._sampler_thread = sampler
        sampler.start()

        writer = threading.Thread(
            target=self._thread_stage, args=("writer", self._writer),
            name="lumen-writer", daemon=True,
        )
        triage = threading.Thread(
            target=self._thread_stage, args=("triage", self._triage),
            name="lumen-triage", daemon=True,
        )
        self._writer_thread = writer
        self._triage_thread = triage
        writer.start()
        triage.start()

        walkers = self._start_walkers()
        self._walker_threads = walkers
        for walker in walkers:
            if not self._join_stage(walker, walker.name, _SHUTDOWN_GRACE):
                self._fail_stage(walker.name, "did not stop after cancellation")
                break
        if not self._fatal.is_set():
            with self._lock:
                self._walk_complete = True
                self._walk_finished_after = time.perf_counter() - self._perf_zero
                self._phase = "finishing" if not self._cancel.is_set() else self._phase
            self._say(
                f"Walk complete: {self._dirs_swept:,} directories, "
                f"{self._books_found:,} books, {self._entries_seen:,} entries examined."
            )

        if not self._cancel.is_set():
            if not self._put_with_deadline(self._walk_queue, None):
                self._fail_stage("conductor", "could not close the triage input queue")
        if not self._join_stage(triage, "triage", _SHUTDOWN_GRACE):
            self._fail_stage("triage", "did not stop after cancellation")
        self._stop_extractors()
        self._touch_queue.put(None)             # unbounded lightweight queue
        if not self._join_stage(writer, "writer", _SHUTDOWN_GRACE):
            self._fail_stage("writer", "did not stop after cancellation")
        sampler.join(timeout=1.0)

    def _finalise(self) -> None:
        fatal = self._fatal.is_set()
        cancelled = self._cancel.is_set() and not fatal
        with self._lock:
            self._books_extracted = max(
                self._books_extracted,
                sum(int(vitals[_V_DONE]) for vitals in self._vitals),
            )
            settled = self._books_indexed + self._books_unchanged + self._books_failed
            unaccounted = max(0, self._books_found - settled)
            error = self._error
        partial = bool(unaccounted) and not fatal and not cancelled
        status = (
            "error" if fatal else "cancelled" if cancelled else "partial" if partial else "done"
        )
        with LibraryIndex(self.database, text_budget=self._text_budget) as index:
            if self.config.prune_missing and status == "done":
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
                status=status,
                error=error,
                extracted=self._books_extracted,
                committed=self._books_indexed + self._books_failed,
                rejected=self._books_rejected,
                unaccounted=unaccounted,
            )
        with self._lock:
            self._counts = counts
            self._phase = status
        self._say(
            ("Sweep failed. " if fatal else "Sweep stopped by request. " if cancelled
             else "Sweep incomplete. " if partial else "Sweep complete. ")
            + f"{counts.total:,} books in the index  ·  "
            f"{self._books_indexed:,} committed  ·  {self._books_unchanged:,} already current  ·  "
            f"{self._books_failed:,} unreadable  ·  {self._books_rejected:,} rejected for retry  ·  "
            f"{time.monotonic() - self._started_at:,.1f}s"
        )
        # Every book found must be accounted for one way or another.  Saying so
        # out loud is the difference between a sweep that quietly lost work and
        # one the reader can trust; they are re-read on the next sweep, because
        # nothing was written for them.
        if unaccounted:
            self._say(
                f"WARNING: {unaccounted:,} book(s) were found but not committed. "
                f"No missing-book pruning was performed; the next sweep will retry them."
            )

    # ── stage 1: the walker fleet ──────────────────────────────────────────

    def _thread_stage(self, label: str, target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exception:
            self._fail_stage(label, exception)

    def _start_walkers(self) -> list[threading.Thread]:
        with self._dir_lock:
            self._dir_stack.append((str(self.root), 0))
            self._dir_outstanding = 1
        walkers = []
        for position in range(self._walker_count):
            name = f"lumen-walk-{position}"
            walkers.append(threading.Thread(
                target=self._thread_stage, args=(name, self._walk), name=name, daemon=True,
            ))
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
        job_depth = self.config.resolved_job_queue(self.root)
        self._jobs = queue.Queue(maxsize=job_depth) if self._inline \
            else context.Queue(maxsize=job_depth)
        self._results = queue.Queue() if self._inline \
            else context.Queue(maxsize=self.config.resolved_result_queue(self.root))

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
                      self._paths[position], self._abort, self._priority),
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
        normal_deadline = time.monotonic() + _SHUTDOWN_GRACE
        abort_deadline: float | None = None
        sent = 0
        while sent < self._processes:
            now = time.monotonic()
            if self._cancel.is_set() or self._fatal.is_set():
                if abort_deadline is None:
                    abort_deadline = now + _CANCEL_GRACE
                deadline = abort_deadline
            else:
                deadline = normal_deadline
            if now >= deadline:
                break
            try:
                self._jobs.put(None, timeout=min(0.25, max(0.01, deadline - now)))
                sent += 1
            except queue.Full:
                continue
            except (EOFError, OSError, ValueError):
                break

        for worker in self._worker_processes:
            while worker.is_alive():
                worker.join(timeout=0.1)
                now = time.monotonic()
                if self._cancel.is_set() or self._fatal.is_set():
                    if abort_deadline is None:
                        abort_deadline = now + _CANCEL_GRACE
                    deadline = abort_deadline
                else:
                    deadline = normal_deadline
                if now >= deadline:
                    break
            if worker.is_alive():
                self._say(
                    f"{worker.name} did not stop in time and was terminated; the book "
                    f"it was reading is not indexed and will be picked up next sweep."
                )
                if hasattr(worker, "terminate"):
                    worker.terminate()
                    worker.join(timeout=2.0)

        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            if not self._put_with_deadline(self._results, None, _CANCEL_GRACE):
                self._fail_stage("conductor", "could not close the full result queue")

    # ── stage 4: the writer ────────────────────────────────────────────────

    def _writer(self) -> None:
        """The only stage allowed to write.  Batches everything into one txn.

        It is deliberately the stage that never blocks on a full queue: results
        always drain here, which is what guarantees the pipeline above can never
        wedge itself.
        """
        connection: sqlite3.Connection | None = None
        pending: list[dict[str, Any]] = []
        touches: list[int] = []
        results_open = True
        touches_open = True
        batch_size = max(16, self.config.write_batch)
        last_commit = time.monotonic()

        def flush() -> None:
            nonlocal last_commit
            if not pending and not touches:
                return
            assert connection is not None
            cursor = connection.cursor()
            indexed = failed = rejected = committed_bytes = 0
            diagnostics: list[str] = []
            try:
                if pending:
                    indexed, failed, rejected, committed_bytes, diagnostics = \
                        self._commit_records(cursor, pending)
                if touches:
                    cursor.executemany(
                        "UPDATE books SET seen_gen = ? WHERE id = ?",
                        [(self._generation, book_id) for book_id in touches],
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

            # A book becomes visible in the monitor only after SQLite confirms
            # the transaction.  The old writer incremented these counters on
            # dequeue, so a dead transaction could look like successful work.
            with self._lock:
                self._books_indexed += indexed
                self._books_failed += failed
                self._books_rejected += rejected
                self._bytes_indexed += committed_bytes
            pending.clear()
            touches.clear()
            for diagnostic in diagnostics:
                self._say(diagnostic)
            last_commit = time.monotonic()

        try:
            connection = self._connect()
            self._ensure_fts_map(connection)
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
                                    self._books_extracted += 1
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
        except BaseException:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if connection is not None:
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

    def _commit_records(
        self,
        cursor: sqlite3.Cursor,
        records: Sequence[dict[str, Any]],
    ) -> tuple[int, int, int, int, list[str]]:
        """Write a batch without allowing one malformed book to poison it.

        SQLite rolls a statement back on most errors, but the remainder of the
        transaction is not a safe recovery boundary for the FTS delete/insert
        sequence.  A savepoint per book makes that sequence atomic.  Bad record
        data becomes an indexed failure row; infrastructure errors still escape
        and fail the complete sweep loudly.
        """

        indexed = failed = rejected = committed_bytes = 0
        diagnostics: list[str] = []
        data_errors = (
            UnicodeError,
            ValueError,
            TypeError,
            OverflowError,
            sqlite3.IntegrityError,
            sqlite3.DataError,
        )

        for raw_record in records:
            record: dict[str, Any] | None = None
            changed: tuple[str, ...] = ()
            record_error: BaseException | None = None
            try:
                record, changed = _sanitize_index_record(raw_record)
                self._commit_record_savepoint(cursor, record)
            except data_errors as exception:
                record_error = exception

            if record_error is not None:
                try:
                    fallback = _failed_index_record(raw_record, record_error)
                    self._commit_record_savepoint(cursor, fallback)
                except data_errors as fallback_error:
                    rejected += 1
                    diagnostics.append(
                        "Rejected one unpersistable book record; it will be retried next sweep: "
                        f"{escaped_for_log(raw_record.get('path', '<unknown>'))} — "
                        f"{escaped_for_log(fallback_error)}"
                    )
                    continue
                failed += 1
                committed_bytes += int(fallback.get("size") or 0)
                diagnostics.append(
                    "Stored one malformed book as unreadable instead of stopping the sweep: "
                    f"{escaped_for_log(fallback['path'])} — {escaped_for_log(record_error)}"
                )
                continue

            assert record is not None
            if record.get("ok", True):
                indexed += 1
            else:
                failed += 1
            committed_bytes += int(record.get("size") or 0)
            if changed:
                diagnostics.append(
                    f"Sanitized invalid document text in {escaped_for_log(record['path'])} "
                    f"({', '.join(changed)})."
                )

        return indexed, failed, rejected, committed_bytes, diagnostics

    def _commit_record_savepoint(self, cursor: sqlite3.Cursor, record: dict[str, Any]) -> None:
        cursor.execute("SAVEPOINT lumen_book_record")
        try:
            self._commit_record(cursor, record)
        except BaseException:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT lumen_book_record")
            finally:
                cursor.execute("RELEASE SAVEPOINT lumen_book_record")
            raise
        cursor.execute("RELEASE SAVEPOINT lumen_book_record")

    def _commit_record(self, cursor: sqlite3.Cursor, record: dict[str, Any]) -> None:
        generation = self._generation
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
        # Remember where both rows landed, so the next sweep that replaces this
        # book can find them without reading the index end to end.
        cursor.execute(
            "INSERT INTO fts_rowid (book_id, meta_row, content_row) VALUES (?,?,?)"
            " ON CONFLICT(book_id) DO UPDATE SET meta_row = excluded.meta_row,"
            " content_row = excluded.content_row",
            (book_id, meta_row, content_row),
        )
        # Keep the interactive library sweep on its original fast path.  MCP
        # passages are a second, much heavier index (chunking plus another FTS
        # write) and are built by the resumable ``lumen-mcp index build`` job.
        # Coupling that work to this single writer made every extractor wait on
        # SQLite and made a healthy sweep look frozen between large commits.

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


def describe_fleet(config: ScanConfig, root: Any = None) -> str:
    """One sentence describing exactly what pressing Sweep will launch.

    *root* is the library folder, because the fleet is sized against the volume
    the books are on.  Omitting it describes the fleet for the current working
    volume, which is what the settings window wants before a library is chosen.
    """
    processes = config.resolved_processes(root)
    physical, logical = cpu_topology()
    priority = config.resolved_priority(root).upper()
    per_core = processes / max(1, logical)
    return (
        f"{processes} extractor process{'' if processes == 1 else 'es'} at {priority} priority "
        f"({per_core:.2f} per logical processor; this machine has {physical} cores / "
        f"{logical} logical processors) and {config.resolved_walkers(root)} walker threads."
    )
