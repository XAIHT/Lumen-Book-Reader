# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""Acceleration seams: where a GPU and DirectStorage plug in later.

Angela's requirement is that Lumen must not have to be rewritten the day the
super-NAS and an RTX card arrive.  That is an architecture problem, and it is
solved now, before the hardware exists, by making the two stages that would move
to the GPU into *replaceable backends* rather than inlined code:

* **Extraction** - turning a book on disk into rows.  Today a fleet of CPU
  processes.  Tomorrow, potentially, DirectStorage streaming NVMe pages straight
  into VRAM with GPU-side decompression, so the CPU never touches the bytes.

* **Search** - turning a query into ranked book ids.  Today SQLite FTS5.
  Tomorrow, potentially, a resident GPU index doing brute-force scoring across
  the whole corpus in one kernel launch.

Both are declared here as protocols with an explicit contract, both have a
working CPU implementation, and both have a named GPU implementation that
currently reports itself unavailable *with a reason*.  Nothing above this module
knows which backend it is talking to, so switching one on is a registration, not
a refactor.

──────────────────────────────────────────────────────────────────────────
    An honest word about the target size
──────────────────────────────────────────────────────────────────────────
10 × 10^128 bytes cannot be stored.  The observable universe holds on the order
of 10^80 atoms, so a corpus of 10^129 bytes is not an engineering problem, it is
a physical impossibility - no architecture reaches it, including this one.

What this design *does* reach is set out in :func:`capacity_report`, and it is
not modest.  The index is shardable, every shard is independent, and nothing in
the pipeline holds the corpus in memory, so the ceiling is the sum of the
devices you can attach rather than anything Lumen imposes.  A thousand shards of
a billion books each is 10^12 books - and at a realistic 2 MB per book that is
about 2 × 10^18 bytes, two exabytes, which is a real datacentre and a real
number.  Everything here is built to scale until the hardware, not the software,
says stop.

This module must never import Qt: it is read by the worker processes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

#: What one book's row costs in the index once its metadata and FTS postings are
#: written.  Measured, not guessed: a real 27,956-book index came to 7.79 GB at
#: a 250,000-character text budget, which is 292 KB a book - and the formula in
#: :func:`index_bytes_per_book` lands on 289 KB for the same inputs.
INDEX_BYTES_METADATA = 1_200

#: FTS5 postings run a little larger than the text they cover.
FTS_OVERHEAD = 1.15

#: How much index one SQLite file should be asked to carry.  SQLite itself
#: allows 2^63-1 bytes, but FTS5 segment merges stop being pleasant long before
#: that; past this the answer is more shards, not a bigger file.
PRACTICAL_SHARD_BYTES = 2 * 1024 ** 4          # 2 TB
PRACTICAL_ROWS_PER_SHARD = 250_000_000


# ────────────────────────────── what is present ────────────────────────────


@dataclass(slots=True)
class Accelerator:
    """One piece of hardware or runtime the sweep could use."""

    kind: str                 # "cpu" | "gpu" | "directstorage" | "storage"
    name: str
    available: bool
    detail: str = ""
    memory_bytes: int = 0

    @property
    def badge(self) -> str:
        return "READY" if self.available else "ABSENT"


def _run(command: Sequence[str], timeout: float = 4.0) -> str:
    """Run a probe command, returning "" for anything that goes wrong.

    Hardware detection must never be able to stop Lumen from starting, so every
    failure mode - missing binary, timeout, permission, garbage output - is the
    same answer: we could not tell, so assume absent.
    """
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def detect_cpu() -> Accelerator:
    from .turbo_scan import cpu_topology

    physical, logical = cpu_topology()
    return Accelerator(
        kind="cpu",
        name=f"{physical} cores / {logical} logical processors",
        available=True,
        detail=f"Extraction fleet: up to {logical} ultra-priority processes, one per processor.",
    )


