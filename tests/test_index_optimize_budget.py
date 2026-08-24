"""``optimize`` must never be the thing that fills the user's system drive.

The bug these pin down was measured, not imagined: an 11.15 GB index with a
2.16 GB WAL on a C: with 16 GB free. ``optimize`` ran an FTS5 full merge (the
whole text index rewritten in ONE transaction, so the WAL grows to the size of
the index) and then a ``VACUUM`` (a complete second copy of the database), on
the GUI thread, with no free-space check anywhere in the codebase. Neither
statement fails cleanly when the volume fills, and a Windows system drive at
zero free space cannot page - so the symptom was not a failed query, it was a
stopped machine.

Free space is injected here rather than detected, because the failure only
appears on a disk this workstation does not have.
"""

from __future__ import annotations

import shutil

import pytest

from lumen_reader.library_index import (
    INCREMENTAL_MERGE_PAGES,
    SAFETY_FLOOR_BYTES,
    LibraryIndex,
    OptimizeReport,
)

GIGABYTE = 1024 ** 3

#: The machine state that actually went down, byte for byte, as measured on
#: 2026-08-23. ``database_bytes()`` sums the database, the WAL and the shared
#: memory file, which is the right total: all three sit on the same volume, and
#: the WAL is the part a killed optimize leaves behind.
MEASURED_DB = 11_153_821_696        # library-index.db
MEASURED_WAL = 2_165_245_432        # library-index.db-wal
MEASURED_SHM = 4_227_072            # library-index.db-shm
MEASURED_TOTAL = MEASURED_DB + MEASURED_WAL + MEASURED_SHM      # 13.32 GB
MEASURED_FREE = 16 * GIGABYTE                                   # 17.18 GB free on C:


@pytest.fixture()
def index(tmp_path):
    index = LibraryIndex(tmp_path / "library-index.db")
    yield index
    index.connection.close()


def _pretend(index, *, database: int, free: int, monkeypatch):
    """Put the index on a disk of a given size, whatever this machine has."""
    monkeypatch.setattr(type(index), "database_bytes", lambda _self: database)
    monkeypatch.setattr(type(index), "free_bytes", lambda _self: free)


def _statements(index) -> list[str]:
    """Record the SQL ``optimize`` actually issues.

    ``set_trace_callback`` rather than a patched ``execute``: sqlite3.Connection
    is a C type and will not accept an attribute, and the trace hook has the
    advantage of seeing the statement SQLite really ran, parameters bound.
    """
    seen: list[str] = []
    index.connection.set_trace_callback(
        lambda statement: seen.append(" ".join(str(statement).split()))
    )
    return seen


# ── The measured catastrophe ────────────────────────────────────────────────

def test_the_real_world_case_does_not_vacuum(index, monkeypatch):
    """13.3 GB of index files, 16 GB free: the machine state that went down."""
    _pretend(index, database=MEASURED_TOTAL, free=MEASURED_FREE,
             monkeypatch=monkeypatch)
    seen = _statements(index)
    report = index.optimize()

    assert "VACUUM" not in seen, "VACUUM needs a second copy that does not fit"
    assert not any("VALUES('optimize')" in s for s in seen), (
        "a full FTS merge rewrites the whole index in one transaction"
    )
    assert report.deferred is True
    assert report.vacuumed is False and report.merged_fully is False


def test_a_roomy_disk_still_gets_the_full_treatment(index, monkeypatch):
    """The guard must not punish someone who has the space."""
    _pretend(index, database=2 * GIGABYTE, free=500 * GIGABYTE,
             monkeypatch=monkeypatch)
    seen = _statements(index)
    report = index.optimize()

    assert "VACUUM" in seen
    assert sum("VALUES('optimize')" in s for s in seen) == 2, "both FTS tables"
    assert report.deferred is False
    assert report.merged_fully and report.vacuumed


# ── Where the line sits ─────────────────────────────────────────────────────

def test_headroom_is_the_database_plus_a_floor_the_volume_keeps(index, monkeypatch):
    """Just under the line defers; just over it proceeds."""
    database = 10 * GIGABYTE
    needed = database + SAFETY_FLOOR_BYTES

    _pretend(index, database=database, free=needed - 1, monkeypatch=monkeypatch)
    assert index.optimize().deferred is True

    _pretend(index, database=database, free=needed + 1, monkeypatch=monkeypatch)
    assert index.optimize().deferred is False


