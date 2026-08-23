"""Tests for the Turbo Sweep: the concurrent, multi-process library scanner.

The point of these is the *fleet*.  ``test_library_index`` drives the scanner
with one worker, which deliberately takes the in-process path, so nothing there
touches spawning, shared memory, or the bounded queues.  Everything below runs
with a real process fleet, because that is what a reader actually gets.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lumen_reader import accel, turbo_scan
from lumen_reader.library_index import LibraryIndex
from lumen_reader.turbo_scan import (
    MAX_PROCESSES,
    WINDOWS_PRIORITY_CLASSES,
    ScanConfig,
    TurboScanner,
    apply_process_priority,
    cpu_topology,
    current_priority,
    describe_fleet,
    sweep,
)

from test_library_index import make_epub, make_pdf

#: Two is enough to prove the fleet is real without making the suite crawl.
FLEET = 2


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    root = tmp_path / "datalake"
    make_epub(root / "alpha.epub", "Alpha World", "Ada Writer", "hydrology at length")
    make_epub(root / "nested" / "beta.epub", "Beta Days", "Ben Author", "medieval falconry")
    make_epub(root / "nested" / "deep" / "delta.epub", "Delta Deep", "Dee Writer", "abyssal plains")
    make_pdf(root / "gamma.pdf", "Gamma Report", "detonation velocity")
    (root / "notes.txt").write_text("not a book", encoding="utf-8")
    make_epub(root / ".git" / "hidden.epub", "Hidden", "Nobody", "secret")
    return root


def fleet_config(**overrides: object) -> ScanConfig:
    config = ScanConfig(processes=FLEET, priority="above")
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# ─────────────────────────── the fleet does the work ───────────────────────


def test_a_real_process_fleet_indexes_every_book(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config())
    assert final.phase == "done"
    assert final.error == ""
    assert final.books_found == 4
    assert final.books_indexed == 4
    assert final.books_failed == 0
    with LibraryIndex(tmp_path / "i.db") as index:
        assert index.counts(library).total == 4
        assert [row.title for row in index.search(library, "Delta")] == ["Delta Deep"]


def test_every_worker_is_a_separate_ultra_priority_process(library: Path, tmp_path: Path) -> None:
    """The whole point of the rewrite: real OS processes, not a promise of them."""
    for count in range(200):
        make_epub(library / f"bulk-{count}.epub", f"Bulk {count}", "Author", "filler prose")

    scanner = TurboScanner(tmp_path / "i.db", library, fleet_config())
    scanner.start()
    seen_pids: set[int] = set()
    while not scanner.wait(0.05):
        for worker in scanner.snapshot().workers:
            if worker.pid:
                seen_pids.add(worker.pid)

    final = scanner.snapshot()
    assert final.phase == "done"
    assert len(final.workers) == FLEET
    assert len(seen_pids) == FLEET, "each worker must be its own process"
    assert os.getpid() not in seen_pids, "the fleet must not run inside the GUI process"
    assert sum(worker.done for worker in final.workers) == final.books_indexed
    assert all(worker.done > 0 for worker in final.workers), "work must actually be spread"


def test_the_fleet_reads_while_a_slow_share_is_still_being_listed(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a slow share the fleet must start reading long before the walk ends.

    This is the property that makes the scanner usable on a NAS and the one the
    old phase-at-a-time indexer could not have had.  It is only *observable*
    when listing a directory costs something, so the walk is given a realistic
    network round trip per folder - on a local SSD the entire walk finishes
    inside one clock tick and the question is meaningless.

    It also pins the time-bounded triage flush: the shelf here holds far fewer
    books than one 512-book batch, so a size-only flush would hand the fleet
    nothing at all until the walk sentinel arrived, leaving every core idle for
    the length of the walk.
    """
    for count in range(40):
        make_epub(library / f"shelf-{count % 8}/book-{count}.epub", f"B{count}", "A", "prose")

    real_scandir = os.scandir

    def slow_scandir(path):                       # one network round trip per folder
        time.sleep(0.2)
        return real_scandir(path)

    monkeypatch.setattr(turbo_scan.os, "scandir", slow_scandir)
    final = sweep(tmp_path / "i.db", library, fleet_config())

    assert final.books_indexed == 44
    assert final.first_dispatch_after > 0, "no book ever reached the fleet"
    assert final.first_dispatch_after < final.walk_finished_after, (
        f"the fleet sat idle until the walk finished "
        f"(first book dispatched at {final.first_dispatch_after:.3f}s, "
        f"walk ended at {final.walk_finished_after:.3f}s)"
    )


