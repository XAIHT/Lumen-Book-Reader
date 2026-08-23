"""Tests for the acceleration seams: one build, GPU or no GPU.

Angela's requirement is that Lumen must never need two versions - the same code
has to run on a workstation with an RTX card and on a laptop with no GPU stack
at all, choosing for itself.  These tests pin that both ways round by faking the
hardware, so the GPU path is exercised on a machine that has no GPU and the
CPU path is exercised on a machine that does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lumen_reader import accel
from lumen_reader.accel import (
    AUTO,
    CPU_FLEET,
    FTS5,
    GPU_DIRECTSTORAGE,
    GPU_RESIDENT,
    Accelerator,
    capacity_report,
    choose_backends,
    extraction_backend_status,
    index_bytes_per_book,
    resolve_extraction_backend,
    resolve_search_backend,
    search_backend_status,
    shard_for,
    shard_path,
)


@pytest.fixture(autouse=True)
def clean_probe_cache():
    """Every test starts from an unprobed machine and leaves one behind."""
    accel.refresh_probes()
    yield
    accel.refresh_probes()


def pretend(monkeypatch: pytest.MonkeyPatch, *, gpu: bool, dstorage: bool, nvme: bool) -> None:
    """Run the rest of the test on a machine of our choosing.

    Replaces the detectors rather than the cache they fill, so the caching path
    is exercised too and the fake machine reaches the code under test by exactly
    the route a real one would.
    """
    graphics = ([Accelerator("gpu", "GeForce RTX 5090", True, "32,768 MB VRAM",
                             32768 * 1024 * 1024)] if gpu else
                [Accelerator("gpu", "No NVIDIA GPU detected", False, "nvidia-smi absent")])
    monkeypatch.setattr(accel, "detect_gpus", lambda: graphics)
    monkeypatch.setattr(accel, "detect_directstorage",
                        lambda: Accelerator("directstorage", "DirectStorage runtime", dstorage))
    monkeypatch.setattr(accel, "detect_storage",
                        lambda: Accelerator("storage", "NVMe" if nvme else "No NVMe", nvme))
    accel.gpus()                 # fill the cache from the fake machine
    accel.directstorage()
    accel.storage()


# ─────────────────────────── a machine with no GPU ─────────────────────────


def test_auto_uses_the_cpu_fleet_on_a_machine_without_a_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    backend, reason = resolve_extraction_backend(AUTO)
    assert backend == CPU_FLEET
    assert "no GPU" in reason or "CPU fleet" in reason


def test_auto_search_uses_fts5_without_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    assert resolve_search_backend(AUTO)[0] == FTS5


def test_forcing_the_gpu_without_one_degrades_to_the_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    backend, reason = resolve_extraction_backend(GPU_DIRECTSTORAGE)
    assert backend == CPU_FLEET
    assert "Falling back" in reason


def test_the_reason_names_every_missing_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal has to be actionable, not just 'unavailable'."""
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    ok, why = extraction_backend_status(GPU_DIRECTSTORAGE)
    assert not ok
    assert "no CUDA-capable GPU" in why
    assert "DirectStorage runtime not installed" in why
    assert "no NVMe device" in why


def test_the_cpu_fleet_is_always_available(monkeypatch: pytest.MonkeyPatch) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    assert extraction_backend_status(CPU_FLEET)[0]
    assert search_backend_status(FTS5)[0]


# ──────────────────────────── a machine with a GPU ─────────────────────────


def test_hardware_alone_is_not_enough_without_a_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully equipped machine with no kernel registered must still say no.

    This is the honest half of the contract: the RTX card being present is not
    a reason to claim the GPU is reading books when nothing implements it.
    """
    pretend(monkeypatch, gpu=True, dstorage=True, nvme=True)
    ok, why = extraction_backend_status(GPU_DIRECTSTORAGE)
    assert not ok
    assert "no GPU extraction kernel is registered" in why
    assert resolve_extraction_backend(AUTO)[0] == CPU_FLEET


def test_registering_a_kernel_switches_the_whole_application_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam must actually be wired, not merely documented.

    Registering a backend is the single change that shipping a GPU path needs -
    no second build, no edits above this module.  This proves that by doing it.
    """
    pretend(monkeypatch, gpu=True, dstorage=True, nvme=True)
    monkeypatch.setitem(accel._extraction_implementations, GPU_DIRECTSTORAGE, object)
    monkeypatch.setitem(accel._search_implementations, GPU_RESIDENT, object)

    assert extraction_backend_status(GPU_DIRECTSTORAGE)[0]
    backend, reason = resolve_extraction_backend(AUTO)
    assert backend == GPU_DIRECTSTORAGE
    assert "GPU" in reason
    assert resolve_search_backend(AUTO)[0] == GPU_RESIDENT
    assert choose_backends(AUTO, AUTO).fallback_from == ""


