"""The reading-data export: the one step of the uninstall that cannot be redone.

Uninstalling Lumen is now total - configuration, index, caches and every
registry key go without asking. The single thing that survives is what the user
made: where they were in each book, and their marks, notes, quotes and tags.
That survives only because it is exported first, so these tests hold the export
to the standard the rest of the wizard is allowed to relax:

  * it must take the reading and leave the configuration behind,
  * it must not invent, reorder or re-key a position,
  * it must refuse to report success it cannot verify on disk.

The wizard is standard-library only and normally driven by Tk, so every test
here builds the collaborators by hand rather than starting a window.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_uninstaller():
    spec = importlib.util.spec_from_file_location("lumen_uninstall", ROOT / "uninstall.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lumen_uninstall"] = module
    spec.loader.exec_module(module)
    return module


uninstaller_module = _load_uninstaller()
LumenUninstaller = uninstaller_module.LumenUninstaller
MARKS_FILENAME = uninstaller_module.MARKS_FILENAME


class _Value:
    """Stands in for a tk StringVar/BooleanVar without a Tk root."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _wizard(appdata: Path, install: Path = None):
    """A LumenUninstaller with its filesystem pointed at a temp directory."""
    wizard = LumenUninstaller.__new__(LumenUninstaller)
    wizard.install_path = _Value(str(install or appdata))
    wizard.save_path = _Value(str(appdata))
    wizard.skip_save_var = _Value(False)
    wizard._export_path = ""
    wizard._export_counts = {}
    wizard._kept = []
    wizard._appdata_path = staticmethod(lambda: str(appdata))
    # bound, because the real one is a @staticmethod on the class
    wizard.__dict__["_appdata_path"] = lambda: str(appdata)
    return wizard