# ──────────────────────────────── incrementality ───────────────────────────


def test_a_second_sweep_reads_nothing_again(library: Path, tmp_path: Path) -> None:
    database = tmp_path / "i.db"
    sweep(database, library, fleet_config())
    second = sweep(database, library, fleet_config())
    assert second.books_found == 4
    assert second.books_unchanged == 4
    assert second.books_indexed == 0


def test_an_edited_book_is_the_only_one_re_read(library: Path, tmp_path: Path) -> None:
    database = tmp_path / "i.db"
    sweep(database, library, fleet_config())
    make_epub(library / "alpha.epub", "Renamed Entirely", "Ada Writer", "new text")
    second = sweep(database, library, fleet_config())
    assert second.books_indexed == 1
    assert second.books_unchanged == 3
    with LibraryIndex(database) as index:
        assert [row.title for row in index.search(library, "Renamed")] == ["Renamed Entirely"]


def test_a_deleted_book_is_pruned_by_generation(library: Path, tmp_path: Path) -> None:
    database = tmp_path / "i.db"
    sweep(database, library, fleet_config())
    (library / "alpha.epub").unlink()
    final = sweep(database, library, fleet_config())
    assert final.counts is not None and final.counts.total == 3
    with LibraryIndex(database) as index:
        assert index.search(library, "Alpha World") == []


def test_pruning_can_be_turned_off(library: Path, tmp_path: Path) -> None:
    database = tmp_path / "i.db"
    sweep(database, library, fleet_config())
    (library / "alpha.epub").unlink()
    final = sweep(database, library, fleet_config(prune_missing=False))
    assert final.counts is not None and final.counts.total == 4


# ──────────────────────────── what the sweep collects ──────────────────────


def test_extensions_limit_what_is_swept(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config(extensions=(".pdf",)))
    assert final.books_found == 1
    with LibraryIndex(tmp_path / "i.db") as index:
        assert index.counts(library).pdf == 1
        assert index.counts(library).epub == 0


def test_depth_limits_the_walk(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config(max_depth=1))
    names = {"alpha.epub", "gamma.pdf", "beta.epub"}   # root plus one level down
    assert final.books_found == len(names)


def test_exclude_globs_are_honoured(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config(exclude_globs=("*nested*",)))
    assert final.books_found == 2


def test_a_minimum_size_filters_stubs(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config(min_bytes=10_000_000))
    assert final.books_found == 0


def test_skipped_directories_are_never_entered(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config())
    with LibraryIndex(tmp_path / "i.db") as index:
        assert index.search(library, "Hidden") == []
    assert final.books_found == 4


def test_text_can_be_switched_off_entirely(library: Path, tmp_path: Path) -> None:
    sweep(tmp_path / "i.db", library, fleet_config(with_text=False))
    with LibraryIndex(tmp_path / "i.db") as index:
        counts = index.counts(library)
        assert counts.total == 4
        assert counts.with_text == 0
        assert index.search(library, "falconry", mode="content") == []
        assert [row.title for row in index.search(library, "Beta")] == ["Beta Days"]


def test_a_corrupt_book_is_recorded_not_fatal(library: Path, tmp_path: Path) -> None:
    (library / "broken.epub").write_bytes(b"definitely not a zip archive")
    final = sweep(tmp_path / "i.db", library, fleet_config())
    assert final.books_failed == 1
    assert final.books_indexed == 4
    assert final.phase == "done"


