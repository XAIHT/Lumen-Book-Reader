"""Explorer launch paths must never turn system folders into writable libraries."""

from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys
import tempfile

import pytest

from lumen_reader import app, launcher, runtime_paths
from lumen_reader.storage import ReaderStore


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer registry")
def test_installed_library_reads_the_installers_registry_key(tmp_path, monkeypatch):
    import winreg

    keys = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_key(hive, key):
        assert hive == winreg.HKEY_CURRENT_USER
        keys.append(key)
        assert key == r"Software\XAIHT\Lumen Book Reader"
        return Key()

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (str(tmp_path), winreg.REG_SZ))
    assert app.installed_library_root() == tmp_path
    assert len(keys) == 1


def test_frozen_start_without_a_book_ignores_inherited_working_directory(tmp_path, monkeypatch):
    inherited = tmp_path / "unrelated-application"
    inherited.mkdir()
    monkeypatch.chdir(inherited)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "installed_library_root", lambda: None)
    store = ReaderStore(tmp_path / "state" / "reader-state.json")
    root = app.resolve_library_root(store)
    assert root == store.path.parent / "library"
    assert root.is_dir()
    assert not list(inherited.iterdir())


def test_explicit_pdf_folder_wins_over_saved_library(tmp_path):
    folder = tmp_path / "PDFs with spaces"
    folder.mkdir()
    store = ReaderStore(tmp_path / "reader-state.json")
    store.data["library_root"] = str(tmp_path)
    assert app.resolve_library_root(store, str(folder / "Example.PDF")) == folder


def test_launcher_preserves_pdf_argument_when_chdir_is_denied(tmp_path, monkeypatch):
    book = tmp_path / "A book with spaces.PDF"
    book.touch()

    def deny_chdir(path):
        raise PermissionError("Inherited working directory cannot change")

    monkeypatch.setattr(os, "chdir", deny_chdir)
    assert launcher._anchor_to_book(["Lumen.exe", str(book)]) == ["Lumen.exe", str(book.resolve())]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scratch policy")
def test_scratch_rejects_windows_user_profiles(monkeypatch):
    destination = Path.home() / "Lumen-test-scratch-must-not-exist"
    monkeypatch.setenv("LUMEN_SCRATCH_DIR", str(destination))
    with pytest.raises(OSError, match="outside Windows user profiles"):
        runtime_paths.scratch_directory()
    assert not destination.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scratch policy")
def test_scratch_ignores_profile_temp_and_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_SCRATCH_DIR", raising=False)
    monkeypatch.setenv("ProgramData", str(tmp_path))
    monkeypatch.setenv("TEMP", str(Path.home() / "AppData" / "Local" / "Temp"))
    monkeypatch.setenv("TMP", str(Path.home() / "AppData" / "Local" / "Temp"))
    scratch = runtime_paths.scratch_directory()
    assert scratch.is_relative_to(tmp_path)
    assert scratch.name == "scratch"
    assert scratch.is_dir()


def test_legacy_bookmarks_survive_read_only_library(tmp_path, monkeypatch):
    from lumen_reader.marks import MarksStore
    from lumen_reader.ui import ReaderWindow

    state = ReaderStore(tmp_path / "reader-state.json")
    legacy = [{"chapter_index": 0, "scroll_percent": 0.25}]
    state.book_state("example")["bookmarks"] = legacy.copy()
    marks = MarksStore(tmp_path / "library" / "lumen-reading-marks.json")
    window = SimpleNamespace(
        book=SimpleNamespace(key="example", path=tmp_path / "example.pdf",
                             chapters=[SimpleNamespace(title="Page 1")],
                             metadata=SimpleNamespace(title="Example", author_line="")),
        store=state, marks_store=marks,
    )

    def deny_save():
        raise PermissionError("Library is read-only")

    monkeypatch.setattr(marks, "save", deny_save)
    ReaderWindow._migrate_legacy_bookmarks(window)
    assert state.book_state("example")["bookmarks"] == legacy
    assert marks.marks


def test_qt_and_child_processes_share_owned_scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_SCRATCH_DIR", str(tmp_path))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, "unused")
    monkeypatch.setattr(tempfile, "tempdir", None)
    runtime_paths.configure_process_scratch()
    assert tempfile.gettempdir() == str(tmp_path)
    assert all(os.environ[name] == str(tmp_path) for name in ("TEMP", "TMP", "TMPDIR"))


@pytest.mark.skipif(sys.platform != "win32", reason="Real visible Windows launch")
@pytest.mark.parametrize("scenario", ["direct", "no-argument", "read-only"])
def test_visible_pdf_startup_from_system32(tmp_path, scenario):
    import pymupdf

    folder = tmp_path / "PDF books with spaces"
    folder.mkdir()
    book = folder / "Reading sample.PDF"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), "Lumen PDF startup verification", fontsize=20)
        document.save(book)
    env = dict(os.environ, LUMEN_DATA_DIR=str(tmp_path / "state"),
               LUMEN_SCRATCH_DIR=str(tmp_path / "scratch"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               QT_QPA_PLATFORM="windows", PYTHONIOENCODING="utf-8")
    env.pop("LUMEN_INDEX_PATH", None)
    probe = r'''
import json, sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from lumen_reader import app as bootstrap
from lumen_reader.marks import MarksStore
from lumen_reader.ui import ReaderWindow

book = Path(sys.argv[1])
scenario = sys.argv[2]
inherited = Path.cwd()
outcome = {"rendered": False}
bootstrap.installed_library_root = lambda: None
if scenario == "read-only":
    def deny_save(*args, **kwargs):
        raise PermissionError("Read-only library")
    MarksStore.save = deny_save

class ObservedReader(ReaderWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scan behavior is independent of opening/rendering and would slow
        # down this startup regression. All data belongs to this test only.
        self.store.data.setdefault("scan", {})["scan_on_startup"] = False
        self.probe_timer = QTimer(self)
        self.probe_timer.timeout.connect(self.probe)
        self.probe_timer.start(250)
        QTimer.singleShot(20000, self.close)
        if scenario == "no-argument":
            QTimer.singleShot(750, lambda: self.open_book(str(book)))

    def probe(self):
        if self.book is not None:
            self.web.page().runJavaScript(
                "JSON.stringify({text: [...document.querySelectorAll('.pdf-word')]"
                ".map(w => w.textContent.trim()).join(' '), image: "
                "[...document.images].some(i => i.complete && i.naturalWidth > 0)})",
                self.rendered,
            )

    def rendered(self, raw):
        if outcome["rendered"] or not raw:
            return
        value = json.loads(raw)
        outcome["last_page"] = value
        if "Lumen PDF startup verification" not in value.get("text", "") or not value.get("image"):
            return
        assert self.isVisible()
        assert self.book.path == book.resolve()
        assert Path(self.library_root) != inherited
        assert not self.book.extract_dir.is_relative_to(Path.home())
        assert not self.marks_store.path.exists()
        outcome["rendered"] = True
        self.probe_timer.stop()
        print("PASS: visible PDF text and image from System32: " + scenario, flush=True)
        QTimer.singleShot(1500, self.close)

bootstrap.ReaderWindow = ObservedReader
arguments = ["Lumen.exe"] + ([] if scenario == "no-argument" else [str(book)])
code = bootstrap.main(arguments)
assert code == 0 and outcome["rendered"], "PDF did not render: " + repr(outcome)
'''
    result = subprocess.run(
        [sys.executable, "-c", probe, str(book), scenario],
        cwd=Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32",
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