def detect_gpus() -> list[Accelerator]:
    """Every NVIDIA GPU ``nvidia-smi`` will admit to.

    Deliberately shells out rather than importing a CUDA binding: Lumen must
    install and run on a machine with no GPU stack at all, and a probe that
    needs a dependency to say "no GPU" is a probe that breaks the reader.
    """
    if shutil.which("nvidia-smi") is None:
        return [Accelerator(
            kind="gpu", name="No NVIDIA GPU detected", available=False,
            detail="nvidia-smi is not on PATH. Install an RTX card and its driver to light this up.",
        )]
    output = _run(["nvidia-smi",
                   "--query-gpu=name,memory.total,compute_cap,driver_version",
                   "--format=csv,noheader,nounits"])
    accelerators: list[Accelerator] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            memory_mb = int(float(parts[1]))
        except ValueError:
            memory_mb = 0
        capability = parts[2] if len(parts) > 2 else "?"
        driver = parts[3] if len(parts) > 3 else "?"
        accelerators.append(Accelerator(
            kind="gpu", name=parts[0], available=True,
            memory_bytes=memory_mb * 1024 * 1024,
            detail=f"{memory_mb:,} MB VRAM · compute {capability} · driver {driver}",
        ))
    if not accelerators:
        accelerators.append(Accelerator(
            kind="gpu", name="NVIDIA driver present, no GPU reported", available=False,
            detail="nvidia-smi ran but listed no device.",
        ))
    return accelerators


def detect_directstorage() -> Accelerator:
    """Whether the DirectStorage runtime could be loaded on this machine.

    DirectStorage needs the redistributable DLLs beside the executable (or on
    the search path), Windows 10 1909+ for the API and Windows 11 for GPU
    decompression, and NVMe underneath to be worth anything at all.
    """
    if sys.platform != "win32":
        return Accelerator(
            kind="directstorage", name="DirectStorage", available=False,
            detail="Windows only. This machine is not running Windows.",
        )
    names = ("dstorage.dll", "dstoragecore.dll")
    here = Path(__file__).resolve().parent
    search = [here, here / "bin", Path(sys.executable).parent, *(
        Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part
    )]
    found = [name for name in names
             if any((folder / name).is_file() for folder in search if folder)]
    if len(found) == len(names):
        return Accelerator(
            kind="directstorage", name="DirectStorage runtime", available=True,
            detail="dstorage.dll and dstoragecore.dll are loadable.",
        )
    missing = ", ".join(name for name in names if name not in found)
    return Accelerator(
        kind="directstorage", name="DirectStorage runtime", available=False,
        detail=f"Not installed ({missing} missing). Ship the DirectStorage "
               f"redistributable beside Lumen to enable NVMe→VRAM streaming.",
    )


def detect_storage() -> Accelerator:
    """Whether the machine has NVMe, which is what DirectStorage is for."""
    if sys.platform != "win32":
        return Accelerator(kind="storage", name="Storage bus", available=False,
                           detail="Bus detection is implemented for Windows only.")
    output = _run(["powershell", "-NoProfile", "-Command",
                   "(Get-PhysicalDisk | Select-Object -ExpandProperty BusType) -join ','"], timeout=8.0)
    buses = {part.strip() for part in output.replace("\n", ",").split(",") if part.strip()}
    if "NVMe" in buses:
        return Accelerator(kind="storage", name="NVMe present", available=True,
                           detail=f"Buses seen: {', '.join(sorted(buses))}.")
    return Accelerator(
        kind="storage", name="No NVMe detected", available=False,
        detail=f"Buses seen: {', '.join(sorted(buses)) or 'unknown'}. "
               f"DirectStorage gives little on SATA and nothing over SMB.",
    )


def detect_accelerators() -> list[Accelerator]:
    """Everything relevant, in the order the settings window shows it."""
    return [detect_cpu(), *detect_gpus(), detect_directstorage(), detect_storage()]


# ──────────────────────── probing, exactly once, off the UI ─────────────────
#
# Detection shells out to ``nvidia-smi`` and to PowerShell for the storage bus.
# That costs seconds, and a settings window that froze for seconds every time a
# combo box changed would be its own bug.  So the probe runs once, in the
# background, and every reader of it gets the cached answer - including on a
# machine with no GPU at all, where the answer is simply "none" and nothing
# anywhere has to care.

_probe_lock = threading.Lock()
_probe_cache: dict[str, Any] = {}
_probe_thread: threading.Thread | None = None


def _probe(key: str, producer) -> Any:
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    value = producer()
    with _probe_lock:
        _probe_cache.setdefault(key, value)
        return _probe_cache[key]


def gpus() -> list[Accelerator]:
    return _probe("gpus", detect_gpus)


def directstorage() -> Accelerator:
    return _probe("directstorage", detect_directstorage)


def storage() -> Accelerator:
    return _probe("storage", detect_storage)


def accelerators() -> list[Accelerator]:
    """The cached view of this machine, safe to call from the UI thread."""
    return [detect_cpu(), *gpus(), directstorage(), storage()]