def test_the_same_auto_setting_lands_on_the_cpu_when_the_gpu_goes_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One configuration, two machines. Nothing for the reader to change."""
    monkeypatch.setitem(accel._extraction_implementations, GPU_DIRECTSTORAGE, object)

    pretend(monkeypatch, gpu=True, dstorage=True, nvme=True)
    assert resolve_extraction_backend(AUTO)[0] == GPU_DIRECTSTORAGE

    accel.refresh_probes()
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    assert resolve_extraction_backend(AUTO)[0] == CPU_FLEET


def test_a_gpu_without_directstorage_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, gpu=True, dstorage=False, nvme=True)
    assert resolve_extraction_backend(AUTO)[0] == CPU_FLEET
    assert "DirectStorage" in extraction_backend_status(GPU_DIRECTSTORAGE)[1]


def test_a_gpu_search_backend_reports_its_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    pretend(monkeypatch, gpu=True, dstorage=True, nvme=True)
    ok, why = search_backend_status(GPU_RESIDENT)
    assert not ok            # no kernel registered yet, and it says so
    assert "32 GB VRAM" in why
    assert "register_search_backend" in why


def test_choose_backends_reports_a_downgrade_rather_than_hiding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    choice = choose_backends(GPU_DIRECTSTORAGE, FTS5)
    assert choice.extraction == CPU_FLEET
    assert choice.fallback_from == GPU_DIRECTSTORAGE
    assert choice.reason


def test_auto_is_never_reported_as_a_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    pretend(monkeypatch, gpu=False, dstorage=False, nvme=False)
    assert choose_backends(AUTO, AUTO).fallback_from == ""


# ────────────────────────────── probe behaviour ────────────────────────────


def test_detection_is_cached_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probing shells out; doing it per keystroke would freeze the settings window."""
    calls: list[int] = []

    def counting_probe() -> list[Accelerator]:
        calls.append(1)
        return [Accelerator("gpu", "fake", False)]

    monkeypatch.setattr(accel, "detect_gpus", counting_probe)
    for _ in range(25):
        accel.gpus()
    assert len(calls) == 1


def test_an_unprobed_machine_still_answers_immediately() -> None:
    """Before detection lands, auto must resolve to the always-available path."""
    assert not accel.probed()
    backend, reason = resolve_extraction_backend(AUTO)
    assert backend == CPU_FLEET
    assert reason


def test_a_failed_probe_command_reads_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accel.shutil, "which", lambda _name: None)
    assert not any(accelerator.available for accelerator in accel.detect_gpus())


# ──────────────────────────────── sharding ─────────────────────────────────


def test_a_book_always_lands_in_the_same_shard() -> None:
    """Sharding must not use hash(): it is salted per process and would scatter
    the same book to a different shard on every launch."""
    key = r"D:\Books\!Absalon, Absalon! - William Faulkner.epub"
    landings = {shard_for(key, 64) for _ in range(50)}
    assert len(landings) == 1
    assert 0 <= landings.pop() < 64


def test_one_shard_is_shard_zero() -> None:
    assert shard_for("anything", 1) == 0
    assert shard_for("anything", 0) == 0


def test_shards_spread_books_broadly() -> None:
    counts = [0] * 16
    for number in range(4000):
        counts[shard_for(f"book-{number}.epub", 16)] += 1
    assert min(counts) > 4000 // 16 // 2      # nothing wildly starved


def test_a_single_shard_keeps_the_plain_filename() -> None:
    assert shard_path("lib.db", 0, 1) == Path("lib.db")
    assert shard_path("lib.db", 3, 8).name == "lib.0003of0008.db"


# ─────────────────────────────── honest scale ──────────────────────────────


def test_index_size_matches_a_real_measured_index() -> None:
    """27,956 books at a 250,000-char budget really occupied 7.79 GB.

    That is 292 KB a book; the formula must land near it, or every capacity
    number the settings window shows is fiction.
    """
    measured = 7.79 * 1024 ** 3 / 27_956
    predicted = index_bytes_per_book(250_000, with_text=True)
    assert 0.85 < predicted / measured < 1.15


def test_turning_off_full_text_collapses_the_index() -> None:
    with_text = index_bytes_per_book(250_000, with_text=True)
    without = index_bytes_per_book(250_000, with_text=False)
    assert without * 100 < with_text


def test_capacity_grows_with_shards() -> None:
    one = capacity_report(1)
    many = capacity_report(1000)
    assert many.books_total == one.books_total * 1000
    assert "1,000 shards" in many.summary()


def test_capacity_without_full_text_holds_far_more_per_shard() -> None:
    lean = capacity_report(1, with_text=False)
    rich = capacity_report(1, with_text=True)
    assert lean.books_per_shard > rich.books_per_shard
