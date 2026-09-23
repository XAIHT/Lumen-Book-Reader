"""Same date contract under CPU and all hardware-capability fallback states."""
from __future__ import annotations

from itertools import product

import pytest

from lumen_reader import accel
from lumen_reader.library_index import LibraryIndex
from lumen_reader.turbo_scan import ScanConfig, TurboScanner
from test_library_index import make_epub


@pytest.mark.parametrize("gpu,ds,nvme", list(product((False, True), repeat=3)))
@pytest.mark.parametrize("preference", [accel.AUTO, accel.CPU_FLEET, accel.GPU_DIRECTSTORAGE])
def test_dates_survive_every_hardware_fallback(tmp_path, monkeypatch, gpu, ds, nvme, preference):
    monkeypatch.setattr(accel, "has_gpu", lambda: gpu)
    monkeypatch.setattr(accel, "directstorage", lambda: accel.Accelerator("directstorage", "fixture", ds))
    monkeypatch.setattr(accel, "storage", lambda: accel.Accelerator("storage", "fixture", nvme))
    monkeypatch.setattr(accel, "_extraction_implementations", {})
    root = tmp_path / "books"
    make_epub(root / "dated.epub", "Dated book", body="indexed chronology", published="1975-09")
    with LibraryIndex(tmp_path / "index.db") as index:
        config = ScanConfig(processes=1, extraction_backend=preference)
        scan = TurboScanner(index.path, root, config)
        scan.start()
        assert scan.wait(20)
        snapshot = scan.snapshot()
        assert snapshot.backend == accel.CPU_FLEET
        assert snapshot.books_committed == 1 and not snapshot.error
        row = index.search(root)[0]
        assert row.published_date == "1975-09" and row.date_metadata_version == 1
        assert row.created_ns is not None and row.mtime_ns is not None
        before = tuple(index.connection.execute("SELECT rowid,body FROM content_fts").fetchone())
        index.connection.execute("UPDATE books SET date_metadata_version=0,published_date='' ")
        index.connection.commit()
        index.scan(root, workers=1, config=config)
        assert index.search(root)[0].published_date == "1975-09"
        assert tuple(index.connection.execute("SELECT rowid,body FROM content_fts").fetchone()) == before


def test_registered_candidate_without_executor_does_not_mislabel_cpu_work(monkeypatch):
    monkeypatch.setattr(accel, "has_gpu", lambda: True)
    monkeypatch.setattr(accel, "directstorage", lambda: accel.Accelerator("directstorage", "fixture", True))
    monkeypatch.setattr(accel, "storage", lambda: accel.Accelerator("storage", "fixture", True))
    monkeypatch.setattr(accel, "_extraction_implementations", {accel.GPU_DIRECTSTORAGE: object})
    assert accel.resolve_extraction_backend(accel.AUTO)[0] == accel.GPU_DIRECTSTORAGE
    used, reason = accel.resolve_sweep_backend(accel.AUTO)
    assert used == accel.CPU_FLEET and "no executable Turbo Sweep adapter" in reason


def test_capacity_planning_includes_date_indexes():
    assert accel.index_bytes_per_book(with_text=False) == accel.INDEX_BYTES_METADATA + accel.DATE_INDEX_BYTES_PER_BOOK


def test_real_multiprocess_date_backfill_keeps_existing_text(tmp_path):
    root = tmp_path / "books"
    for number in range(4):
        make_epub(root / f"{number}.epub", published=f"{2000 + number}", body="preserve this body")
    with LibraryIndex(tmp_path / "index.db") as index:
        index.scan(root, workers=2)
        before = [tuple(row) for row in index.connection.execute("SELECT rowid,* FROM content_fts ORDER BY rowid")]
        index.connection.execute("UPDATE books SET date_metadata_version=0,published_date='' ")
        index.connection.commit()
        index.scan(root, workers=2)
        assert {row.published_date for row in index.search(root)} == {"2000", "2001", "2002", "2003"}
        assert [tuple(row) for row in index.connection.execute("SELECT rowid,* FROM content_fts ORDER BY rowid")] == before