def _write_state(appdata: Path, **overrides):
    """A reader-state.json holding BOTH reading and configuration."""
    doc = {
        "theme": "dark",
        "font_size": 20,
        "sidebar_visible": True,
        "recent_books": [{"path": "C:/books/dune.epub", "title": "Dune", "author": "Herbert"}],
        "books": {
            "a1b2c3d4e5f6a7b8c9d0e1f2": {"chapter": 7, "scroll": 0.42, "bookmarks": [3, 9]},
            "0f1e2d3c4b5a69788796a5b4": {"chapter": 0, "scroll": 0.0, "bookmarks": []},
        },
        "speed_reader": {"wpm": 400},
        "scan": {"workers": 8},
        "search": {"backend": "fts5"},
        "accel": {"search": "sqlite"},
    }
    doc.update(overrides)
    home = appdata / "Lumen"
    home.mkdir(parents=True, exist_ok=True)
    (home / "reader-state.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def _write_marks(directory: Path, *ids: str):
    directory.mkdir(parents=True, exist_ok=True)
    marks = [
        {"id": mark_id, "book_title": f"Book {mark_id}", "note": "a note",
         "quote": "a quote", "tags": ["t"], "chapter_index": 1, "scroll_percent": 0.5}
        for mark_id in ids
    ]
    (directory / MARKS_FILENAME).write_text(json.dumps(marks), encoding="utf-8")
    return marks


# ── What the export takes, and what it deliberately leaves ──────────────────

def test_export_takes_the_reading(tmp_path):
    appdata = tmp_path / "Lumen Reader"
    _write_state(appdata)
    payload, counts = _wizard(appdata)._collect_reading_data()

    assert counts["positions"] == 2
    assert payload["positions"]["a1b2c3d4e5f6a7b8c9d0e1f2"]["chapter"] == 7
    assert payload["positions"]["a1b2c3d4e5f6a7b8c9d0e1f2"]["scroll"] == 0.42
    assert payload["positions"]["a1b2c3d4e5f6a7b8c9d0e1f2"]["bookmarks"] == [3, 9]


def test_export_leaves_the_configuration_behind(tmp_path):
    """The whole point of the change: configuration is erased, not exported.

    Copying reader-state.json wholesale would smuggle theme, fonts, sweep,
    search and acceleration settings back out in the file the user keeps -
    which is precisely what an uninstall was asked to destroy.
    """
    appdata = tmp_path / "Lumen Reader"
    _write_state(appdata)
    payload, _counts = _wizard(appdata)._collect_reading_data()

    serialised = json.dumps(payload)
    for banned in ("theme", "font_size", "sidebar_visible", "speed_reader",
                   "scan", "search", "accel"):
        assert banned not in payload, f"{banned} is configuration and must not be exported"
    assert "fts5" not in serialised
    assert "sqlite" not in serialised


def test_position_keys_are_never_rewritten(tmp_path):
    """``books`` is keyed by hash(path|size|mtime); re-keying silently loses it."""
    appdata = tmp_path / "Lumen Reader"
    original = _write_state(appdata)
    payload, _ = _wizard(appdata)._collect_reading_data()
    assert set(payload["positions"]) == set(original["books"])


# ── Marks live beside the library, not in AppData ───────────────────────────

def test_marks_are_found_beside_the_library(tmp_path):
    appdata = tmp_path / "Lumen Reader"
    library = tmp_path / "My Books"
    _write_state(appdata, recent_books=[{"path": str(library / "dune.epub"),
                                         "title": "Dune", "author": "Herbert"}])
    _write_marks(library, "mark-one", "mark-two")

    payload, counts = _wizard(appdata)._collect_reading_data()
    assert counts["marks"] == 2
    assert {m["id"] for m in payload["reading_marks"]} == {"mark-one", "mark-two"}


def test_the_same_mark_in_two_places_is_exported_once(tmp_path):
    """A migration leaves copies behind; the export must not duplicate them."""
    appdata = tmp_path / "Lumen Reader"
    library = tmp_path / "My Books"
    _write_state(appdata, recent_books=[{"path": str(library / "dune.epub"),
                                         "title": "Dune", "author": "Herbert"}])
    _write_marks(library, "shared", "only-library")
    _write_marks(appdata / "Lumen", "shared")

    payload, counts = _wizard(appdata)._collect_reading_data()
    assert counts["marks"] == 2
    assert sorted(m["id"] for m in payload["reading_marks"]) == ["only-library", "shared"]


def test_missing_state_is_an_empty_export_not_a_crash(tmp_path):
    """A user who never opened a book still gets a clean uninstall."""
    appdata = tmp_path / "Lumen Reader"
    appdata.mkdir(parents=True)
    payload, counts = _wizard(appdata)._collect_reading_data()
    assert counts["positions"] == 0 and counts["marks"] == 0
    assert payload["positions"] == {} and payload["reading_marks"] == []


# ── Writing it out ──────────────────────────────────────────────────────────

def test_export_is_written_and_reads_back(tmp_path):
    appdata = tmp_path / "Lumen Reader"
    destination = tmp_path / "Desktop"
    _write_state(appdata)

    path, counts = _wizard(appdata)._export_reading_data(str(destination))
    assert Path(path).is_file()
    assert Path(path).parent == destination

    restored = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(restored["positions"]) == counts["positions"] == 2
    assert "Angela López Mendoza" in restored["_created_by"]


def test_a_second_export_never_overwrites_the_first(tmp_path):
    """Two uninstalls in one day must not silently replace the earlier file."""
    appdata = tmp_path / "Lumen Reader"
    destination = tmp_path / "Desktop"
    _write_state(appdata)

    first, _ = _wizard(appdata)._export_reading_data(str(destination))
    second, _ = _wizard(appdata)._export_reading_data(str(destination))
    assert first != second
    assert Path(first).is_file() and Path(second).is_file()


def test_no_partial_file_survives_a_successful_export(tmp_path):
    appdata = tmp_path / "Lumen Reader"
    destination = tmp_path / "Desktop"
    _write_state(appdata)
    _wizard(appdata)._export_reading_data(str(destination))
    assert not list(destination.glob("*.part"))


def test_an_unwritable_destination_raises_before_anything_is_deleted(tmp_path):
    """The export must fail loudly. Silence here costs the user everything."""
    appdata = tmp_path / "Lumen Reader"
    _write_state(appdata)
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a folder", encoding="utf-8")

    with pytest.raises(OSError):
        _wizard(appdata)._export_reading_data(str(blocked))


# ── The export must survive the erase that follows it ───────────────────────

def test_an_export_saved_into_the_state_folder_is_protected(tmp_path):
    """Nothing stops a user saving into %APPDATA%\\Lumen Reader itself.

    Erasing that tree afterwards would delete the very file the wizard just
    promised them, so containment is checked before the tree is removed.
    """
    appdata = tmp_path / "Lumen Reader"
    _write_state(appdata)
    wizard = _wizard(appdata)
    path, _ = wizard._export_reading_data(str(appdata))
    wizard._export_path = path

    assert wizard._protects_export(str(appdata)) is True
    assert wizard._protects_export(str(tmp_path / "somewhere else")) is False


def test_no_export_means_nothing_to_protect(tmp_path):
    """A user who opted out has no file that needs saving from the erase."""
    appdata = tmp_path / "Lumen Reader"
    appdata.mkdir(parents=True)
    wizard = _wizard(appdata)
    wizard._export_path = ""
    assert wizard._protects_export(str(appdata)) is False
