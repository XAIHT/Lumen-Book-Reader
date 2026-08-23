# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""The sweep must be usable on the machine most people actually own.

Lumen's fleet was tuned on a 22-processor workstation with NVMe.  These tests
pin the behaviour on the hardware that workstation is *not*: four cores, a 7200
rpm disk, 8 GB of memory.  Every one of them describes a way the old defaults
would have made the reader unusable there, so a future tuning change cannot
quietly take the low end back out.

The machine is injected rather than detected: a test that only passes on the
machine that runs it proves nothing about anyone else's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lumen_reader import machine_profile
from lumen_reader.library_index import LibraryIndex
from lumen_reader.turbo_scan import sweep
from lumen_reader.machine_profile import (
    STORAGE_HDD,
    STORAGE_NETWORK,
    STORAGE_NVME,
    STORAGE_REMOVABLE,
    STORAGE_SSD,
    STORAGE_UNKNOWN,
    MachineProfile,
)
from lumen_reader.turbo_scan import AUTO_PRIORITY, MAX_PROCESSES, ScanConfig

from test_library_index import make_epub, make_pdf

GIGABYTE = 1024 ** 3


def pretend(monkeypatch: pytest.MonkeyPatch, **fields) -> MachineProfile:
    """Make every sizing decision see the machine described by *fields*."""
    profile = MachineProfile(**fields)
    monkeypatch.setattr(machine_profile, "profile", lambda _root=None: profile)
    return profile


#: The machine this whole change exists for: a cheap four-core laptop with a
#: mechanical disk.  Named so the failures read as sentences.
LOW_END = dict(logical_cpus=4, ram_bytes=8 * GIGABYTE, storage=STORAGE_HDD,
               storage_detail="C: reports a seek penalty: mechanical disk.")

#: What Lumen was developed on, so the regression can be seen from both sides.
WORKSTATION = dict(logical_cpus=22, ram_bytes=64 * GIGABYTE, storage=STORAGE_NVME,
                   storage_detail="C: is on an NVMe bus.")


# ───────────────────────── detection, on any machine ────────────────────────


def test_probing_this_machine_never_raises_and_answers_sanely() -> None:
    """A hardware probe must never be able to stop a book from opening."""
    profile = machine_profile.profile(".")
    assert profile.logical_cpus >= 1
    assert profile.ram_bytes >= 0
    assert profile.storage in {
        STORAGE_NVME, STORAGE_SSD, STORAGE_HDD,
        STORAGE_REMOVABLE, STORAGE_NETWORK, STORAGE_UNKNOWN,
    }
    assert profile.summary()


def test_a_path_that_does_not_exist_still_produces_a_profile() -> None:
    profile = machine_profile.profile("Q:/no/such/place")
    assert profile.logical_cpus >= 1
    assert profile.storage in {STORAGE_UNKNOWN, STORAGE_NETWORK}


def test_unknown_storage_is_not_treated_as_a_spindle() -> None:
    """Throttling every machine we failed to identify would be the worse bug."""
    assert not MachineProfile(storage=STORAGE_UNKNOWN).seek_bound
    assert MachineProfile(storage=STORAGE_HDD).seek_bound
    assert MachineProfile(storage=STORAGE_REMOVABLE).seek_bound
    assert not MachineProfile(storage=STORAGE_SSD).seek_bound


def test_memory_thresholds_treat_an_unknown_size_as_roomy() -> None:
    """0 means "could not tell", and must not read as "no memory"."""
    unknown = MachineProfile(ram_bytes=0)
    assert not unknown.low_memory and not unknown.tight_memory
    assert MachineProfile(ram_bytes=6 * GIGABYTE).low_memory
    assert MachineProfile(ram_bytes=3 * GIGABYTE).tight_memory


# ──────────────────── the fleet a low-end machine gets ──────────────────────


def test_a_mechanical_disk_gets_a_narrow_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """One head, ~100 random IOPS: more readers is slower, not faster."""
    pretend(monkeypatch, **LOW_END)
    assert ScanConfig().resolved_processes() == 2


def test_a_mechanical_disk_does_not_get_high_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """That sweep waits on the head; raising it only starves the desktop."""
    pretend(monkeypatch, **LOW_END)
    assert ScanConfig().resolved_priority() == "normal"


def test_a_mechanical_disk_does_not_get_a_thundering_herd_of_walkers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, **LOW_END)
    assert ScanConfig().resolved_walkers() == 2


def test_a_small_machine_keeps_a_core_for_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window that cannot repaint reads as a hung program, not a busy one."""
    pretend(monkeypatch, logical_cpus=4, ram_bytes=16 * GIGABYTE, storage=STORAGE_SSD)
    assert ScanConfig().resolved_processes() == 3


def test_a_four_core_machine_never_runs_the_fleet_above_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, logical_cpus=4, ram_bytes=16 * GIGABYTE, storage=STORAGE_NVME)
    assert ScanConfig().resolved_priority() == "normal"


def test_a_midsize_machine_gets_above_normal_not_high(monkeypatch: pytest.MonkeyPatch) -> None:
    pretend(monkeypatch, logical_cpus=8, ram_bytes=16 * GIGABYTE, storage=STORAGE_SSD)
    config = ScanConfig()
    assert config.resolved_processes() == 7
    assert config.resolved_priority() == "above"


def test_little_memory_caps_the_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each worker holds a book and its text; a wide fleet on 6 GB swaps."""
    pretend(monkeypatch, logical_cpus=16, ram_bytes=6 * GIGABYTE, storage=STORAGE_SSD)
    assert ScanConfig().resolved_processes() == 4
    pretend(monkeypatch, logical_cpus=16, ram_bytes=3 * GIGABYTE, storage=STORAGE_SSD)
    assert ScanConfig().resolved_processes() == 2