def test_every_book_found_is_accounted_for(library: Path, tmp_path: Path) -> None:
    """No book may be found and then quietly dropped.

    A completed sweep used to be able to lose work: the fleet was given twenty
    seconds to shut down, and whichever workers happened to be part-way through
    a very large PDF were terminated mid-book.  On a real 9,335-book shelf that
    left 26 books out of the index while the sweep reported success.
    """
    for count in range(120):
        make_epub(library / f"bulk-{count}.epub", f"Bulk {count}", "Author", "prose " * 400)
    final = sweep(tmp_path / "i.db", library, fleet_config())
    settled = final.books_indexed + final.books_unchanged + final.books_failed
    assert settled == final.books_found == 124
    with LibraryIndex(tmp_path / "i.db") as index:
        assert index.counts(library).total == 124


# ────────────────────────────────── control ────────────────────────────────


def test_cancelling_stops_the_sweep_and_leaves_the_index_usable(
    library: Path, tmp_path: Path
) -> None:
    for count in range(400):
        make_epub(library / f"bulk-{count}.epub", f"Bulk {count}", "Author", "filler " * 200)

    scanner = TurboScanner(tmp_path / "i.db", library, fleet_config())
    scanner.start()
    while not scanner.wait(0.02):
        if scanner.snapshot().books_indexed > 5:
            scanner.cancel()
            break
    assert scanner.wait(120), "a cancelled sweep must still shut its fleet down"
    final = scanner.snapshot()
    assert final.phase == "cancelled"
    with LibraryIndex(tmp_path / "i.db") as index:
        assert index.counts(library).total > 0     # what was read is still searchable


def test_a_scanner_refuses_a_second_sweep(library: Path, tmp_path: Path) -> None:
    scanner = TurboScanner(tmp_path / "i.db", library, fleet_config())
    scanner.start()
    with pytest.raises(RuntimeError):
        scanner.start()
    scanner.wait(120)


def test_the_run_is_recorded_for_the_settings_window(library: Path, tmp_path: Path) -> None:
    sweep(tmp_path / "i.db", library, fleet_config())
    with LibraryIndex(tmp_path / "i.db") as index:
        run = index.last_scan(library)
    assert run is not None
    assert run["indexed"] == 4
    assert run["cancelled"] == 0
    assert run["seconds"] >= 0


# ─────────────────────────────── configuration ─────────────────────────────


def test_the_default_fleet_is_one_process_per_logical_processor() -> None:
    config = ScanConfig()
    assert config.resolved_processes() == min(MAX_PROCESSES, os.cpu_count() or 4)


def test_the_fleet_never_exceeds_the_windows_handle_limit() -> None:
    assert ScanConfig(processes=5000).resolved_processes() == MAX_PROCESSES


def test_config_round_trips_through_stored_state() -> None:
    original = ScanConfig(
        processes=9, priority="realtime", walkers=17, max_depth=3,
        exclude_globs=("*tmp*",), with_text=False, text_budget=1234, pdf_page_cap=7,
    )
    restored = ScanConfig.from_mapping(original.to_dict())
    assert restored == original


def test_a_corrupt_settings_file_falls_back_to_defaults() -> None:
    restored = ScanConfig.from_mapping(
        {"processes": "not a number", "priority": "supersonic", "extensions": [], "walkers": None}
    )
    assert restored.processes == 0
    assert restored.priority == "high"
    assert restored.extensions == ScanConfig().extensions


def test_config_from_nonsense_is_still_a_config() -> None:
    assert ScanConfig.from_mapping(None) == ScanConfig()
    assert ScanConfig.from_mapping("nonsense") == ScanConfig()


def test_priority_names_are_the_ones_windows_understands() -> None:
    for name in ("idle", "below", "normal", "above", "high", "realtime"):
        assert name in WINDOWS_PRIORITY_CLASSES


