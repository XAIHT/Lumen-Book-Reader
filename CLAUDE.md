# Lumen Book Reader — project instructions

**Created by Angela López Mendoza · @angelahack1.** Her name is the authorship of
this project and is never scrubbed, abbreviated, or reattributed.

Angela's machine-wide rules in `~/.claude/CLAUDE.md` apply here in full and take
precedence: never rewrite git history, never run an invisible test, screenshots
are taken by Tlamatini's Shoter, and she is addressed by name. This file adds
only what is specific to *this* repository.

---

## What this is

A desktop EPUB and PDF reader in Python and Qt, with three subsystems that each
have real depth:

| Subsystem | Entry point | Design doc |
|---|---|---|
| Reading surface | `lumen_reader/ui.py`, `book.py`, `pdf_book.py` | — |
| RSVP speed reader | `lumen_reader/speed_reader.py` | [SpeedReadingToolInLumenReader.md](SpeedReadingToolInLumenReader.md) |
| Library engine | `lumen_reader/turbo_scan.py`, `library_index.py`, `accel.py`, `machine_profile.py` | [LibraryEngineInLumenReader.md](LibraryEngineInLumenReader.md) |
| Definitions | `lumen_reader/dictionary.py`, `smart_definition.py` | — |
| Release scheme | `build_complete_release.py` and friends | [RELEASING.md](RELEASING.md) |

`lumen_reader/ui.py` is large (~175 KB). Read the region you need rather than
the whole file.

## Running it

- **GUI toolkit is PySide6**, not PyQt6. Imports are `from PySide6.QtWidgets …`.
- **The app runs on system Python 3.12**, `C:\Program Files\Python312\`, not on a
  virtualenv. Launch without a console with `pythonw.exe`.
- From a checkout: `python run_reader.py`, or `python run_reader.py "<book>"`.
- The release wizards are **Tkinter**, so the interpreter used for a build must
  have `tkinter`. See RELEASING.md ▸ Requirements.

```powershell
& "C:/Program Files/Python312/pythonw.exe" C:\Lumen-Book-Reader\run_reader.py
```

## Tests

```powershell
python -m pytest        # 322 tests
```

Per Angela's global rule, run them in a **visible foreground window** she can
watch — `Start-Process powershell -NoExit …` with `dangerouslyDisableSandbox` —
never backgrounded, never hidden. A test that cannot be made visible is not run.

`tests/test_release_scheme.py` is a guard, not a unit test: it fails the build if
the installer and uninstaller stop mirroring each other, or if the build source
grows a git call that would rewrite history. Do not "fix" it by relaxing it.

## Documentation

Six documents, and they are expected to agree with the code:

| File | Scope |
|---|---|
| `README.md` | The front page: features, install, keyboard table, doc map |
| `LibraryEngineInLumenReader.md` | Sweep pipeline, index schema, search, acceleration |
| `SpeedReadingToolInLumenReader.md` | RSVP design, evidence, timing, markers |
| `RELEASING.md` | Release pipeline, versioning, registry surfaces, pre-tag checklist |
| `THIRD_PARTY_NOTICES.md` | Dependency inventory and redistribution checklist |
| `CHANGELOG.md` | What each tag actually contains |

**Line endings are mixed, per file, and must be preserved:**

| CRLF | LF |
|---|---|
| `README.md`, `RELEASING.md` | `THIRD_PARTY_NOTICES.md`, `SpeedReadingToolInLumenReader.md`, `LibraryEngineInLumenReader.md`, `CHANGELOG.md`, `CLAUDE.md` |

Rewriting a file with the wrong ending turns a 20-line edit into a 400-line diff
that hides the real change, and `sed -i` does exactly that to a whole file.

Check the **committed** bytes before editing, not the working copy:

```powershell
git show HEAD:THIRD_PARTY_NOTICES.md | python -c "import sys; d=sys.stdin.buffer.read(); print(d.count(b'\r\n'), 'CRLF')"
```

`grep -c $'\r'` under this Git Bash reports every line as CRLF even for an LF
file, so it cannot be used for this. Patch with a Python script that reads with
universal newlines and writes with the matching `newline=`, or with the
Write/Edit tools.

## Versioning

`lumen_reader/version.py` is the single source of truth at build time and at
runtime; **git tags are the source of truth for the number**. `_version.py` is
generated and gitignored.

A tag's message must describe what the tag contains, because it can never be
corrected afterwards — history is not rewritten here. `CHANGELOG.md` ▸ *Notes on
version history* records the two places where this has already gone wrong.

## Conventions that matter here

- **Comments explain the decision, not the syntax.** This codebase documents
  *why* a thing is the way it is, usually with the measurement that forced it —
  see the `fts_rowid` comment block in `library_index.py`, or the `--onedir`
  reasoning in RELEASING.md. Match that register; do not add narration.
- **Never claim capability that is not there.** The Acceleration tab
  distinguishes "no GPU on this machine" from "hardware ready, no kernel
  registered in this build". Docs follow the same rule: a reserved seam is
  documented as a seam.
- **Never assume capability that is not there either.** This machine has 22
  threads and NVMe; almost nobody else's does. Any default sized from
  `os.cpu_count()`, any priority above Normal, any queue depth multiplied by the
  core count is a decision about *someone else's* four-core laptop with a
  spinning disk. Ask `machine_profile.profile(root)` and size from the answer,
  cap on `seek_bound` and `low_memory`, and make the reasoning visible via
  `tuning_notes()`. Relieve memory pressure by doing less at once, never by
  silently doing a worse job — the text budget is not a tuning knob.
  `tests/test_machine_profile.py` injects the machine instead of detecting it,
  so low-end behaviour stays pinned on hardware this machine does not have.
- **User data is sacred; the index is not.** `library-index.db` in
  `%APPDATA%\Lumen Reader` is a rebuildable cache. Reading positions, notes,
  quotes, tags and `lumen-reading-marks.json` are not — they survive uninstall
  unless the user explicitly asks otherwise, and books on disk are never touched.
- **Fail-safe, never fail-open.** `preserved_user_state.json` is read by three
  programs, each with a built-in fallback. Keeping a file by mistake is
  recoverable; deleting a user's settings because JSON did not parse is not.

## Known open seams

Documented so nobody rediscovers them as bugs. All are recorded in
`LibraryEngineInLumenReader.md` ▸ *Known limits*.

- **Sharding is addressed, not stored.** `accel.shard_for` / `shard_path` are
  real and tested and `capacity_report()` prices a multi-shard index, but the
  sweep and the search use one database file. The shard count in Configuration
  is a capacity projection.
- **The search-backend preference is stored, not consumed.**
  `LibraryIndex.search()` uses SQLite FTS5 unconditionally; `accel["search"]` has
  no reader. Correct today — nothing else is registered — but unwired.
- **`result_queue_depth`** is a live `ScanConfig` knob with no control in the
  Configuration window, so saving settings resets it to auto.
- **Seek-penalty detection is Windows and Linux only.** macOS reports `unknown`,
  which is deliberately not treated as rotational; the CPU and memory guards
  still apply, but a Mac with a spinning external drive gets a wider fleet than
  it should. A RAID or storage pool of spinning disks likewise reports as one
  seek-bound volume, so Lumen sizes for one head where several exist.
- **In the frozen build, each spawned extractor walks the full GUI import path**
  (`lumen_main` → `launcher.main` → PySide6 and `ui.py`) before
  `multiprocessing.freeze_support()` exits it. Correct, but it costs a Qt import
  per worker.
- **`settings_dialog.py` has no tests.**