def test_a_floor_is_left_even_when_the_copy_would_technically_fit(index, monkeypatch):
    """Fitting is not enough - the volume must survive the operation.

    A 10 GB database with 10.5 GB free could complete a VACUUM and leave the
    system drive with 500 MB, which on Windows means the pagefile cannot grow.
    """
    _pretend(index, database=10 * GIGABYTE, free=10 * GIGABYTE + 512 * 1024 ** 2,
             monkeypatch=monkeypatch)
    assert index.optimize().deferred is True


# ── Unknown hardware is not the worst case ──────────────────────────────────

def test_unmeasurable_free_space_is_not_treated_as_empty(index, monkeypatch):
    """A volume we cannot measure gets the cheap path, never the expensive one.

    Throttling every machine you failed to identify is the worse bug, but the
    expensive path is the one that stops computers - so unknown takes the
    branch that is always safe, and says it deferred.
    """
    _pretend(index, database=GIGABYTE, free=-1, monkeypatch=monkeypatch)
    seen = _statements(index)
    report = index.optimize()

    assert not any("VALUES('optimize')" in s for s in seen)
    assert report.merged_fully is False
    assert report.deferred is True


# ── What always happens ─────────────────────────────────────────────────────

@pytest.mark.parametrize("free", [16 * GIGABYTE, 500 * GIGABYTE, -1])
def test_the_wal_is_always_checkpointed(index, monkeypatch, free):
    """The cheap win, and the one that rescues an index left mid-flight.

    A killed optimize leaves gigabytes of WAL behind; truncating it needs no
    headroom at all, so it must never be gated on the same budget as VACUUM.
    """
    _pretend(index, database=MEASURED_TOTAL, free=free, monkeypatch=monkeypatch)
    seen = _statements(index)
    index.optimize()
    assert any("wal_checkpoint(TRUNCATE)" in s for s in seen)


def test_the_deferred_path_still_merges_incrementally(index, monkeypatch):
    """Doing less at once, not doing a worse job."""
    _pretend(index, database=MEASURED_TOTAL, free=MEASURED_FREE,
             monkeypatch=monkeypatch)
    seen = _statements(index)
    index.optimize()

    merges = [s for s in seen if "'merge'" in s]
    assert merges, "the deferred path must still reclaim what it can"
    assert any("books_fts" in s for s in merges)
    assert any("content_fts" in s for s in merges)
    # And it must checkpoint between rounds, or the WAL grows into the very
    # problem the incremental path exists to avoid.
    assert any("wal_checkpoint(PASSIVE)" in s for s in seen)


def test_incremental_merge_is_bounded_by_pages_not_by_the_index(index, monkeypatch):
    """The page count is what keeps peak WAL off the size of the index."""
    _pretend(index, database=MEASURED_TOTAL, free=MEASURED_FREE,
             monkeypatch=monkeypatch)
    seen = _statements(index)
    index.optimize()

    merges = [s for s in seen if "'merge'" in s]
    assert merges, "no bounded merge was issued"
    # The trace hook reports the statement with its parameters bound, so the
    # bound itself is visible rather than inferred.
    assert all(str(INCREMENTAL_MERGE_PAGES) in s for s in merges), merges


# ── The report has to be usable by the window that shows it ─────────────────

def test_report_reports_what_was_reclaimed(index, monkeypatch):
    sizes = iter([9 * GIGABYTE, 6 * GIGABYTE])   # before, after
    monkeypatch.setattr(type(index), "database_bytes", lambda _self: next(sizes))
    monkeypatch.setattr(type(index), "free_bytes", lambda _self: 900 * GIGABYTE)
    report = index.optimize()
    assert report.reclaimed == 3 * GIGABYTE


def test_a_grown_database_never_reports_negative_reclaim():
    """VACUUM can leave a file larger; the summary must not print a negative."""
    report = OptimizeReport(merged_fully=True, vacuumed=True, before=100,
                            after=140, free_before=0, needed=0)
    assert report.reclaimed == 0


def test_optimize_runs_for_real_on_a_real_index(index):
    """Not a mock: the SQL must be valid against the actual FTS5 schema."""
    index.connection.execute(
        "INSERT INTO content_fts(body, book_id) VALUES ('lorem ipsum dolor', 1)")
    index.connection.commit()
    report = index.optimize()
    assert isinstance(report, OptimizeReport)
    assert index.connection.execute(
        "SELECT count(*) FROM content_fts").fetchone()[0] == 1


def test_free_bytes_survives_a_volume_it_cannot_stat(index, monkeypatch):
    """A removable drive pulled mid-session must not crash the settings window."""
    def explode(_path):
        raise OSError("the device is not ready")

    monkeypatch.setattr(shutil, "disk_usage", explode)
    assert index.free_bytes() == -1