@pytest.mark.skipif(sys.platform != "win32", reason="priority classes are a Windows idea")
def test_the_priority_is_really_applied_not_merely_claimed() -> None:
    """Raising the process must actually change it, and be read back from Windows.

    This is here because it silently did not.  ``GetCurrentProcess`` returns the
    pseudo-handle -1, and without declared ctypes signatures it was passed to
    ``SetPriorityClass`` as 0x00000000FFFFFFFF instead of all-ones - an invalid
    handle.  The call failed, the code reported success, and a fleet advertised
    as HIGH ran the whole sweep at Normal.  Task Manager was the only way to
    know.  ``above`` is used rather than ``high`` because it needs no privilege.
    """
    before = current_priority()
    try:
        assert apply_process_priority("above") == "above"
        assert current_priority() == "above"
    finally:
        apply_process_priority(before if before in WINDOWS_PRIORITY_CLASSES else "normal")
    assert current_priority() == before


@pytest.mark.skipif(sys.platform != "win32", reason="priority classes are a Windows idea")
def test_a_refused_priority_steps_down_never_up() -> None:
    before = current_priority()
    try:
        assert apply_process_priority("nonsense") in WINDOWS_PRIORITY_CLASSES
    finally:
        apply_process_priority(before if before in WINDOWS_PRIORITY_CLASSES else "normal")


def test_the_fleet_reports_the_priority_it_actually_got(library: Path, tmp_path: Path) -> None:
    """Whatever the monitor shows has to have come back from the workers."""
    final = sweep(tmp_path / "i.db", library, fleet_config(priority="above"))
    assert final.priority in WINDOWS_PRIORITY_CLASSES
    if sys.platform == "win32":
        assert final.priority == "above"


def test_the_machine_reports_at_least_one_core() -> None:
    physical, logical = cpu_topology()
    assert physical >= 1 and logical >= 1


def test_the_fleet_describes_itself_before_it_runs() -> None:
    description = describe_fleet(ScanConfig(processes=4, priority="high"))
    assert "4 extractor processes" in description
    assert "HIGH" in description


# ───────────────────── one build, GPU or no GPU ────────────────────────────


def test_the_sweep_runs_on_whatever_this_machine_has(library: Path, tmp_path: Path) -> None:
    """`auto` must complete on any machine, GPU or not, and say which engine ran.

    This is the "one version everywhere" guarantee: the same configuration is
    handed to the scanner regardless of hardware, and it resolves itself.
    """
    final = sweep(tmp_path / "i.db", library, fleet_config(extraction_backend="auto"))
    assert final.phase == "done"
    assert final.books_indexed == 4
    assert final.backend in {accel.CPU_FLEET, accel.GPU_DIRECTSTORAGE}
    assert final.backend_reason, "the sweep must say why it chose that engine"


def test_forcing_a_missing_gpu_falls_back_rather_than_failing(
    library: Path, tmp_path: Path
) -> None:
    """Asking for a GPU that is not there must degrade, loudly, never crash."""
    final = sweep(
        tmp_path / "i.db", library, fleet_config(extraction_backend=accel.GPU_DIRECTSTORAGE)
    )
    assert final.phase == "done"
    assert final.books_indexed == 4
    if not accel.extraction_backend_status(accel.GPU_DIRECTSTORAGE)[0]:
        assert final.backend == accel.CPU_FLEET
        assert "Falling back" in final.backend_reason


def test_an_unknown_backend_name_still_sweeps(library: Path, tmp_path: Path) -> None:
    final = sweep(tmp_path / "i.db", library, fleet_config(extraction_backend="quantum-flux"))
    assert final.phase == "done"
    assert final.backend == accel.CPU_FLEET


def test_the_backend_choice_survives_a_settings_round_trip() -> None:
    original = ScanConfig(extraction_backend=accel.GPU_DIRECTSTORAGE)
    assert ScanConfig.from_mapping(original.to_dict()).extraction_backend == (
        accel.GPU_DIRECTSTORAGE
    )
    assert ScanConfig.from_mapping({}).extraction_backend == "auto"