def probed() -> bool:
    """Whether the hardware answer is already known."""
    with _probe_lock:
        return {"gpus", "directstorage", "storage"} <= _probe_cache.keys()


def has_gpu() -> bool:
    return any(accelerator.available for accelerator in gpus())


def start_background_probe() -> None:
    """Warm the hardware answer at startup so nothing ever waits on it.

    Called once from application start.  It is a daemon thread doing read-only
    detection, so a machine that has no GPU stack pays nothing beyond one failed
    ``shutil.which`` and the reader never notices it happened.
    """
    global _probe_thread
    with _probe_lock:
        if _probe_thread is not None:
            return
        _probe_thread = threading.Thread(
            target=lambda: (gpus(), directstorage(), storage()),
            name="lumen-hardware-probe", daemon=True,
        )
    _probe_thread.start()


def refresh_probes() -> None:
    """Forget the cached answer, so the next question re-detects the machine."""
    global _probe_thread
    with _probe_lock:
        _probe_cache.clear()
        _probe_thread = None


# ─────────────────────────────── the contracts ─────────────────────────────


@runtime_checkable
class ExtractionBackend(Protocol):
    """Turns books on disk into index records.

    A replacement backend must honour four things, all of which the pipeline
    above it already relies on:

    1. ``submit`` may block.  Back-pressure is how the scanner keeps memory flat
       on a corpus larger than RAM; a backend that queues without bound breaks
       that guarantee for everything upstream.
    2. Results may arrive in any order, and every submitted job must produce
       exactly one result - a failure is a record with ``ok=False``, never a
       dropped job and never a raised exception.
    3. ``vitals`` must stay truthful while work is in flight, because it is the
       only thing the live monitor reads.
    4. ``stop`` must be safe to call twice and must not lose committed results.

    A DirectStorage backend fits this shape without straining it: ``submit``
    becomes an enqueue onto a DStorage queue, the batch size becomes the queue
    depth, and results are drained on fence completion.
    """

    name: str

    def available(self) -> tuple[bool, str]: ...
    def start(self, workers: int, priority: str) -> None: ...
    def submit(self, job: Sequence[Any]) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class SearchBackend(Protocol):
    """Turns a query into ranked book ids.

    This is the seam that actually matters for a GPU.  Parsing a book is branchy
    work that a GPU is bad at; scoring a query against a hundred million
    resident postings is exactly what it is good at.  The contract is
    deliberately narrow - ids and scores, no rows - so a GPU backend never has
    to know what a book record looks like:

        match(query, limit, offset) -> list[(book_id, score)]

    The row bodies are then fetched from whichever shard owns each id, which is
    an indexed primary-key lookup and stays on the CPU where it belongs.
    """

    name: str

    def available(self) -> tuple[bool, str]: ...
    def match(self, query: str, *, limit: int, offset: int) -> list[tuple[int, float]]: ...


# ─────────────────────────── the backends we have ──────────────────────────


@dataclass(slots=True)
class BackendChoice:
    """What the scanner will actually use, and why."""

    extraction: str
    search: str
    reason: str
    fallback_from: str = ""


#: The default, and the answer to "one build for every machine": ask the
#: hardware at run time and take the best thing that is actually there.
AUTO = "auto"

CPU_FLEET = "cpu-fleet"
GPU_DIRECTSTORAGE = "gpu-directstorage"
FTS5 = "sqlite-fts5"
GPU_RESIDENT = "gpu-resident"

EXTRACTION_BACKENDS: dict[str, str] = {
    AUTO: "Automatic  —  use the GPU when this machine has one, the CPU when it does not",
    CPU_FLEET: "CPU process fleet  —  one ultra-priority process per core (always available)",
    GPU_DIRECTSTORAGE: "Force GPU + DirectStorage  —  NVMe streamed straight into VRAM",
}

SEARCH_BACKENDS: dict[str, str] = {
    AUTO: "Automatic  —  use the GPU index when this machine has one",
    FTS5: "SQLite FTS5  —  indexed lookup, sharded by library (always available)",
    GPU_RESIDENT: "Force GPU-resident index  —  whole corpus scored in one launch",
}

#: What each backend needs, so "why not" is derived from one list rather than
#: written out twice and allowed to drift.
_EXTRACTION_REQUIREMENTS = (
    (lambda: has_gpu(), "no CUDA-capable GPU"),
    (lambda: directstorage().available, "DirectStorage runtime not installed"),
    (lambda: storage().available, "no NVMe device"),
)


