# Changelog

*Lumen Book Reader · created by Angela López Mendoza · @angelahack1*

Every entry below is derived from what the annotated git tag actually contains,
not from what a release note remembered afterwards. Git history is never
rewritten in this project — see the note at the end of this file for two places
where a tag's message and a tag's contents disagree, recorded rather than
tidied away.

Versions follow [SemVer](https://semver.org/). Tags are the source of truth for
the version number; see [RELEASING.md](RELEASING.md).

---

## [Unreleased]

### Added

- **`lumen_reader/machine_profile.py`** — Lumen now asks what machine it is on
  before deciding what to run on it: logical processors (affinity-limited),
  installed memory, and whether the volume holding the library pays a seek
  penalty. Rotational media is detected with `IOCTL_STORAGE_QUERY_PROPERTY`
  against a volume handle opened with **zero** access rights, which is the only
  form of that call an unelevated user may make; the same handle yields the bus
  type, so NVMe, SATA SSD and spinning disk are distinguished in one call with
  no subprocess and no WMI. Linux reads `/sys/block/<dev>/queue/rotational`.
  Every probe degrades to "could not tell" rather than raising.
- **`ScanConfig.tuning_notes()`**, shown under the fleet summary in
  **Configuration ▸ Sweep engine** and written into the sweep log, stating which
  measurement caused each automatic decision.
- **`accel.detect_library_volume()`** — reports what the *library* volume is, as
  distinct from whether the machine has NVMe for DirectStorage.
- **`tests/test_machine_profile.py`** — 20 tests that inject the machine rather
  than detecting it, so the low-end behaviour is pinned on hardware the test
  runner does not have.

### Changed

- **The sweep is sized to the machine instead of to the developer's.** The
  defaults were one HIGH-priority extractor per logical processor and walkers at
  twice the core count — correct on a 22-thread workstation with NVMe, and
  hostile on a four-core laptop with a 7200 rpm disk, where four HIGH-priority
  processes on four cores left the Qt thread unable to repaint (indistinguishable
  from a hang) while four extractors and eight walkers thrashed a single disk
  head into reading *slower* than one worker would have.
  - A seek-bound volume now caps the fleet at 2 processes and 2 walkers.
  - At or below 8 logical processors the fleet leaves one core for the reader.
  - `priority` defaults to the new **`auto`**, which resolves to Normal at or
    below four processors or on a spinning disk, Above normal to eight, and High
    beyond that — so the workstation contract is unchanged on a workstation.
  - Under 8 GB of RAM the fleet and the in-flight result queue are capped.
    The **text budget is deliberately left alone**: memory is relieved by
    holding fewer books in flight, never by indexing less of each one.
- **An unparseable `priority` in a settings file now falls back to `auto`, not
  `high`.** The case where the settings cannot be read is exactly the case where
  the machine should be asked rather than assumed.
- Unrecognised priority levels reaching `apply_process_priority` settle at
  Normal rather than High — the fail-open version of that guard hurts the
  machine least able to absorb it.

Nothing here is automatic-only: an explicit process count, priority or walker
count is obeyed exactly, including one that is bad for the machine.

---

## [1.4.0] — 2026-08-23

Tag `v1.4.0` → `cf5a0ec`. The library engine.

### Added

- **The Turbo Sweep** (`lumen_reader/turbo_scan.py`) — a four-stage concurrent
  pipeline that indexes the library. Directory walkers, index triage, an
  extractor process fleet and the index writer all run at once, joined by
  bounded queues, so the fleet starts reading before the walk finishes and
  resident memory is set by the queue depths rather than by the size of the
  library. Configurable process count, priority, walker count, queue depths and
  batch sizes; per-worker shared-memory telemetry; pause, resume and cancel.
- **The sweep monitor** (`lumen_reader/scan_monitor.py`) — a live window with
  one tile per extractor process showing its PID, the book it is reading at
  that instant and how many it has finished, plus six headline counters, a
  throughput sparkline, an estimate, and a log. The grid reflows by window
  width and scrolls rather than hiding tiles.
- **The Configuration window** (`lumen_reader/settings_dialog.py`) — one window,
  six tabs, every setting Lumen has, on <kbd>Ctrl+,</kbd>. The library folder is
  read back from disk as you type and reports how many books are actually there;
  a folder that does not exist is refused rather than saved. Includes
  **Save and sweep now**, index optimise/compact, and forgetting libraries whose
  folder has gone.
- **The acceleration seam** (`lumen_reader/accel.py`) — background hardware
  detection (CPU topology, NVIDIA GPUs via `nvidia-smi`, the DirectStorage
  runtime, the storage bus), a backend registry for extraction and search with
  an `auto` mode that reports *which* backend won and *why*, shard addressing
  (`shard_for`, `shard_path`), and a measured index-capacity model.
- **<kbd>F5</kbd> sweeps the library folder**; <kbd>Ctrl+,</kbd> opens
  Configuration.
- **[LibraryEngineInLumenReader.md](LibraryEngineInLumenReader.md)** — the
  design document for all of the above: the pipeline, the schema, the FTS rowid
  map, search, the acceleration seam, measured numbers, and known limits.
- **This changelog**, and a project `CLAUDE.md`.

### Changed

- `library_index.py` gains sweep **generations** and pruning, per-root
  bookkeeping, a `scan_runs` history so Configuration can state what actually
  happened last time, and an `index_meta` table.
- The shelf searches the index rather than the disk, with **Titles & authors**
  and **Inside books** modes, EPUB/PDF filter chips, paging, and FTS5 query
  syntax (`"exact phrase"`, `ext:pdf`, `neural*`).
- `README.md` refactored around the library engine, with a documentation map.

### Fixed

- **Re-indexing one book no longer scans the entire full-text index.**
  `book_id` must be `UNINDEXED` in FTS5, which left
  `DELETE FROM content_fts WHERE book_id = ?` with no index and a full virtual
  table scan. Measured at **218 ms per book against 2.1 ms by rowid — 105×** on
  a 235 MB index, and worse as the index grows. A sweep of 304 changed books
  would commit seventeen and then appear to hang forever. Lumen now keeps its
  own `fts_rowid` map, built once on the first sweep after the upgrade.
- The library folder is now written by the application. `library_root` was read
  from the very first version but nothing ever wrote it, so "Rescan library"
  would faithfully sweep a folder with no books in it and report success.

---

## [1.3.0] — 2026-08-22

Tag `v1.3.0` → `eb50c11`.

### Changed

- Documentation refresh across `README.md`,
  `SpeedReadingToolInLumenReader.md`, `RELEASING.md` and
  `THIRD_PARTY_NOTICES.md`.

> **Note.** The tag's message describes the improved welcome screen, advanced
> search and advanced indexing system. That work lives in commits *after* this
> tag and is listed under **[1.4.0]** above.

---

## [1.1.0] — 2026-08-21

Tag `v1.1.0` → `e2dba68`. The release scheme.

### Added

- A complete per-user Windows release: `build.py`, `build_installer.py`,
  `build_uninstaller.py` and `build_complete_release.py`, producing a wizard,
  an uninstaller and the package they install, with SHA-256 checksums and a
  release manifest.
- `install.py` / `uninstall.py` — Tkinter wizards writing only under
  `HKEY_CURRENT_USER`: Add/Remove Programs, discovery, App Paths, the
  application entry, ProgIDs, additive extension links, Default-apps
  capabilities and the Explorer cache.
- `.epub` and `.pdf` offered as **separate tick-boxes**, with *"make Lumen the
  default"* as a separate, unticked switch.
- `register_associations.ps1` / `unregister_associations.ps1`,
  `CreateShortcut.ps1` / `RemoveShortcut.ps1`.
- `preserved_user_state.json` — one preserve list read by three programs, with
  a fail-safe built-in fallback in each.
- `lumen_reader/version.py` and `versioning.py` — one source of truth for the
  version at build time and at runtime, rendering the Win32 `VERSIONINFO`
  resource so the header badge, Explorer's Properties sheet and the Installed
  apps entry match by construction.
- `tests/test_release_scheme.py` — fails the build if the installer and
  uninstaller stop mirroring each other.

---

## [1.0.4] — 2026-08-20

Tag `v1.0.4` → `a2fb5ff`. RSVP start and end markers.

### Added

- **Point to the first word.** A session begins on exactly the word the reader
  points at, on EPUB text and the PDF selectable layer alike, instead of an
  estimate derived from scroll position. A reticle tracks the cursor, a
  **START HERE** tag labels the word under it, and <kbd>Esc</kbd> leaves without
  moving the reader's place.
- **See where you stopped.** The exact final chunk displayed is marked
  **LAST WORD READ** (or **LAST PHRASE READ**) when the session ends. The marker
  is transient and is never written into the book or into the reading marks.

---

## [1.0.2] — 2026-08-16

Tag `v1.0.2` → `0a2c3e9`.

### Changed

- GUI refinements across the reader and the Speed Reader Studio, with
  supporting tests.

---

## [1.0.0] — 2026-08-15

Tag `v1.0.0` → `aaf218d`. The first release.

### Added

- EPUB and PDF reading with a sanitized, themeable EPUB surface and
  original-page PDF rendering behind an aligned transparent text layer.
- The **RSVP Speed Reader Studio**: 80–1200 WPM, 1–5 words per fixation, a
  3 → 2 → 1 transition, optimal-recognition-point highlighting, rhythm-aware
  timing, and configurable presentation.
- Contextual definitions with an append-only, clearly labeled source ladder:
  bundled Princeton WordNet, Wiktionary, DictionaryAPI.dev, Wikipedia,
  Datamuse, optional Tlamatini Googler evidence, and optional Ollama
  contextual resolution after conventional sources miss.
- Reading memory: position restore, bookmarks, notes, quotations and tags in a
  portable `lumen-reading-marks.json` beside the library.
- Night, Paper and Sepia themes; password-protected PDFs; optional Tesseract
  OCR for image-only pages.

---

## Notes on version history

Two places where a tag's message and a tag's contents disagree. Both are
recorded here rather than corrected, because **git history in this project is
never rewritten** — no rebase, no amend, no force-push, no tag deletion.

1. **`v1.3.0`** is annotated *"Improved a lot the welcome screen empowering with
   advanced search and an advanced indexing system"*, but points at `eb50c11`,
   a documentation-only commit. The work it describes was committed afterwards
   and is listed under **[1.4.0]**.
2. **The RSVP start/end markers** shipped in **`v1.0.4`**, but `README.md` and
   `SpeedReadingToolInLumenReader.md` both introduce them as *"new in 1.1.0"*.
   `v1.1.0` is the release scheme.