def test_a_small_machine_holds_fewer_books_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """The result queue carries extracted text, so it is where the memory goes."""
    pretend(monkeypatch, logical_cpus=16, ram_bytes=6 * GIGABYTE, storage=STORAGE_SSD)
    roomy = ScanConfig()
    assert roomy.resolved_result_queue() == roomy.resolved_processes() * 16


def test_search_quality_is_never_traded_for_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slower sweep is fair.  A quietly less searchable library is not."""
    pretend(monkeypatch, **LOW_END)
    low_end = ScanConfig().effective_text_budget()
    pretend(monkeypatch, **WORKSTATION)
    assert ScanConfig().effective_text_budget() == low_end


# ─────────────────── the workstation contract is unchanged ──────────────────


def test_a_workstation_still_gets_one_high_priority_process_per_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Angela's original requirement, unchanged on the machine it was written for."""
    pretend(monkeypatch, **WORKSTATION)
    config = ScanConfig()
    assert config.resolved_processes() == 22
    assert config.resolved_priority() == "high"
    assert config.resolved_walkers() == 44


def test_a_network_share_is_latency_not_seeks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extra workers wait in parallel there; they do not fight over a head."""
    pretend(monkeypatch, logical_cpus=22, ram_bytes=64 * GIGABYTE, storage=STORAGE_NETWORK)
    config = ScanConfig()
    assert config.resolved_processes() == 8
    assert config.resolved_walkers() == 44


# ───────────────────────── the user still decides ───────────────────────────


def test_an_explicit_fleet_is_obeyed_on_any_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second-guessing a deliberate choice is how a settings window loses trust."""
    pretend(monkeypatch, **LOW_END)
    assert ScanConfig(processes=12).resolved_processes() == 12
    assert ScanConfig(priority="realtime").resolved_priority() == "realtime"
    assert ScanConfig(walkers=32).resolved_walkers() == 32


def test_an_explicit_fleet_still_respects_the_windows_handle_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, **LOW_END)
    assert ScanConfig(processes=5000).resolved_processes() == MAX_PROCESSES


def test_auto_is_the_default_and_survives_a_round_trip() -> None:
    config = ScanConfig()
    assert config.priority == AUTO_PRIORITY
    assert ScanConfig.from_mapping(config.to_dict()) == config


def test_a_corrupt_priority_becomes_auto_not_high() -> None:
    """The case where we cannot read the settings is the case to ask the machine."""
    assert ScanConfig.from_mapping({"priority": "supersonic"}).priority == AUTO_PRIORITY


# ───────────────────────── and it says so out loud ──────────────────────────


def test_the_sweep_explains_why_it_held_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Lumen decided your computer is slow", unexplained, is indistinguishable
    from Lumen being broken."""
    pretend(monkeypatch, **LOW_END)
    notes = " ".join(ScanConfig().tuning_notes())
    assert "seek penalty" in notes
    assert "NORMAL" in notes
    assert "4 logical processors" in notes


def test_the_notes_never_claim_a_throttle_that_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pretend(monkeypatch, **WORKSTATION)
    notes = " ".join(ScanConfig().tuning_notes())
    assert "held to" not in notes
    assert "HIGH" in notes


# ─────────── and the throttled sweep is a working sweep, end to end ─────────


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    root = tmp_path / "shelf"
    make_epub(root / "alpha.epub", "Alpha World", "Ada Writer", "hydrology at length")
    make_epub(root / "nested" / "beta.epub", "Beta Days", "Ben Author", "medieval falconry")
    make_pdf(root / "gamma.pdf", "Gamma Report", "detonation velocity")
    return root


def test_the_low_end_machine_actually_indexes_its_library(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right numbers are not the claim - a working library is.

    Everything above asserts what ``auto`` *decides* on a four-core machine with
    a spinning disk.  This runs the whole pipeline at those decisions and checks
    that the books are all there and searchable afterwards, because a fleet
    tuned down to two processes at Normal priority is only a fix if it still
    does the job.
    """
    pretend(monkeypatch, **LOW_END)
    config = ScanConfig()
    assert (config.resolved_processes(), config.resolved_priority()) == (2, "normal")

    final = sweep(tmp_path / "low-end.db", library, config)

    assert final.phase == "done"
    assert final.error == ""
    assert final.books_indexed == 3
    assert final.books_failed == 0
    with LibraryIndex(tmp_path / "low-end.db") as index:
        assert index.counts(library).total == 3
        assert [row.title for row in index.search(library, "Alpha")] == ["Alpha World"]
        # And the text *inside* the books, not just their metadata: memory is
        # relieved by holding fewer books in flight, never by indexing less of
        # each one, so "search inside books" works the same on a small machine.
        found = index.search(library, "falconry", mode="content")
        assert [row.title for row in found] == ["Beta Days"]