# ─────────────────────── where a GPU kernel plugs in ───────────────────────
#
# Hardware being present is necessary but not sufficient: something has to
# actually implement the kernel.  Keeping that as a registry rather than a
# hard-coded "not yet" is what makes the seam real - shipping a GPU backend is
# one ``register_extraction_backend`` call, with nothing above this module
# changed and no second build of Lumen.  Until then the registry is empty, every
# ``auto`` resolves to the CPU fleet, and nobody is told otherwise.

_extraction_implementations: dict[str, Any] = {}
_search_implementations: dict[str, Any] = {}


def register_extraction_backend(name: str, factory: Any) -> None:
    """Make an :class:`ExtractionBackend` available under *name*."""
    _extraction_implementations[name] = factory


def register_search_backend(name: str, factory: Any) -> None:
    """Make a :class:`SearchBackend` available under *name*."""
    _search_implementations[name] = factory


def extraction_kernel_ready(name: str) -> bool:
    return name in _extraction_implementations


def search_kernel_ready(name: str) -> bool:
    return name in _search_implementations


def extraction_backend_status(name: str) -> tuple[bool, str]:
    """Can this extraction backend run here, and if not, exactly why not."""
    if name in (CPU_FLEET, AUTO):
        return True, "Available."
    if name == GPU_DIRECTSTORAGE:
        missing = [why for test, why in _EXTRACTION_REQUIREMENTS if not test()]
        if missing:
            return False, "Not on this machine: " + "; ".join(missing) + "."
        if not extraction_kernel_ready(name):
            return False, (
                "Hardware is ready, but no GPU extraction kernel is registered in "
                "this build. Registering one switches Lumen over — see "
                "accel.register_extraction_backend."
            )
        return True, "Available: GPU extraction kernel registered and hardware present."
    return False, f"Unknown backend {name!r}."


def search_backend_status(name: str) -> tuple[bool, str]:
    if name in (FTS5, AUTO):
        return True, "Available."
    if name == GPU_RESIDENT:
        present = [accelerator for accelerator in gpus() if accelerator.available]
        if not present:
            return False, "Not on this machine: no CUDA-capable GPU detected."
        vram = max(accelerator.memory_bytes for accelerator in present)
        if not search_kernel_ready(name):
            return False, (
                f"Hardware is ready ({vram / 1024 ** 3:,.0f} GB VRAM), but no "
                f"resident index kernel is registered in this build. Registering "
                f"one switches Lumen over — see accel.register_search_backend."
            )
        return True, f"Available: GPU index registered, {vram / 1024 ** 3:,.0f} GB VRAM."
    return False, f"Unknown backend {name!r}."


def resolve_extraction_backend(preference: str = AUTO) -> tuple[str, str]:
    """The extraction backend that will actually run, and the reason it won.

    ``auto`` is the whole point: the same build runs everywhere, choosing the
    GPU path on a machine that has one and the CPU fleet on a machine that does
    not, with no second version and nothing for the reader to configure.
    """
    if preference == AUTO or not preference:
        if extraction_backend_status(GPU_DIRECTSTORAGE)[0]:
            return GPU_DIRECTSTORAGE, "Automatic: this machine has the GPU path, so it is used."
        if not probed():
            return CPU_FLEET, ("Automatic: still detecting the hardware, so the CPU fleet "
                               "runs this sweep. It is always available.")
        return CPU_FLEET, ("Automatic: no GPU extraction path on this machine, so the "
                           "CPU fleet runs — which is the fast path here anyway.")
    ok, why = extraction_backend_status(preference)
    if ok:
        return preference, why
    return CPU_FLEET, f"Falling back to the CPU fleet — {why}"


def resolve_search_backend(preference: str = AUTO) -> tuple[str, str]:
    if preference == AUTO or not preference:
        if search_backend_status(GPU_RESIDENT)[0]:
            return GPU_RESIDENT, "Automatic: this machine has the GPU index, so it is used."
        return FTS5, "Automatic: no GPU index on this machine, so SQLite FTS5 answers queries."
    ok, why = search_backend_status(preference)
    if ok:
        return preference, why
    return FTS5, f"Falling back to SQLite FTS5 — {why}"


def choose_backends(preferred_extraction: str = AUTO, preferred_search: str = AUTO) -> BackendChoice:
    """Resolve both stages, and say out loud what was chosen and why.

    Silently falling back is how a reader ends up believing the GPU is doing
    something it is not, so a downgrade is always reported through
    ``fallback_from`` rather than hidden.
    """
    extraction, extraction_why = resolve_extraction_backend(preferred_extraction)
    search, search_why = resolve_search_backend(preferred_search)
    fell_back = ""
    if preferred_extraction not in (AUTO, "", extraction):
        fell_back = preferred_extraction
    elif preferred_search not in (AUTO, "", search):
        fell_back = preferred_search
    return BackendChoice(
        extraction=extraction,
        search=search,
        reason=f"{extraction_why}  {search_why}",
        fallback_from=fell_back,
    )


# ──────────────────────────────── sharding ─────────────────────────────────
#
# One SQLite file is the right answer for one reader's shelf and the wrong
# answer for a datacentre.  The index is therefore addressed through a shard
# function from the start, so growing past one file is a configuration change
# rather than a migration: shard 0 of 1 is exactly today's single database.


def shard_for(key: str, shards: int) -> int:
    """Which shard owns *key*.  Stable across runs, platforms and Python builds.

    ``hash()`` is salted per process and would scatter the same book across
    different shards on every launch, so this uses an explicit FNV-1a instead.
    """
    if shards <= 1:
        return 0
    digest = 0xCBF29CE484222325
    for byte in key.encode("utf-8", "surrogatepass"):
        digest = ((digest ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return digest % shards


def shard_path(base: str | Path, shard: int, shards: int) -> Path:
    """The file backing one shard.  Shard 0 of 1 keeps the plain filename."""
    base = Path(base)
    if shards <= 1:
        return base
    return base.with_name(f"{base.stem}.{shard:04d}of{shards:04d}{base.suffix}")


def index_bytes_per_book(text_budget: int = 250_000, with_text: bool = True) -> int:
    """What one book costs in the index at a given text budget.

    Checked against a real index rather than asserted: 27,956 books at a
    250,000-character budget occupied 7.79 GB, which is 292 KB a book; this
    returns 289 KB for the same inputs.  Turning the text off drops it by more
    than two orders of magnitude, which is why the setting matters so much on a
    very large shelf.
    """
    if not with_text:
        return INDEX_BYTES_METADATA
    return INDEX_BYTES_METADATA + int(max(0, text_budget) * FTS_OVERHEAD)


@dataclass(slots=True)
class CapacityReport:
    shards: int
    books_per_shard: int
    books_total: int
    index_bytes: int
    corpus_bytes: int
    bytes_per_book: int
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.shards:,} shard{'' if self.shards == 1 else 's'} × "
            f"{self.books_per_shard:,} books = {self.books_total:,} books  "
            f"({_si(self.index_bytes)} of index at {_si(self.bytes_per_book)} a book, "
            f"cataloguing about {_si(self.corpus_bytes)} of books)."
        )


def _si(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"):
        if abs(value) < 1024 or unit == "YB":
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} YB"


def capacity_report(
    shards: int = 1,
    corpus_bytes_per_book: int = 2_000_000,
    text_budget: int = 250_000,
    with_text: bool = True,
) -> CapacityReport:
    """What this design actually reaches at a given shard count.

    Every number here is derived, so the settings window states a measured
    capacity rather than a marketing one.  A shard is capped by whichever runs
    out first: the rows FTS5 stays pleasant with, or the bytes one file should
    be asked to carry.
    """
    shards = max(1, int(shards))
    per_book = index_bytes_per_book(text_budget, with_text)
    per_shard = min(PRACTICAL_ROWS_PER_SHARD, max(1, PRACTICAL_SHARD_BYTES // per_book))
    total = shards * per_shard
    report = CapacityReport(
        shards=shards,
        books_per_shard=per_shard,
        books_total=total,
        index_bytes=total * per_book,
        corpus_bytes=total * max(1, corpus_bytes_per_book),
        bytes_per_book=per_book,
    )
    report.notes.append(
        "Every shard is an independent file with its own FTS5 index: they can "
        "sit on different disks, different NAS volumes, or different machines, "
        "and they are swept and searched in parallel."
    )
    report.notes.append(
        "Nothing in the pipeline is proportional to corpus size — the walk "
        "streams, triage works in fixed batches, and the writer commits in "
        "fixed batches — so memory is set by queue depth, not by the library."
    )
    if with_text:
        report.notes.append(
            "Most of that index is the text inside the books. Turning full-text "
            "indexing off shrinks it by more than two hundred times, at the cost "
            "of being able to search inside books."
        )
    return report
