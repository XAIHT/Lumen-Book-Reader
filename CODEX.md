# CODEX project dossier

> Living engineering record for **Lumen Book Reader**
> Repository: `C:\Lumen-Book-Reader`
> Audited revision: `5db5de44a6ad` / tag `v1.5.0`
> Audit date: 2026-08-23 (`America/Mexico_City`)
> Project author and publisher: **Angela López Mendoza / @angelahack1**

This is the code-oriented memory of the project: what exists, when it arrived, how the major systems work, which details are minor but load-bearing, what degrades gracefully, and where comments or release prose differ from executable behavior. It is deliberately based on the repository and Git history, not on promotional inference.

The request that created this document asked for “at least 1,000,000 details.” A literal million-row document would be mostly repetition and would obscure the facts engineers need. This dossier therefore maximizes **verified, atomic, useful coverage** instead: every tracked file at the audited revision is inventoried, major and minor tagged changes are quantified, runtime and release paths are traced, and fallback behavior is stated explicitly. Unknown or aspirational functionality is identified as such rather than invented.

## 0.4 RSVP clicked-word launch repair — 2026-08-30

The v1.5.4 patch repairs the case where the green `START HERE` marker remained
on the selected word and the RSVP player never appeared. The exact reported
`#Annoyomics - Risto Mejide.epub` file exposed three underlying boundaries:
QtWebEngine could consume the native release event, malformed XHTML could shift
visible and RSVP token indexes, and an EPUB comment was accidentally serialized
as visible prose while remaining correctly absent from the RSVP token stream.

| File | Major/minor implementation detail | Result and fallback |
|---|---|---|
| `lumen_reader/ui.py` — browser payload | A targeting click now records the selected token, its visible DOM index, and up to six visible tokens before and after it. EPUB text nodes and PDF `.pdf-word` elements use the same bounded context contract. | Normal documents retain their exact direct-index path. Shifted documents carry enough local identity to reconcile the two token streams without changing book content. |
| `lumen_reader/ui.py` — conservative resolver | `resolve_rsvp_target_word_index` first accepts an exact index/token match. When indexes differ, it scores contiguous context around every same-token candidate and accepts only a unique best match with at least two corroborating context tokens. | Repeated words resolve to the one actually clicked. Ties, empty words, malformed indexes, insufficient evidence, and missing candidates return `None`, leaving targeting armed rather than launching at a guessed location. Legacy payloads without context retain the narrow unique-within-eight fallback. |
| `lumen_reader/ui.py` — Chromium handoff | A 60 ms timer asks the page for a recorded click only while RSVP targeting is active. The existing Qt mouse-release event filter remains the low-latency path; either path consumes the same one-shot payload. | A swallowed native release no longer strands the overlay. Cancellation and successful launch both stop the timer and remove the injected page controls before opening the player. |
| `lumen_reader/book.py` | Removes BeautifulSoup `Comment` nodes before sanitized page reconstruction and before plain-text extraction. | Publisher/editor notes can no longer be converted into visible selectable prose. The rendered first word and RSVP first word remain aligned; unsafe tags and head-metadata exclusions are unchanged. |
| `tests/test_rsvp_targeting.py` | Extends the live Chromium payload assertion, simulates a JavaScript pointer-down with no Qt release and a browser-only prefix, verifies unique shifted duplicate resolution, and verifies ambiguous context refusal. | Pins both reported root causes while protecting safe non-guess behavior and teardown. The repository now contains 372 collected tests. |
| `tests/test_epub_books.py` | Builds a synthetic EPUB chapter containing a publisher comment and verifies that neither rendered reader text nor `SpeedReadingDocument` contains it. | Prevents the exact `se usa el …` leak shown in the installed application. The repository now contains 373 collected tests. |
| `pyproject.toml` / `README.md` / `CHANGELOG.md` | Advances the forward patch identity to 1.5.4 without moving or rewriting v1.5.3. | Source metadata, public badge, release history, test inventory, and generated artifact can agree. |

Focused targeting checks passed 4/4 with one full-desktop Tlamatini Shoter
capture per test. The exact reported EPUB then passed a real `ReaderWindow` /
`SpeedReaderDialog` smoke test: clicking `cuesta` opened chapter 1 at internal
word 16 and reached the countdown stage. The affected EPUB, PDF, link-policy,
and RSVP set passed 37/37 with 37 per-test desktop captures. After the screenshot
revealed that `se` was actually publisher-comment text, the combined EPUB,
targeting, and exact-book replay passed 21/21: the comment was absent, `Te` was
the first visible token, and it launched chapter 1 at word 0/countdown. The
uninterrupted complete v1.5.4 collection then passed 373/373 with zero failures,
errors, or skips in 416.62 seconds and produced exactly 373 new full-desktop
Tlamatini Shoter captures.

## 0.3 Persistent original-file identity — 2026-08-30

The v1.5.3 patch makes a book's source file a first-class visible part of both
library and reading surfaces. Previously the library card exposed the full path
only through its hover tooltip, omitted even the short folder line for
root-level books, and the open reader showed no persistent file location at all.

| File | Major/minor implementation detail | Result and fallback |
|---|---|---|
| `lumen_reader/shelf.py` | Adds one shared `source_path_text` formatter and permanently paints `FILE · <original path-and-filename>` as the third line of every virtualized card. The formatter measures the real themed font rather than estimating characters. | A wide card shows the complete normalized path. Under pressure, Qt middle-elides the parent path while retaining the drive/root and the complete filename when they fit; the final fallback safely elides the combined prefix and filename. Root-level and nested books follow the same rule. |
| `lumen_reader/ui.py` | Replaces the single-line reader title slot with an elastic two-line identity block: chapter title above, source path below, plus a compact copy-path button. The complete path drives the clipboard, accessible name, and tooltip; only the painted representation is shortened. | The path persists for the lifetime of the open book and is cleared when returning to the library. Copy feedback changes briefly from `⧉` to `✓`. Font/style and resize events recompute elision, and the identity block collapses before mandatory controls can overlap. |
| `tests/test_shelf_ui.py` | Covers root and nested paths, full-path rendering at useful width, filename-preserving middle elision, and a live three-card shelf kept visible for desktop evidence. | Prevents regression to tooltip-only identity, root-path omission, or filename-destroying right elision. |
| `tests/test_reader_header_layout.py` | Opens a live Sepia `ReaderWindow` with the original source path at 940, 1100, 1420, and 1640 logical pixels; checks visible identity, complete accessible value, reader-to-library cleanup, pairwise geometry, and restoration of optional actions. | Search, `A−`, `A+`, theme, and Speed stay disjoint at every supported width while the source filename remains visible. |
| `pyproject.toml` / `README.md` / `CHANGELOG.md` | Advances the forward patch identity to 1.5.3 without moving or rewriting v1.5.2. | Source fallback, public badge, history, test inventory, tag, and generated release artifact can agree. |

Focused behavior checks passed 9/9, and the complete affected shelf/header set
passed 61/61 in 87.19 seconds with zero failures, errors, or skips. Both runs
produced one full-desktop Tlamatini Shoter photograph per case; the live visual
evidence shows the full path inside root and nested cards and the responsive
two-line reader identity without control overlap. The uninterrupted complete
collection then passed 369/369 with zero failures, errors, or skips in 392.311
seconds and produced exactly 369 new full-desktop photographs. A prior complete
attempt reported 365 consecutive passes before the validation host exited
without a pytest failure or final XML; its four unreported uninstall-export
safety cases passed 4/4 in isolation before the successful complete rerun.

## 0.2 Responsive reader-header repair — 2026-08-29

The v1.5.2 patch repairs the scaled Sepia reader shown with the `A−` text-size
button covering the right edge of in-book search. The failure was layout
pressure, not painting corruption: a book-controlled chapter title advertised
its entire text width while every lower-priority header action retained its
minimum width. At the logical width produced by Windows display scaling, Qt had
no valid allocation and adjacent controls intersected.

| File | Major/minor implementation detail | Result and fallback |
|---|---|---|
| `lumen_reader/ui.py` | Makes the chapter heading horizontally ignorable while reserving a useful 160-pixel reading preview; measures the real minimum width of explicit header children; progressively collapses Open Book, Mark, Notes, Definer, and Configuration only while necessary. | Search, `A−`, `A+`, theme, and Speed remain disjoint. Optional actions reappear automatically as the window grows; the decorative LUMEN brand is the final narrow-window release valve. |
| `tests/test_reader_header_layout.py` | New live `ReaderWindow` geometry regressions in Sepia at 940, 1100, 1420, and 1640 logical pixels, plus restoration coverage. | Every pair of visible header widgets must have a positive gap, the final widget must remain inside the header, and search must end before `A−` begins. |
| `pyproject.toml` / `README.md` / `CHANGELOG.md` | Advances the forward patch identity to 1.5.2 without moving or rewriting v1.5.1. | Release history remains immutable and the source, badge, changelog, and next artifact agree. |

The five exact layout cases passed twice (including the final Sepia form) with
five Shoter photographs per run. The nine-test affected set—layout, reader
search order, link policy, and the real Chromium RSVP target—passed 9/9 with
one new full-desktop Shoter photograph per case. The complete collection then
passed 365/365 with zero failures, errors, or skips in 391.322 seconds and 365
new full-desktop Shoter photographs.

## 0.1 Post-audit sweep-hang remediation — 2026-08-28

This living update records the v1.5.1 repair made after an installed sweep stopped at 800/2,881 changed books. The observed writer exception was `UnicodeEncodeError: surrogates not allowed`; the exact malformed PDF title was `001.jpg\udcc0\udc80`. Once the only writer thread exited, the bounded result queue filled, all 20 extractor processes blocked publishing, and their old telemetry incorrectly said `waiting for a book`. These are the implemented changes, not future proposals:

| File | Major/minor implementation detail | Failure now contained by |
|---|---|---|
| `lumen_reader/text_safety.py` | New shared scalar/control sanitizer, strict UTF-8 validator, and log-safe escaping. | Invalid PDF/EPUB text cannot reach Qt, JSON, SQLite, or FTS5 as a lone surrogate. |
| `lumen_reader/library_index.py` | Cleans every extracted field; rejects unknown formats; migrates and writes extended `scan_runs`; checkpoints oversized retained WAL. | Malformed document text becomes safe data; terminal failures leave durable status/counts; interrupted journals are recovered visibly. |
| `lumen_reader/turbo_scan.py` | Cancellation-aware result puts; idle/busy/publishing/stopped telemetry; fatal-stage propagation; bounded joins; per-record savepoints/fallbacks; post-commit counters. | A dead writer releases producers; one bad record cannot poison a batch; operational DB failures cannot be swallowed; displayed work equals committed work. |
| `lumen_reader/scan_monitor.py` | Publishing and failing states are visible; `READ NOW` is replaced by `INDEXED OK`; progress says committed. | The UI no longer depicts blocked publishers as idle or releases the scanner before cleanup ends. |
| `lumen_reader/settings_dialog.py` | Configuration now offers only EPUB/PDF, matching actual extractors. | MOBI/DJVU/CBZ cannot be silently routed through the EPUB parser and falsely indexed. |
| `lumen_reader/book.py` / `pdf_book.py` | Both reading surfaces use the same Unicode boundary as the index. | Renderer and index no longer disagree about which malformed scalars are safe. |
| `tests/test_library_index.py` | Exact-surrogate extraction, unsupported format, scan-history migration, and WAL recovery regressions. | The document-boundary and recovery contracts are executable. |
| `tests/test_turbo_scan.py` | Exact installed failure, data-error fallback, fatal-writer fleet release, and extension filtering regressions. | The original deadlock signature and every new shutdown/accounting invariant are pinned. |
| `pyproject.toml` / `README.md` / `CHANGELOG.md` | Patch identity advanced to 1.5.1 and its remediation is recorded separately from the immutable 1.5.0 history. | Source fallback, public badge, runtime, and release artifact agree. |

Eight initial focused cases passed visibly with one Tlamatini Shoter full-desktop photograph per case. The first complete suite passed 358/358 with 358 additional per-test photographs in 436.094 seconds. After rejected-record accounting was strengthened, five affected cases passed and the complete 359-test state passed again in 424.535 seconds with 359 new photographs. Four presentation/terminal cases then pinned incomplete-state wording, followed by three photographed version-surface cases and a photographed strict compile of every changed Python module. The current 360-test collection differs from that complete run by the one passing shelf presentation regression only. The baseline statistics below intentionally remain the immutable v1.5.0 audit snapshot; this section is the forward ledger rather than a rewritten history.

## 1. Audit baseline and repository facts

| Fact | Verified value | Engineering significance |
|---|---:|---|
| Audited branch | `main` | The audit did not switch branches or rewrite history. |
| Audited HEAD | `5db5de44a6ad` | Same commit as `v1.5.0`. |
| Latest reachable tag | `v1.5.0` | Runtime version resolution can therefore report 1.5.0 from Git. |
| Total commits at HEAD | 20 | Small history with large feature-bearing commits; file history must be read together with code. |
| Tracked files before this dossier | 87 | All 87 appear in the file ledger below. |
| Tracked bytes before this dossier | 15,914,680 | Dominated by WordNet and image assets rather than source text. |
| Runtime Python files | 22 files / 14,519 physical lines / 626,086 bytes | `lumen_reader/` plus its package entry points and generated-version resolver. |
| Root/build Python files | 11 files / 5,007 physical lines / 219,589 bytes | Launch, build, install, uninstall, version, and reindex tooling. |
| Tests | 21 files / 4,897 physical lines / 200,707 bytes | 296 statically declared `test_*` functions; parametrization produces the documented 322 collected cases. |
| Documentation | 8 files / 1,968 physical lines / 125,076 bytes | Includes `LICENSE`; excludes this new dossier. |
| PowerShell integration | 4 files / 865 physical lines / 41,291 bytes | Shortcuts and file associations, mirrored by Python fallbacks. |
| Other tracked text/data | 9 files / 1,596 physical lines / 124,574 bytes | Configuration, preserved-state fixture, and visual-test JSON. |
| Tracked binary assets | 12 files / 14,577,357 bytes | WordNet corpus, icons, screenshots, and visual-test captures. |
| Current release directory | `dist/Lumen_Release_v1.5.0` | Generated artifact, not tracked source. |
| Current unpacked release | 1,013 files / 366,562,952 bytes | PyInstaller onedir application plus installer artifacts. |
| Current release archive | `Lumen_Release_v1.5.0_win11x64_20260823_204530.zip` / 348,104,330 bytes | Distribution archive with adjacent checksum. |
| Existing unrelated untracked file | `image copy 2.png` / 10,837 bytes | Preserved untouched; it predates this dossier. |

### 1.1 Source-of-truth order

When evidence conflicts, use this order:

1. Executable code at the audited commit.
2. Tests that assert the executable contract.
3. Git tag targets and diffs.
4. Generated release manifest and checksum data.
5. `CLAUDE.md`, `CHANGELOG.md`, `RELEASING.md`, and topic documents.
6. UI prose and packaged `README-INSTALL.txt`.

This ordering matters because several prose/version records lag the implementation. Those cases are called out in §15.

## 2. Product and architecture in one view

Lumen Book Reader is a local-first Windows desktop reader written in Python and PySide6. It reads EPUB and PDF, provides RSVP speed reading, definitions, marks, full-text library indexing, and a per-user installer/uninstaller. A staged multiprocessing scanner supplies workstation-scale throughput while SQLite FTS5 remains the shipped search engine. NVIDIA/DirectStorage concepts exist as detection and backend-registration seams; the repository does not ship a GPU extraction kernel, a GPU-resident search implementation, or DirectStorage I/O calls.

```text
run_reader.py / python -m lumen_reader / frozen lumen_main.py
                         |
                         v
                  app.main + launcher
                         |
        +----------------+-------------------+
        |                |                   |
        v                v                   v
  ReaderWindow      LibraryShelf       ReaderStore / MarksStore
  ui.py              shelf.py           AppData + library-adjacent JSON
        |                |
   +----+-----+          v
   |          |     LibraryIndex (SQLite WAL + FTS5)
   v          v          ^
 EPUBBook   PDFBook      |
 book.py    pdf_book.py  +-- TurboScanJob staged pipeline
   |          |              walkers -> extractors -> triage -> writer
   |          |                         ^
   |          +-- optional OCR          |
   +-- sanitized HTML              machine_profile + accel policy seam

ReaderWindow also coordinates:
  RSVP controller -> speed_reader.py
  definitions     -> WordNet + HTTP sources + optional Googler/Ollama
  marks/search    -> exact reader position and local index
```

### 2.1 Technology inventory

| Layer | Technology | Concrete use | Absence/fallback boundary |
|---|---|---|---|
| Language | Python `>=3.10` | Runtime, build, installer, uninstaller, tests. Release environment documents Python 3.12. | No native application core is shipped. |
| Desktop UI | PySide6 `>=6.7,<7` | Widgets, WebEngine, networking, model/view shelf, dialogs, printing support. | Project rules explicitly reject PyQt6 substitutions. |
| EPUB | `zipfile`, XML, BeautifulSoup 4 | Safe archive validation/extraction, OPF/spine/nav parsing, HTML sanitization. | NCX and spine-order fallbacks cover incomplete EPUB navigation. |
| PDF | PyMuPDF / `fitz` | Page rasterization, text and word extraction, links, TOC, password authentication. | Image-only pages remain viewable; optional Tesseract supplies text when available. |
| Search | SQLite 3 FTS5 | Metadata/content indexes, BM25 ranking, snippets, paging, incremental maintenance. | Malformed FTS queries yield empty results instead of crashing. |
| Concurrency | `threading`, `multiprocessing` spawn, bounded queues | Overlapped discovery, extraction, triage, writes, telemetry sampling. | A one-process scan stays inline in a thread and avoids process IPC. |
| Definitions | NLTK WordNet, Qt network, optional subprocess/Ollama | Offline definitions first; online lexical/contextual enrichment. | Each source is time-bounded, independently retryable, and non-fatal. |
| Persistence | JSON with temp-file replace; SQLite WAL | Reader state, marks, dictionary cache, scan/search database. | Invalid JSON is ignored or defaulted; generated indexes are disposable. |
| Packaging | PyInstaller onedir | Frozen Windows application and support files. | Unused scientific/Qt modules are explicitly excluded to constrain size. |
| Installation | Tkinter + Win32 registry through Python/PowerShell | Per-user install, shortcuts, file associations, uninstall. | Python implementations cover PowerShell failure for removal paths. |
| Versioning | SemVer, Git tags, generated `_version.py`, Win32 VERSIONINFO | Runtime labels, artifact naming, executable metadata. | Resolution falls through generated value, environment, Git, then `pyproject.toml`. |
| GPU seam | `nvidia-smi` capability probe + backend protocols | Reports NVIDIA device/VRAM/compute capability and chooses registered providers. | Registries ship empty, so extraction and search select CPU/SQLite. |
| DirectStorage seam | DLL presence detection | Requires both `dstorage.dll` and `dstoragecore.dll` before advertising availability. | No DirectStorage API is called by the shipped code; ordinary filesystem I/O remains active. |
| Testing | pytest, pypdf, reportlab | Runtime, scanner, UI policy, release, and uninstall contracts. | `settings_dialog.py` has no dedicated test module. |

## 3. Tagged history: majors, minors, and measurable deltas

Lumen uses SemVer tags but all audited releases remain in major version 1. “Major” below means a feature-bearing project milestone; “minor” means refinements, fixes, documentation, or release mechanics within that milestone.

| Tag | Commit | Date | Files at tag | Delta from prior tag | Major additions | Minor/fix details |
|---|---|---:|---:|---:|---|---|
| `v1.0.0` | `aaf218d` | 2026-08-15 | 46 | Baseline | EPUB/PDF reader, RSVP, definitions, themes, marks/memory, OCR path. | Established local-first persistence and packaged WordNet. |
| `v1.0.2` | `0a2c3e9` | 2026-08-16 | 46 | 1 commit; 7 files; +246/-36 | No new subsystem. | GUI refinement and interaction fixes across the initial reader. |
| `v1.0.4` | `a2fb5ff` | 2026-08-20 | 48 | 2 commits; 14 files; +955/-476 | Exact RSVP start/end selection and return targeting. | Marks/storage safety and reader-target behavior refined. |
| `v1.1.0` | `e2dba68` | 2026-08-21 | 67 | 1 commit; 23 files; +7,269/-473 | Formal Windows release scheme: installer, uninstaller, associations, build/release/version tooling. | Per-user registration, shortcuts, checksum/manifest pipeline, release documentation. |
| `v1.3.0` | `eb50c11` | 2026-08-21 | 67 | 1 commit; 4 files; +64/-13 | Tag content is documentation-only. | Release prose announced advanced search/index work before its implementation landed; see §15. |
| `v1.4.0` | `cf5a0ec` | 2026-08-23 | 82 | 3 commits; 25 files; +9,102/-111 | Library shelf, SQLite/FTS5 engine, staged high-parallelism scanner, settings, acceleration seam, scan monitor. | Stable FTS row-ID map, pagination, machine/storage policy, large-library documentation and tests. |
| `v1.5.0` | `5db5de4` | 2026-08-23 | 87 | 4 commits; 22 files; +2,505/-148 | Machine-aware auto tuning and more resilient release/uninstall behavior. | Bounded optimize path, total-uninstall reading-state export, runner/release refinements, additional test coverage. |

Across tagged intervals after `v1.0.0`, the repository accumulated **+20,141 / -1,257 lines**. HEAD contains no commits after `v1.5.0`; the only workspace additions at audit start were an unrelated PNG.

### 3.1 Change themes by subsystem

| Subsystem | First substantial tag | Later evolution | Current audited state |
|---|---|---|---|
| Reading core | `v1.0.0` | Sanitization, link policy, exact selection targeting, search. | Mature EPUB/PDF paths with guarded rendering and cleanup. |
| RSVP | `v1.0.0` | Exact range start/end and resume highlighting in `v1.0.4`. | Sentence/chapter-safe chunking, ORP focus, punctuation timing, rest/countdown. |
| Definitions | `v1.0.0` | Contextual and phrase paths expanded. | Offline-first multi-source pipeline with optional Googler/Ollama. |
| Release engineering | `v1.1.0` | Manifests, validation, resilient uninstall, total export. | Per-user onedir distribution with signed-by-checksum artifact inventory, not code signing. |
| Library/search | Implemented for `v1.4.0` | Machine tuning and bounded maintenance in `v1.5.0`. | SQLite FTS5, incremental scans, paged UI, staged multiprocess indexing. |
| GPU/DirectStorage | `v1.4.0` seam | Capability/tuning language refined. | Detection and provider interfaces only; CPU/FTS5 is the active implementation. |
| Uninstall privacy | `v1.1.0` | `v1.5.0` total-uninstall export and broader cleanup. | Reading positions/recents/marks exported by default before user-state deletion. |

### 3.2 Exact file-level tag deltas

`A` means added and `M` means modified. No tracked file was deleted or renamed across these adjacent tagged intervals.

| Interval | Added files | Modified files |
|---|---|---|
| `v1.0.0 → v1.0.2` | None | `lumen_reader/dialog_layout.py`<br>`lumen_reader/pdf_book.py`<br>`lumen_reader/speed_reader.py`<br>`lumen_reader/ui.py`<br>`tests/test_dialog_layout.py`<br>`tests/test_pdf_books.py`<br>`tests/test_speed_reader.py` |
| `v1.0.2 → v1.0.4` | `docs/rsvp-speed-reader.png`<br>`tests/test_rsvp_targeting.py` | `README.md`<br>`SpeedReadingToolInLumenReader.md`<br>`docs/screenshot.png`<br>`lumen_reader/app.py`<br>`lumen_reader/book.py`<br>`lumen_reader/marks.py`<br>`lumen_reader/speed_reader.py`<br>`lumen_reader/storage.py`<br>`lumen_reader/ui.py`<br>`tests/test_marks.py`<br>`tests/test_safety_and_storage.py`<br>`tests/test_speed_reader.py` |
| `v1.0.4 → v1.1.0` | `CreateShortcut.ps1`<br>`RELEASING.md`<br>`RemoveShortcut.ps1`<br>`build.py`<br>`build_complete_release.py`<br>`build_installer.py`<br>`build_support.py`<br>`build_uninstaller.py`<br>`image.png`<br>`install.py`<br>`lumen_main.py`<br>`lumen_reader/launcher.py`<br>`lumen_reader/version.py`<br>`preserved_user_state.json`<br>`register_associations.ps1`<br>`tests/test_release_scheme.py`<br>`uninstall.py`<br>`unregister_associations.ps1`<br>`versioning.py` | `.gitignore`<br>`README.md`<br>`lumen_reader/__init__.py`<br>`lumen_reader/app.py` |
| `v1.1.0 → v1.3.0` | None | `README.md`<br>`RELEASING.md`<br>`SpeedReadingToolInLumenReader.md`<br>`THIRD_PARTY_NOTICES.md` |
| `v1.3.0 → v1.4.0` | `CHANGELOG.md`<br>`CLAUDE.md`<br>`LibraryEngineInLumenReader.md`<br>`lumen_reader/accel.py`<br>`lumen_reader/library_index.py`<br>`lumen_reader/scan_monitor.py`<br>`lumen_reader/settings_dialog.py`<br>`lumen_reader/shelf.py`<br>`lumen_reader/turbo_scan.py`<br>`reindex.py`<br>`tests/test_accel.py`<br>`tests/test_fts_rowid_map.py`<br>`tests/test_library_index.py`<br>`tests/test_scan_monitor.py`<br>`tests/test_turbo_scan.py` | `README.md`<br>`RELEASING.md`<br>`SpeedReadingToolInLumenReader.md`<br>`THIRD_PARTY_NOTICES.md`<br>`build_complete_release.py`<br>`install.py`<br>`lumen_reader/app.py`<br>`lumen_reader/ui.py`<br>`pyproject.toml`<br>`tests/test_shelf_ui.py` |
| `v1.4.0 → v1.5.0` | `image copy.png`<br>`lumen_reader/machine_profile.py`<br>`tests/test_index_optimize_budget.py`<br>`tests/test_machine_profile.py`<br>`tests/test_uninstall_export.py` | `CHANGELOG.md`<br>`CLAUDE.md`<br>`LibraryEngineInLumenReader.md`<br>`README.md`<br>`RELEASING.md`<br>`THIRD_PARTY_NOTICES.md`<br>`build_complete_release.py`<br>`lumen_reader/accel.py`<br>`lumen_reader/library_index.py`<br>`lumen_reader/scan_monitor.py`<br>`lumen_reader/settings_dialog.py`<br>`lumen_reader/shelf.py`<br>`lumen_reader/turbo_scan.py`<br>`pyproject.toml`<br>`tests/test_release_scheme.py`<br>`tests/test_turbo_scan.py`<br>`uninstall.py` |

These lists are path-level history. The feature meaning of each interval is in §3 and the current responsibility of every path is in §16.

## 4. Startup, ownership, and runtime lifecycle

| Detail ID | Component | Verified behavior | Fallback/safety consequence |
|---:|---|---|---|
| 4.001 | `run_reader.py` | Imports and invokes `lumen_reader.app:main`. | Minimal source-tree entry point. |
| 4.002 | `lumen_reader.__main__` | Enables `python -m lumen_reader`. | Same application path without a root script. |
| 4.003 | `lumen_main.py` | Frozen entry delegates to `launcher.main`. | Keeps packaging-specific startup out of ordinary runtime. |
| 4.004 | `launcher.py` | Calls `SetDllDirectoryW(None)` on Windows when possible. | Prevents PyInstaller DLL search path from contaminating child tools; failure is non-fatal. |
| 4.005 | `launcher.py` | Converts a book argument to an absolute path before changing cwd. | Preserves shell/file-association paths. |
| 4.006 | `launcher.py` | Changes cwd to the opened book's parent. | Relative resources/tools behave near the selected book; failure leaves existing cwd. |
| 4.007 | `app.py` | Calls `multiprocessing.freeze_support()`. | Frozen spawned workers can initialize on Windows. |
| 4.008 | `app.py` | Sets Qt organization `Lumen Reader`, application `Lumen`, and resolved version. | Stable Qt settings namespace and About/version labels. |
| 4.009 | `app.py` | Uses Qt Fusion style. | Consistent cross-machine widget baseline. |
| 4.010 | `app.py` | Starts acceleration probing in a daemon thread. | UI startup does not block on `nvidia-smi`/PowerShell/storage probing. |
| 4.011 | `app.py` | Places ordinary settings/index data in an AppData-rooted store. | Installation files stay separate from mutable user state. |
| 4.012 | `app.py` | Places `MarksStore` beside the resolved library root. | Marks travel with a library; relocation logic can recover unique filenames. |
| 4.013 | `app.py` | Resolves library location from settings/discovery/default candidates. | A fallback sample/local set is shown when a primary indexed library is unavailable. |
| 4.014 | `ui.py` | `ReaderWindow` owns active book, network requests, definition executor, scan job, and reader state. | Close handling cancels/waits/shuts down resources in a defined order. |
| 4.015 | `ui.py` | Close waits up to 15 seconds for an active sweep. | Avoids abandoning normal work indefinitely; hard process cleanup remains scanner-owned. |
| 4.016 | Attribution | Source and build metadata name Angela López Mendoza / @angelahack1. | Future documentation/build edits must preserve authorship. |

## 5. Reading engines

### 5.1 EPUB path

| Detail ID | Behavior | Implementation | Failure or fallback |
|---:|---|---|---|
| 5.001 | Archive ceiling | Rejects EPUBs whose declared aggregate uncompressed payload exceeds 512 MiB. | Prevents decompression bombs from consuming unbounded disk/memory. |
| 5.002 | Path validation | Rejects absolute paths, traversal components, colon-bearing members, and resolved escapes. | Unsafe archives fail closed before use. |
| 5.003 | Extraction lifetime | Uses `TemporaryDirectory`. | Temporary content is deleted on close; construction exceptions also clean up. |
| 5.004 | Package discovery | Reads `META-INF/container.xml` then the OPF. | Invalid required package data raises a controlled book error. |
| 5.005 | Reading order | Uses OPF spine. | Missing navigation does not destroy readable spine order. |
| 5.006 | EPUB 3 navigation | Parses the navigation document when available. | Falls back to NCX, then generated spine entries. |
| 5.007 | Active-content removal | Deletes `script`, `iframe`, `object`, `embed`, `form`, `input`, `button`, `textarea`, `select`, and `base`. | Untrusted book markup cannot run those elements. |
| 5.008 | Attribute removal | Removes event handlers plus `srcdoc` and `formaction`. | Blocks common script/navigation injection vectors. |
| 5.009 | Content policy | Injects CSP with default denial and explicit local/data allowances for required media/styles/fonts. | Unexpected network/script loading is denied. |
| 5.010 | Link handling | Web page navigation is intercepted; only the guarded Ctrl-click policy can open an external link. | Normal clicks remain inside reader navigation. |
| 5.011 | Search | Normalized chapter text is cached for in-book search. | Repeated searches avoid reparsing every chapter. |
| 5.012 | Resource URLs | Extracted local resources are resolved under the validated temp root. | Containment validation prevents book-controlled filesystem escape. |

### 5.2 PDF path

| Detail ID | Behavior | Implementation | Failure or fallback |
|---:|---|---|---|
| 5.101 | PDF engine | PyMuPDF opens, authenticates, extracts, and renders. | Invalid/password failure becomes a controlled open error. |
| 5.102 | Raster scale | Nominal 2.25× render scale with a 4096-pixel maximum edge. | Very large pages are scaled down to bound image memory. |
| 5.103 | Image output | Produces RGB page images without annotation alpha. | Predictable WebEngine-compatible raster payload. |
| 5.104 | Selection layer | Places transparent HTML words over the image from PyMuPDF coordinates. | Users retain text selection while seeing faithful raster output. |
| 5.105 | Rotation | Transforms word rectangles for rotated pages. | Selection remains aligned for common rotated documents. |
| 5.106 | Browser fit | Uses `scaleX` correction for text-layer width. | Reduces visual/text geometry drift. |
| 5.107 | Cache | Caches page image, extracted text, and word geometry under a temporary directory. | Avoids repeated rendering; close removes the cache. |
| 5.108 | TOC | Uses document TOC when valid. | Documents below 60 pages get page entries; larger ones get 25-page groups. |
| 5.109 | Native text | Extracts page text directly when present. | This is always preferred over OCR. |
| 5.110 | OCR | Calls Tesseract only if discoverable on `PATH`, using roughly 150 dpi English OCR. | Missing/failing OCR does not prevent page display. |
| 5.111 | Image-only notice | A page with no extractable/OCR text still renders with an explanatory state. | Viewing is not coupled to text extraction success. |
| 5.112 | Unicode hygiene | Cleans invalid scalar/control content before HTML/JSON use. | Corrupt text cannot poison the rendering pipeline. |

## 6. RSVP speed-reading engine

| Detail ID | Parameter/contract | Audited value or rule | Why it matters |
|---:|---|---|---|
| 6.001 | Default speed | 300 WPM | Conservative usable entry point. |
| 6.002 | Speed range | 80–1,200 WPM | Hard UI/runtime bound. |
| 6.003 | Default chunk | 1 word | Precision-first reading. |
| 6.004 | Chunk range | 1–5 words | Prevents pathological display groups. |
| 6.005 | Default face/size | Segoe UI, 68 px | Windows-readable baseline. |
| 6.006 | Font range | 28–144 px | Bounded accessibility control. |
| 6.007 | Default colors | `#050709` background, `#76ffb2` text, `#ffd166` focus | High-contrast visual rhythm. |
| 6.008 | ORP focus | Enabled by default | Highlights the optimal recognition point. |
| 6.009 | Guide marks | Enabled by default | Anchors the focus position spatially. |
| 6.010 | Blank interval | 12%; configurable 0–40% | Gives perceptual separation without changing semantic position. |
| 6.011 | Clause pause | 1.35×; range 1–3× | Slows at clause punctuation. |
| 6.012 | Sentence pause | 1.85×; range 1–4× | Slows at sentence boundaries. |
| 6.013 | Long-word delay | +12 ms/character beyond 8; range 0–60 | Adds lexical processing time. |
| 6.014 | Countdown | Default 3 seconds; clamped 3–10 | Prevents an accidentally invisible start. |
| 6.015 | Rest cadence | Default 10 minutes; range 0–60 | Optional eye/cognitive break scheduling. |
| 6.016 | Fullscreen/minimal | Both enabled by default | Removes competing chrome while playing. |
| 6.017 | Boundary rule | A chunk never crosses sentence or chapter boundaries. | Timing and resume positions remain semantically stable. |
| 6.018 | Timing | `60000 / WPM × words`, then punctuation/long-word multipliers. | Word count, not chunk count, drives speed. |
| 6.019 | Timer floors | 40 ms total interval; 30 ms visible interval | Avoids zero/negative or unrenderable flashes. |
| 6.020 | ORP index | 0/1/2/3/4 selected from word-length bands | Focus moves deeper into longer tokens. |
| 6.021 | Keyboard | Space play/pause; left/right ±10 seconds; up/down ±25 WPM. | Full operation without pointer travel. |
| 6.022 | Seek estimate | Ten seconds maps through current `WPM / 6`. | Time seek remains speed-aware. |
| 6.023 | Countdown integrity | The 3→2→1 countdown is not skipped by an early timer event. | First word is not flashed prematurely. |
| 6.024 | Exact targeting | Stores the exact last-presented tuple and reader range. | Closing RSVP can return to and highlight the true source location. |

## 7. Definition pipeline

| Stage | Provider/technology | Trigger and limits | Failure behavior |
|---|---|---|---|
| Normalize | Local validation | Word ≤64 characters; phrase ≤180 characters and 2–24 tokens. | Invalid/oversized input is rejected before network/process work. |
| Immediate offline | Bundled NLTK WordNet | First lexical source; packaged corpus avoids download. | A miss continues to online sources. |
| Word online | DictionaryAPI + Wiktionary | QNetworkAccessManager, bounded session. | 404 completes the source; transient errors retry if time remains. |
| Phrase online | Wikipedia + Datamuse | Wikipedia requires an exact usable page; Datamuse requires strict evidence. | Missing/disambiguation/no evidence is treated as a clean miss. |
| Retry schedule | Qt timers | Approximately 650, 1,400, and 2,600 ms within a 20-second session. | Requests never retry indefinitely. |
| Context escalation | Local coordinator | Begins after about 1.8 seconds when ordinary sources have not produced a usable result. | Ordinary source cards can still arrive independently. |
| Googler | Optional `C:\Tlamatini\agents\googler\googler.py` | Auto-enabled only when present; isolated subprocess; ~12-second total budget. | Playwright Google falls back to DuckDuckGo; any failure returns no references. |
| Ollama | Optional local HTTP | Disabled by default; `127.0.0.1:11434`, model `glm-5.2:cloud`; context ~1,600–1,800 chars. | Connection/model/JSON failure becomes a source miss, not a reader failure. |
| Ollama payload | Lexicographer prompt | Temperature 0, non-streaming, `think=false`; local models request JSON format while cloud model omits it. | Parser still validates the response structure. |
| Context result | Validated JSON | Definition ≤900 chars; up to 6 synonyms, each ≤80 chars. | Invalid or empty responses are discarded. |
| Cache | Atomic local JSON | At most 2,500 entries; caches stable DictionaryAPI/Wiktionary/WordNet results. | Corrupt cache can be replaced; volatile contextual output is not treated as canonical. |
| Cancellation | Futures + network replies | New lookup and window close cancel outstanding work. | Stale cards do not overwrite the current selection. |
| Empty result | UI state | Explicit full-search failure message after all usable paths end. | User sees a result boundary rather than a silent spinner. |

## 8. Library database and FTS5 design

### 8.1 Schema and connection policy

| Detail ID | Area | Verified implementation | Consequence/fallback |
|---:|---|---|---|
| 8.001 | `books` identity | Stable integer `id`; unique path plus root/name/ext/size/mtime metadata. | Incremental change detection avoids re-extracting unchanged books. |
| 8.002 | Metadata | Title, author, publisher, language, subjects, description, page count, text/error status. | Shelf and metadata search function even when content extraction fails. |
| 8.003 | Generations | `seen_gen` plus per-root scan generations. | Successful full sweeps prune unseen stale rows without holding two full datasets. |
| 8.004 | Run history | Keeps the latest 40 scan-run records per root. | Diagnostics remain bounded. |
| 8.005 | Metadata FTS | `title author name subjects publisher book_id UNINDEXED`. | Lightweight title/author/name search. |
| 8.006 | Content FTS | `body book_id UNINDEXED`. | Full-text search is physically separate from metadata search. |
| 8.007 | Tokenizer | `unicode61 remove_diacritics 2`. | Case/diacritic-friendly Unicode tokenization. |
| 8.008 | Stable map | `fts_rowid(book_id PRIMARY KEY, meta_row, content_row)`. | Direct row deletion avoids scanning a huge FTS table. |
| 8.009 | Map version | Stored in `index_meta` under an FTS-map key. | One-time compatibility rebuild can be detected. |
| 8.010 | Measured rationale | Documented test: 218 ms scan-delete vs 2.1 ms mapped-delete on 235 MiB, about 105×. | Mapping targets the real large-index deletion bottleneck. |
| 8.011 | Journal | SQLite WAL. | Readers/triage can overlap the writer. |
| 8.012 | Durability mode | `synchronous=NORMAL`. | Balances local generated-index durability and throughput. |
| 8.013 | Temp storage | `temp_store=MEMORY`. | SQLite temp work favors RAM where available. |
| 8.014 | Page cache | `cache_size=-131072`, approximately 128 MiB. | Database cache is explicitly bounded in KiB form. |
| 8.015 | WAL checkpoint | Autocheckpoint 2,000 pages, roughly 8 MiB at 4 KiB/page. | WAL growth is periodically folded without per-row synchronization. |
| 8.016 | Journal size limit | 256 MiB. | Prevents indefinite retained journal growth. |
| 8.017 | Extraction budget | Default 250,000 characters/book. | Whole-book search detail is bounded before IPC/database insertion. |
| 8.018 | PDF empty prefix | Stops after 24 leading empty pages if no text chunks were found. | Image-only/corrupt PDFs cannot consume the entire extraction budget blindly. |
| 8.019 | Search ranking | FTS5 BM25 plus snippets around 18 terms. | Ranked pages return compact context. |
| 8.020 | Search modes | Metadata, content, or all; SQL extension filters; limit/offset. | Shelf pagination does not materialize the full result set. |
| 8.021 | Hostile FTS syntax | SQLite operational query errors return `[]`/`0`. | User input cannot crash the shelf. |
| 8.022 | Corrupt book | Writes an `ok=false` row with an error where possible. | One unreadable file is visible/diagnosable and does not stop a sweep. |
| 8.023 | EPUB indexing | Reads central directory, OPF, and spine directly without extracting the archive. | Lower disk churn and faster bulk indexing. |
| 8.024 | PDF indexing | Uses PyMuPDF native extraction. | OCR is a reader-view fallback, not a bulk-index default. |
| 8.025 | Directory walk | Explicit `scandir` stack. | Avoids recursive Python call depth; inaccessible entries are skipped. |
| 8.026 | Default location | AppData-backed index. | Database is user-local, disposable, and separate from books. |

### 8.2 Optimize and maintenance policy

| Path | Condition | Work performed | Safety boundary |
|---|---|---|---|
| Roomy full path | Known free space is at least `database_size + 4 GiB` | Full FTS `optimize`, then `VACUUM`. | Extra-space test tries to avoid a full-copy disk exhaustion. |
| Constrained path | Known free space is below that threshold | Bounded FTS merges: 256 pages × up to 8 rounds/table, with passive checkpoints. | Work has an upper bound instead of creating a large transient copy. |
| Final WAL step | All paths | Truncate checkpoint. | Reclaims WAL tail after maintenance. |
| Vacuum isolation | Full path | Executes with `isolation_level=None`. | Satisfies SQLite VACUUM transaction requirements. |
| Unknown-space nuance | Free-space probe returns `-1` | Code skips the full FTS merge condition (`free >= 0`) but still considers `roomy` true for `VACUUM`. | This differs from the docstring's “cheap path” implication and should be corrected; see §15. |

## 9. High-parallelism scanner: actual HPC design

“HPC” here means a machine-aware, high-throughput local pipeline using CPU processes, threads, bounded queues, WAL concurrency, batching, and compact shared telemetry. It does **not** mean MPI, a cluster scheduler, distributed storage, CUDA kernels, RDMA, or a remote supercomputer.

### 9.1 Stage topology

| Stage | Concurrency primitive | Input | Output | Backpressure/error boundary |
|---|---|---|---|---|
| Conductor | 1 daemon thread | Scan settings/root | Lifecycle coordination | Owns cancellation, stage closure, and final status. |
| Discovery | Configurable walker threads | Filesystem directories | Candidate paths/jobs | Bounded walk/job queues limit discovery lead. |
| Extraction | Spawned processes, or inline thread for one worker | Book jobs | Whole-book result records | Per-book exceptions become failed records; process isolation contains parser faults. |
| Triage | 1 reader thread + SQLite connection | Candidate metadata and extractor context | Touch/update work | Reads concurrently under WAL; batches up to 512 with ~0.15 s hold ceiling. |
| Writer | 1 thread, sole DB writer | Touches and extracted records | SQLite commits/FTS rows | Commits by batch threshold or ~1.5 s age; never blocks waiting for results. |
| Sampler | 1 thread at ~4 Hz | Shared counters/paths | `ScanSnapshot` history and UI metrics | Telemetry is intentionally lossy and never controls correctness. |
| UI monitor | Qt timer ~250 ms | Snapshots | Core grid, rates, sparkline, messages | UI can pause/resume/cancel without owning worker internals. |

### 9.2 Automatic sizing rules

| Resource | Automatic rule | Explicit/manual behavior | Pressure fallback |
|---|---|---|---|
| Windows processes | Hard maximum 61 to stay below handle/wait constraints. | User override honored within safety bounds. | Tight memory (<4 GiB) caps at 2; low memory (<8 GiB) caps at 4. |
| Seek-bound disk | 2 extraction processes. | Override remains possible. | Avoids turning HDD/removable access into random-seek collapse. |
| Network storage | Up to 8, at least 2 and no more than logical CPUs. | Override remains possible. | Conservative versus local NVMe/SSD. |
| CPU ≤8 | Leaves roughly one logical CPU free. | Manual can choose a different bounded value. | RAM caps still win. |
| CPU >8 | Can use all available logical CPUs. | Manual value capped by platform maximum. | Priority and queue sizing keep UI responsive. |
| Walkers | Seek-bound: 2; otherwise approximately 2× CPU, bounded 4–64. | Explicit value bounded to 256. | Separate from extractor count so traversal can feed workers. |
| Walk queue | Default about 20,000 entries. | Tunable setting. | Bounded queue prevents path discovery from consuming unbounded RAM. |
| Job queue | About processes ×64. | Derived from chosen process count. | Extractors throttle producers naturally. |
| Result queue | Tight RAM: ×8; low RAM: ×16; normal: ×64. | Result depth has no direct settings-dialog control. | Whole-text results are the dominant IPC memory risk. |
| Triage batch | Default 512, minimum 16. | Tunable. | Time ceiling prevents sparse scans waiting forever. |
| Write batch | Default 400, minimum 16. | Tunable. | Age-based commit keeps low-volume progress durable. |

### 9.3 Priority and process behavior

| Detail ID | Mechanism | Verified behavior | Fallback |
|---:|---|---|---|
| 9.301 | Start method | Uses multiprocessing `spawn`. | Compatible with Windows/frozen apps; requires import-safe entry points. |
| 9.302 | Single-worker mode | Executes extraction inline in a thread, with no process IPC. | Small scans/machines avoid spawn overhead. |
| 9.303 | Worker mode | Multiple spawned daemon processes. | Conductor retains cleanup authority. |
| 9.304 | Priority names | idle, below, normal, above, high, realtime. | Invalid values normalize rather than reaching platform calls. |
| 9.305 | Auto priority | Normal for seek-bound storage or ≤4 CPUs; above for 5–8; high above 8. | Realtime is never selected automatically. |
| 9.306 | Windows setting | Applies a step-down ladder from requested class and reads back actual class. | Permission failure lowers priority; it never escalates above the requested class. |
| 9.307 | POSIX setting | Best-effort niceness. | Failure reports/uses normal behavior. |
| 9.308 | Walker boost | Best-effort Windows thread boost for traversal. | Failure does not stop walking. |
| 9.309 | Cancellation grace | Ordinary shutdown up to 900 s; cancellation grace around 25 s. | Remaining processes are terminated after grace and missing books can be found next sweep. |
| 9.310 | Pruning on cancel | Generation pruning is skipped for a cancelled scan. | Partial scans cannot erase unseen library records. |
| 9.311 | Tuning notes | Effective process/queue/storage decisions are logged and shown. | Users can see why “auto” made a conservative choice. |

### 9.4 Telemetry and memory layout

| Detail ID | Structure | Size/bound | Correctness role |
|---:|---|---:|---|
| 9.401 | Per-worker numeric slot | Six signed 64-bit values: PID, state, done, failed, bytes, start-ms. | Display only; lock-free races may produce a transient mixed snapshot. |
| 9.402 | Per-worker path slot | 512 bytes. | Truncated display label only, never the authoritative work item. |
| 9.403 | Snapshot history | 240 samples maximum. | Bounds trend memory. |
| 9.404 | Message history | 400 messages maximum. | Bounds diagnostic UI memory. |
| 9.405 | Rate smoothing | EMA uses ~0.7 previous + 0.3 current. | Reduces jitter without affecting work scheduling. |
| 9.406 | Result payload | Whole extracted text up to the per-book text budget. | Queue depth, not a smaller text budget, is the RAM pressure valve. |
| 9.407 | Touch queue | Unbounded lightweight path/ID records. | Potential growth exists, but payloads are much smaller than extracted text. |
| 9.408 | Inline result queue | Unbounded in one-worker mode. | Only one producer exists, reducing but not eliminating accumulation risk. |

## 10. GPU, DirectStorage, sharding, and acceleration truth table

| Capability | Detection/contract present | Active shipped implementation | Exact fallback |
|---|---|---|---|
| NVIDIA discovery | Yes: `nvidia-smi` on `PATH`, 4-second timeout, queries name, VRAM, compute capability, driver. | Probe/report only. | Missing command, timeout, parse failure, or zero/invalid VRAM produces “no usable NVIDIA GPU.” |
| CUDA extraction | Backend protocol and registry exist. | **No registered CUDA extraction kernel ships in this repository.** | `auto` and unavailable forced choices use `cpu-fleet`. |
| GPU-resident search | Search protocol and registry exist. | **No registered GPU search backend ships.** | SQLite FTS5 is selected. |
| DirectStorage | Availability gate requires Windows and both `dstorage.dll` + `dstoragecore.dll` in searched locations. | **No DirectStorage API/file-read path is implemented.** Detection is file-presence based, not an end-to-end I/O proof. | Standard Python/OS filesystem reads. |
| Storage type | Machine profile and PowerShell/Win32 probes recognize NVMe/SSD/HDD/network/removable/unknown. | Used for CPU/process/queue policy. | Unknown storage uses conservative/general rules, not DirectStorage. |
| GPU extraction eligibility | Requires usable CUDA GPU, DirectStorage runtime, NVMe, and a registered kernel. | Conditions cannot all be satisfied by stock repository because registry is empty. | CPU staged scanner. |
| GPU search eligibility | Requires usable GPU and registered search backend. | Registry empty. | SQLite FTS5. |
| Extraction preference | Names include `auto`, `cpu-fleet`, `gpu-directstorage`. | Selection report can explain an unavailable requested provider. | Falls back to CPU without crashing settings/sweep. |
| Search preference | Names include `auto`, `sqlite-fts5`, `gpu-resident`. | Preference is persisted by settings, but `LibraryIndex` does not consume it. | Search remains SQLite FTS5. |
| Stable sharding | FNV-1a mapping and shard filename convention `.0000of000N` exist. | Current library uses a single SQLite database; no sharded orchestration/storage is wired. | One database, one writer. |
| Capacity estimator | Assumes ~1,200 B metadata/book and FTS ≈1.15× indexed text; targets ≤2 TiB or ≤250M rows/shard. | Planning/diagnostic calculation only. | Does not move or partition current data. |
| Published scale example | Documentation cites 27,956 books / 7.79 GiB and theoretical multi-shard scale. | Evidence for sizing language, not proof of a GPU/distributed engine. | Actual operational limits remain local disk, RAM, SQLite, and extraction throughput. |

### 10.1 Machine-profile detail

| Signal | Windows | Linux/other | Conservative result |
|---|---|---|---|
| Logical CPUs | Affinity-aware where `sched_getaffinity` exists; otherwise `os.cpu_count()`, then 4. | Same generic path. | Never assumes zero workers. |
| Total RAM | `GlobalMemoryStatusEx` via `ctypes`. | `sysconf` page count × page size where available. | Returns 0 when unknown, allowing non-memory-specific defaults. |
| Volume class | UNC/network/removable checks plus Win32 volume query for bus type and seek penalty. | `/sys/dev/block/.../queue/rotational` on Linux. | Unknown on unsupported/failed probes. |
| NVMe | Bus type `0x11`. | Non-rotational classification does not necessarily distinguish NVMe from SSD. | Treated through the general storage profile. |
| Removable | USB `0x07`, SD `0x0C`, MMC `0x0D`, plus drive type. | Platform-specific evidence may be absent. | Conservative seek/removable policy. |
| Probe caching | Per-volume profile cache. | Same abstraction. | Repeated dialog/scan calls avoid repeated expensive probes. |
| Human tier | modest / standard / workstation. | Derived descriptively from CPU/RAM/storage. | Tier labels do not override explicit settings. |

## 11. Memory, disk, state, and cleanup engineering

### 11.1 Memory budgets and bounded collections

| Detail ID | Owner | Bound/strategy | What happens at or beyond the boundary |
|---:|---|---|---|
| 11.001 | EPUB loader | 512 MiB total declared uncompressed archive cap. | Book open fails before full extraction. |
| 11.002 | PDF renderer | 2.25× nominal raster, maximum edge 4,096 px. | Scale is reduced for oversized pages. |
| 11.003 | Index extractor | 250,000 characters/book default. | Remaining book text is not indexed in that sweep result. |
| 11.004 | PDF index extractor | 24 leading empty pages when no content found. | Stops fruitless native-text scanning early. |
| 11.005 | SQLite cache | About 128 MiB. | SQLite evicts pages through its own cache policy. |
| 11.006 | Result IPC | Queue slots scale ×8/×16/×64 by RAM tier. | Producer blocks when the bounded queue is full. |
| 11.007 | Walk/job IPC | About 20,000 paths and workers×64 jobs. | Discovery/triage backpressure prevents unlimited lead. |
| 11.008 | Dictionary cache | 2,500 entries. | Old/excess entries are pruned by the cache policy. |
| 11.009 | Recent books | 8 entries. | Older history drops from the quick list. |
| 11.010 | Mark tags | 20 tags/mark. | Excess input is not persisted. |
| 11.011 | Mark quote | 1,000 characters. | Stored quote is truncated/bounded. |
| 11.012 | Scan history | 240 snapshots and 400 messages. | Old telemetry rolls off. |
| 11.013 | Scan runs | 40 records/root. | Older database diagnostic runs are deleted. |
| 11.014 | Context input | Roughly 1,600–1,800 characters. | Smart-definition prompt context is clipped. |
| 11.015 | Context output | Definition 900 chars; six synonyms ×80 chars. | Oversized generated content is rejected or constrained. |
| 11.016 | Search results | SQL `LIMIT/OFFSET`; shelf holds one page. | UI does not allocate the full catalog/result set. |
| 11.017 | FTS maintenance | 256 pages ×8 merge rounds/table on constrained disks. | Optimize work remains bounded. |
| 11.018 | PyInstaller footprint | Explicit exclusions for torch/TensorFlow/NumPy/Pandas/SciPy/sklearn and unused Qt modules. | Release avoids dragging unrelated scientific runtimes into RAM/disk footprint. |

### 11.2 Ownership and cleanup table

| Resource | Created by | Normal cleanup | Exceptional/cancel cleanup |
|---|---|---|---|
| EPUB temp tree | `EPUBBook` | `close()`/temporary-directory cleanup. | Constructor errors explicitly clean the temporary root. |
| PDF raster/text cache | `PDFBook` | `close()` removes temporary directory and closes document. | Open/render errors release partial state. |
| Dictionary cache temp | Dictionary cache writer | Atomic `replace` promotes completed JSON. | Interrupted `.tmp` does not replace the last good file. |
| Reader-state temp | `ReaderStore` | Atomic `replace`. | Invalid JSON defaults rather than blocking startup. |
| Marks temp | `MarksStore` | Atomic `replace`. | Invalid rows are skipped; valid rows remain usable. |
| SQLite connections | Library index / scanner stages | Each thread owns/closes its own connection. | Worker/stage finalizers close on exit; generated DB can be rebuilt. |
| Scanner children | `TurboScanJob` | Join during graceful completion. | After grace, remaining processes are terminated. |
| Network replies | Definition session | Deleted/cancelled when source/session completes. | New lookup or close aborts stale requests. |
| Definition futures | Reader window executor | Executor shutdown on window close. | Cancellation is requested; late results are session-guarded. |
| Uninstall export `.part` | Uninstaller | Flushed, `fsync`ed, atomically renamed, read back, validated. | Failed validation stops destructive total-uninstall progress. |
| Release staging | Build support | Verified move into final artifact path. | Retry/cleanup handles transient Windows locks; source/user data are outside target. |

### 11.3 Persistence map

| State | Typical location | Format | Relocation/recovery | Uninstall treatment |
|---|---|---|---|---|
| Reader settings/position/recent | User AppData under Lumen Reader | JSON | Invalid file falls back to defaults; recent path can relink by unique filename. | Total uninstall exports positions/recent, then deletes AppData unless `/NOSAVE`. |
| Marks | Beside library root | `lumen-reading-marks.json`, versioned JSON | Relinks only when exactly one matching filename exists; ambiguous matches are not guessed. | Total uninstall discovers/deduplicates marks and includes them in export. |
| Dictionary cache | User-local state | JSON | Atomic replacement; disposable enrichment cache. | Deleted with application user state in total mode. |
| Library index | AppData | SQLite WAL/FTS5 | Entirely rebuildable from books; incremental size+mtime updates. | Deleted as generated/cache state. |
| Books | User-selected library | EPUB/PDF files | Never rewritten by reader/indexer. | External library is not deleted; an install-local top-level `library` is explicitly preserved by code. |
| Install manifest | Install directory | JSON | Used to validate target and unregister artifacts. | Removed with application files after cleanup. |
| Release manifest | Release directory/archive | JSON + SHA-256 inventory | Build validates/copies it. | Distribution metadata, not user state. |
| Total-uninstall export | Desktop or `/SAVETO` path | Date-stamped JSON | Unique filename, atomic write, readback validation. | Explicitly preserved even if destination overlaps a cleanup tree. |

## 12. Comprehensive fallback and safety matrix

The table is intentionally granular. A fallback is not merely “catch exception”: it states what remains usable and which data is authoritative afterward.

| ID | Trigger | Primary path | Fallback/recovery | Data-loss posture |
|---:|---|---|---|---|
| F001 | No command-line entry helper | `run_reader.py` | `python -m lumen_reader` or frozen launcher. | None. |
| F002 | Windows DLL path reset fails | Clear PyInstaller DLL directory. | Continue with inherited DLL search state. | None; child-tool compatibility may differ. |
| F003 | Cannot change cwd to book folder | Anchor to book parent. | Keep current cwd with absolute book argument. | None. |
| F004 | Acceleration probe is slow | Background daemon probe. | UI opens before result. | None. |
| F005 | No configured library | Configured root. | Discovery/default/sample list, up to 60 local fallback books. | No book mutation. |
| F006 | EPUB too large | Extract validated archive. | Reject above 512 MiB declared uncompressed size. | Source untouched. |
| F007 | EPUB path escape | Resolve archive member under temp root. | Reject archive. | Source untouched; temp removed. |
| F008 | EPUB 3 nav missing | Parse navigation document. | NCX, then spine-generated TOC. | Reading order retained. |
| F009 | EPUB has active content | Render chapter HTML. | Strip active elements/attributes and inject CSP. | Content presentation reduced; security preserved. |
| F010 | External link clicked normally | Guarded Ctrl-click policy. | Keep navigation inside reader. | None. |
| F011 | EPUB construction fails | Prepared temp directory. | Cleanup before propagating controlled error. | No residue. |
| F012 | PDF requires password | Authenticate. | Request/handle password failure as open error. | Source untouched. |
| F013 | PDF page is huge | 2.25× raster. | Reduce scale to 4,096-pixel maximum edge. | Visual resolution bounded, page still visible. |
| F014 | PDF TOC missing | Document TOC. | Per-page TOC under 60 pages; 25-page groups otherwise. | Navigation remains available. |
| F015 | PDF has no native text | PyMuPDF extraction. | Optional Tesseract OCR. | Page remains viewable if OCR is absent/fails. |
| F016 | Tesseract absent | OCR path. | Render page and show text-unavailable state. | No crash, no fabricated text. |
| F017 | Invalid PDF Unicode | Direct text. | Clean invalid scalars/control data. | Only unusable code points are removed. |
| F018 | PDF/EPUB closes | Cached temp data. | Delete cache/extraction tree. | Source preserved. |
| F019 | Invalid reader-state JSON | Load settings/history. | Return defaults. | Corrupt state ignored; books untouched. |
| F020 | Reader-state save interrupted | Direct overwrite. | Write temp then atomic replace. | Previous good state survives. |
| F021 | Recent book moved | Stored absolute path. | Relink only unique same filename. | No ambiguous guess. |
| F022 | Invalid marks JSON row | Load all marks. | Skip invalid row, retain valid rows. | Partial recovery. |
| F023 | Marked book moved | Stored source path. | Unique-filename relink only. | Ambiguity remains visible/unresolved. |
| F024 | Dictionary cache invalid | Load cache. | Empty/rebuilt cache. | Definitions can still query sources. |
| F025 | WordNet misses | Offline definition. | Online lexical providers. | No false definition. |
| F026 | DictionaryAPI 404 | Word lookup. | Mark source complete and continue others. | No repeated pointless retries. |
| F027 | Transient definition network error | Online source. | Retry at bounded schedule within 20 s. | UI remains responsive. |
| F028 | Wikipedia page missing/disambiguated | Phrase definition. | Other phrase/context sources. | Rejects weak evidence. |
| F029 | Datamuse evidence weak | Phrase lexical evidence. | Treat as miss. | Avoids presenting unrelated terms. |
| F030 | Googler script absent | Optional reference search. | Feature remains disabled. | Core definitions unaffected. |
| F031 | Google automation fails | Playwright Google. | DuckDuckGo attempt, then empty references. | No browser failure reaches reader. |
| F032 | Ollama disabled/unreachable | Contextual definition. | Other sources; eventual explicit no-result. | No dependency on local model server. |
| F033 | Ollama response not valid JSON | Structured contextual result. | Discard response. | Does not display malformed generation. |
| F034 | Lookup selection changes | Existing futures/network replies. | Cancel and session-gate late results. | Current selection stays authoritative. |
| F035 | All definition sources miss | Definition cards. | Explicit comprehensive-search failure. | No infinite spinner. |
| F036 | RSVP chunk approaches sentence end | Fixed chunk width. | Shorten chunk at sentence boundary. | Position remains exact. |
| F037 | RSVP closes after playback | Approximate reader offset. | Use last-presented exact tuple/range. | Resume highlight maps to actual text. |
| F038 | RSVP timer interval too small | Computed WPM timing. | Enforce 40 ms total / 30 ms visible floors. | UI remains schedulable. |
| F039 | Index root entry inaccessible | Walk directory. | Skip entry and continue. | Other books still index. |
| F040 | Book parser fails in sweep | Extract metadata/content. | Write failed result/error and continue. | One file cannot abort catalog. |
| F041 | Book unchanged | Re-extract. | Touch generation using size+mtime match. | Existing indexed text retained. |
| F042 | Sweep cancelled | End-of-generation prune. | Skip prune. | Existing unseen rows are never deleted by a partial sweep. |
| F043 | FTS row-ID map absent | Direct mapped delete. | Correct but slower scan/delete; attempt one-time rebuild. | Correctness before speed. |
| F044 | FTS map rebuild fails | Fast delete map. | Log and continue slow/correct path. | Search data remains consistent. |
| F045 | User enters malformed FTS query | Execute MATCH expression. | Return zero results/count. | Shelf remains usable. |
| F046 | Search result set is enormous | Materialize all rows. | SQL paging plus one-page model. | Memory stays bounded. |
| F047 | Disk lacks optimize headroom | Full optimize/VACUUM. | Bounded merge + checkpoint. | Avoids full-copy disk pressure. |
| F048 | Free-space probe unknown | Choose maintenance path. | Current code skips full FTS merge but may still VACUUM; flagged in §15. | Potential disk-risk discrepancy, not silent fiction. |
| F049 | HDD/removable library | Maximum parallel extraction. | Auto-select 2 processes and 2 walkers. | Avoids seek thrash. |
| F050 | Network library | Local-NVMe tuning. | Cap automatic process count around 8. | Limits network pressure. |
| F051 | RAM <4 GiB | Normal queue/process sizing. | Cap processes at 2, result depth ×8. | Reduced throughput for bounded memory. |
| F052 | RAM <8 GiB | Normal queue/process sizing. | Cap processes at 4, result depth ×16. | Reduced throughput for bounded memory. |
| F053 | Windows worker count high | Arbitrary process count. | Hard cap 61. | Avoids platform wait-handle failure. |
| F054 | Only one extractor selected | Spawn process fleet. | Inline extraction thread/no IPC. | Lower overhead. |
| F055 | Producer outruns extractor/writer | Unbounded heavy payloads. | Bounded queues block producers. | Memory pressure is controlled. |
| F056 | Worker book extraction throws | Process work item. | Emit failed record and continue. | Failure isolated to book. |
| F057 | Worker fails to exit normally | Graceful join. | Terminate after grace; later sweep recovers missing work. | DB generation safety prevents mass loss. |
| F058 | Requested high/realtime priority denied | Apply requested class. | Step down and read actual priority. | Scan continues without privilege. |
| F059 | POSIX nice fails | Apply priority. | Continue at normal effective priority. | No correctness impact. |
| F060 | Telemetry races | Lock every update. | Accept transient lock-free display inconsistency. | Metrics only; database correctness unaffected. |
| F061 | NVIDIA command missing | GPU probe. | Report unavailable. | CPU path remains complete. |
| F062 | `nvidia-smi` hangs | GPU probe. | Kill/timeout after ~4 s and report unavailable. | Startup remains responsive. |
| F063 | Invalid GPU VRAM/capability output | Parse probe. | Treat device as unusable. | No unsafe GPU selection. |
| F064 | One DirectStorage DLL missing | DirectStorage availability. | Report unavailable. | Ordinary file I/O. |
| F065 | DLLs present but no kernel registered | GPU+DirectStorage extraction. | CPU fleet. | Feature is not falsely executed. |
| F066 | Forced GPU extraction unavailable | Honor preference. | Explain/fall back to CPU. | Sweep still completes. |
| F067 | Forced GPU search unavailable | Honor preference. | Explain/fall back to SQLite FTS5. | Search still works. |
| F068 | Search backend setting is stored | Switch engine. | Current index ignores it and stays FTS5; flagged. | Stable current behavior. |
| F069 | Capacity estimator suggests shards | Oversized theoretical catalog. | Current single-DB implementation. | No automatic data migration. |
| F070 | Settings probe is slow | Synchronous indefinite probe. | Time-bounded/background-friendly probes. | Dialog remains operable. |
| F071 | Shelf has no index rows | Indexed catalog. | Recent/fallback books. | Reader can still open books. |
| F072 | Shelf query changes quickly | Immediate expensive query per keystroke. | Debounce then paged query. | Less DB/UI churn. |
| F073 | Dialog content exceeds screen | Fixed dialog. | Screen-fitting scroll viewport. | Controls remain reachable. |
| F074 | Wheel over spin/combo control | Modify value accidentally. | Forward wheel to scroll container. | Prevents incidental setting changes. |
| F075 | Scan monitor has many workers | Fixed non-scrolling grid. | Reflowing/scrollable core grid. | All worker state remains inspectable. |
| F076 | Release tag exists | Rewrite tag/history. | Release builder creates only a new tag and refuses unsafe reuse. | History preserved. |
| F077 | Build target is transiently locked | Single destructive attempt. | Retried clean/move logic. | User source remains outside generated target. |
| F078 | Build free space inadequate | Continue packaging. | Preflight/validation stops build. | Avoids partial promoted release. |
| F079 | Unused heavy packages installed in environment | Let PyInstaller collect them. | Explicit module exclusions. | Smaller, more deterministic release. |
| F080 | PowerShell association removal fails | Script-only uninstall. | Pure `winreg` cleanup. | Own associations still removable. |
| F081 | Shared association key contains others' data | Delete key recursively. | Remove only Lumen values/defaults when owned. | Other applications preserved. |
| F082 | User has no admin rights | Machine-wide install. | HKCU/per-user install and associations. | No elevation required. |
| F083 | User requests total uninstall | Delete user state immediately. | Export positions/recents/marks first unless explicit `/NOSAVE`. | Reading continuity preserved by default. |
| F084 | Export destination name exists | Overwrite. | Unique date-based filename. | Previous export preserved. |
| F085 | Export write interrupted | Write final directly. | `.part`, flush, `fsync`, replace, readback validation. | Cleanup halts if export cannot be trusted. |
| F086 | Export path falls under deletion tree | Generic recursive cleanup. | Preserve exact export target. | Newly saved state survives. |
| F087 | Silent total uninstall | Cannot ask user. | Still exports unless `/NOSAVE`; `/SAVETO` directs destination. | Silence is not consent to lose reading state. |
| F088 | Install target looks unrelated | Delete requested folder. | Validate Lumen executable/manifest/unregister artifacts first. | Prevents broad accidental deletion. |
| F089 | Install-local library exists | Remove install tree. | Code preserves top-level `library`. | User content kept; parent may remain. |
| F090 | Shortcut desktop is redirected | Assume `%USERPROFILE%\Desktop`. | Resolve User Shell Folders/OneDrive, then fallback. | Shortcuts/export reach visible desktop more reliably. |
| F091 | Registry uninstall/vendor keys contain other products | Remove parent keys. | Delete vendor key only if empty; remove only Lumen product data. | Other software preserved. |
| F092 | Shell traces exist | Claim forensic erasure. | Clean scoped MUICache/UserAssist entries; document that Prefetch/Amcache/Event Log remain OS-owned. | Honest privacy boundary. |
| F093 | Uninstaller must delete itself | Delete running executable in-process. | Detached command script waits, removes target, then itself. | Plain `rmdir` leaves unexpected/non-empty content. |
| F094 | Release README promises an option not in current flow | Trust packaged prose. | Treat executable uninstaller behavior as source of truth; flagged in §15. | Avoids misleading maintenance assumptions. |
| F095 | Runtime version generated file absent | Read `_version.py`. | Environment → Git tag → `pyproject.toml` → unknown. | App can report a value in source and frozen contexts. |
| F096 | Source tree reports declared 1.4.0 | Trust only `pyproject.toml`. | Git/generated release currently resolves 1.5.0; drift flagged. | Runtime/release remains identifiable. |
| F097 | Tests run in hidden process | Long invisible execution. | Project rules require visible foreground test runs. | Diagnostics remain observable. |
| F098 | Index/cache is corrupt | Treat like irreplaceable user data. | Rebuild generated data from books; preserve marks/settings first. | Sacred user data separated from disposable acceleration data. |
| F099 | Root screenshot contains local UI/path detail | Treat as runtime dependency. | It is unreferenced development evidence; privacy/cleanup review recommended. | Do not delete without owner decision. |
| F100 | Future GPU marketing outruns code | Infer execution from names. | Require a registered backend and actual call path before claiming acceleration. | Documentation stays technically honest. |

## 13. UI, shelf, settings, and monitoring

| Component | Implementation details | Important minor behavior | Fallback/known seam |
|---|---|---|---|
| `ReaderWindow` | Large coordinator for WebEngine reader, toolbar/sidebar, navigation, search, definitions, marks, drag/drop, RSVP, and scan state. | Three themes; trusted JavaScript calls are guarded; state saved during navigation/close. | Its size (3,936 physical lines) is a future modularization pressure point, not itself a defect. |
| `ReaderWebPage` | Intercepts navigation and reader events. | Ctrl-click external policy separates book navigation from browser launch. | Tests cover link policy at the behavior seam. |
| In-book search | Cached normalized text and result navigation. | Search state maps back to exact chapter/position. | Empty/malformed terms return controlled states. |
| Marks dialogs | Bookmark/highlight/note management with tags/quotes. | Position tolerance around 0.005 supports approximate location matching. | Invalid stored rows do not poison the whole marks file. |
| Shelf model | `QAbstractListModel` with paged database fetches. | Keeps a single result page rather than catalog objects. | Falls back to recent/local books when index is absent. |
| Shelf delegate | Custom painting and keyboard routing. | Typing can focus/filter search without a dedicated click. | Debounce avoids query-per-keypress storms. |
| Search chips | Metadata/content/all and extension filters. | Pagination and counts are SQL-backed. | Backend preference UI currently does not replace SQLite. |
| Settings dialog | Six areas: Library; Sweep; Acceleration/scale; Index; Search/shelf; Reading. | Validates roots, offers optimize/forget, exposes effective auto tuning. | Result-queue depth has no direct control; no dedicated `test_settings_dialog.py`. |
| Screen fitting | Reusable scroll/fitting dialog base. | Wheel-safe controls pass wheel motion to viewport. | Makes dense settings usable on smaller displays. |
| Scan monitor | Reflowing core grid, sparkline, rates, status/messages, pause/resume/stop/open-folder. | Refresh around 250 ms, based on 4 Hz snapshots. | Telemetry may be momentarily inconsistent by design. |

## 14. Build, install, release, and uninstall

### 14.1 Build and release pipeline

| Step | Tool/file | Verified detail | Validation/fallback |
|---:|---|---|---|
| 1 | `versioning.py` | Validates/normalizes SemVer and resolves generated/env/Git/project versions. | Falls back without making source execution depend on Git. |
| 2 | `build.py` | Creates PyInstaller onedir application, Win32 metadata, icon, support files, hidden imports/data. | Explicitly excludes unused heavy dependencies. |
| 3 | NLTK packaging | Bundles WordNet zip; package inventory documents 19 entries, ~10.77 MiB compressed/~36.35 MiB expanded. | Definitions stay offline-first without corpus download. |
| 4 | `build_installer.py` | Builds Tkinter per-user installer executable. | Standard-library UI reduces runtime dependency surface. |
| 5 | `build_uninstaller.py` | Builds Tkinter uninstaller executable. | Supports interactive and silent modes. |
| 6 | `build_support.py` | Cleans/retries locked paths, checks space, copies support files, hashes and validates artifacts. | Promotion occurs only after verification. |
| 7 | `build_complete_release.py` | Coordinates tag/build/manifest/package/archive/checksums. | Creates a new tag only; does not rewrite history. |
| 8 | Release output | Folder, package zip, installer, uninstaller, manifest, SHA-256 list, README, outer archive+hash. | Each distributable can be independently verified. |

The audited generated manifest records version `1.5.0`, commit `5db5de44a6ad`, build time `2026-08-23 20:45:30 -0600`, Windows 11 AMD64, Python 3.12.10, about 397.2 seconds, 366,445,483 manifest-counted bytes, and per-user HKCU scope.

### 14.2 Install transaction

| Area | Behavior | Scope/safety |
|---|---|---|
| Target | Per-user application directory. | No machine-wide elevation requirement. |
| Payload | Extracts packaged onedir content while preserving tree. | Manifest/validation defines expected application identity. |
| Discovery | Writes install metadata, uninstall registration, and manifest. | HKCU ownership. |
| Shortcuts | Desktop, Start Menu, and install-local shortcuts via Python/PowerShell. | Working directory points at library where configured. |
| Associations | Separate EPUB and PDF ProgIDs; OpenWith registration; optional default takeover. | Additive by default; shared keys are not force-created/destructively replaced. |
| Shell refresh | Notifies Windows association/shortcut changes. | Failure is non-fatal to installed files. |
| Preserved JSON | Manifest/uninstaller/link/log/library names are treated specially during reinstall/uninstall. | User content is not casually overwritten. |

### 14.3 Total-uninstall export and cleanup

| Phase | Approx. progress share | Exact responsibility |
|---|---:|---|
| Export | 10% | Discover AppData state plus candidate library-adjacent marks; export positions, recent books, and deduplicated marks. |
| Shortcuts | 7% | Remove Lumen-owned desktop/Start/install shortcuts. |
| Associations | 13% | Remove only Lumen association values/ProgIDs, using PowerShell then Python fallback. |
| Registry | 8% | Remove product uninstall/discovery entries and scoped traces. |
| Files | 45% | Remove validated installed application files while preserving protected content/export. |
| Configuration | 9% | Delete Lumen user config/cache/temp state in total mode. |
| Shell | 8% | Refresh shell and schedule self-removal. |

The default total-uninstall behavior favors continuity: it exports reading positions, recent books, and marks unless the user explicitly supplies `/NOSAVE`. `/SAVETO` selects a destination. It does **not** export every setting. The exporter uses a unique date-based filename, `.part` staging, flush + `fsync`, atomic replace, then readback validation including position count before destructive cleanup proceeds.

Scoped privacy cleanup includes Lumen registry/config/temp/install data and Lumen-named MUICache/UserAssist entries. It intentionally does not claim to erase operating-system-owned Event Logs, Prefetch, Amcache, restore points, pagefile remnants, backups, or forensic traces outside the product's ownership.

## 15. Verified discrepancies, open seams, and technical debt

These are audit findings, not speculative feature requests. Severity means documentation/maintenance impact unless a runtime impact is stated.

| ID | Severity | Finding | Evidence/impact | Recommended resolution |
|---:|---|---|---|---|
| D001 | High | Project version declarations lag the tag. | HEAD/tag/generated release are 1.5.0, while `pyproject.toml` and the README badge still say 1.4.0; the 1.5 work remains under “Unreleased” in `CHANGELOG.md`. Source fallback without Git/generated data can report 1.4.0. | Move 1.5 changes into a dated 1.5.0 section and update project/readme declarations in one release commit. |
| D002 | High | Installer and runtime use different discovery registry keys. | `app.py` reads `Software\Lumen Reader`; installer writes discovery below `Software\XAIHT\Lumen Book Reader`. The installed shortcut/manifest/settings can mask this, but the registry fallback is not wired end-to-end. | Centralize the key constant or read both old/new paths with a tested migration. |
| D003 | Medium | Unknown free-space optimize behavior differs from prose. | `free=-1` skips the full FTS optimize condition but makes `roomy=true`, allowing `VACUUM`; the docstring describes unknown space as the cheap path. VACUUM may need substantial free space. | Treat unknown free space as constrained for both optimize and VACUUM, or update the contract/tests deliberately. |
| D004 | Medium | `v1.3.0` tag narrative and code do not align. | The tag changes four documentation files only; advanced index/search implementation lands after it, principally for v1.4.0. | Preserve history, but clarify the changelog/tag annotation as a preview/documentation release. |
| D005 | Low | RSVP feature dating is inconsistent. | Exact start/end targeting shipped in the v1.0.4 interval, while later prose presents it as new in v1.1.0. | Correct historical wording without moving tags. |
| D006 | High for claims | GPU and DirectStorage are seams, not active acceleration. | Backend registries are empty; DirectStorage detection only checks DLL presence; no CUDA kernel, GPU-resident index, or DirectStorage read calls ship. | Keep UI/prose labels explicit: “available provider” only after a backend registers and passes an actual capability check. |
| D007 | Medium | Search-backend preference is persisted but not consumed by `LibraryIndex`. | Settings can store `gpu-resident`, while queries always execute SQLite FTS5. | Wire selection through a backend facade or mark control as preview/unavailable. |
| D008 | Medium | Sharding utilities are not integrated storage. | FNV-1a shard assignment, names, and capacity estimates exist; current index remains one SQLite file/one writer. | Avoid scale claims that imply automatic partitioning until orchestration, migration, and multi-shard query merge exist. |
| D009 | Medium | Packaged install README has stale uninstall wording. | Current v1.5 total uninstall exports by default unless `/NOSAVE`; packaged prose describes keeping reading data through an option/checkbox that does not match the executable flow. | Generate README-INSTALL from the current uninstall contract. |
| D010 | Low/Medium | Install-local `library` preservation is broader than prose. | Uninstaller `_keep_names` preserves the top-level `library` name; documentation often says a non-empty library is preserved. Even an empty preserved directory can leave the install parent. | Align prose and tests with the intended empty-directory behavior. |
| D011 | Medium | Result-queue depth is important but not directly user-configurable. | Whole-book text results can dominate RAM; auto tiering is the only exposed control path. | Add an advanced control only if real-world profiles show auto tuning insufficient. |
| D012 | Low | Non-Windows storage classification is Linux-specific. | The generic non-Windows path probes Linux sysfs; macOS/other systems tend to become `unknown`. | Either document Windows/Linux support or add platform-native probes. |
| D013 | Medium | Frozen spawned extractors import the application package/GUI graph. | Spawn correctness is covered, but imports can add startup/RAM cost. | Move extraction worker entry and pure parsers into a Qt-light module if profiling justifies it. |
| D014 | Medium | Settings dialog lacks a dedicated test module. | Scanner, shelf, accel, and monitor have tests; settings integration is the visible gap named by project guidance. | Add save/reload/validation tests around all six tabs and unavailable-provider messaging. |
| D015 | Low | Root development screenshots are tracked but unreferenced. | `image.png` and `image copy.png` do not appear in runtime/docs references; the latter contains a visible local development resume identifier. | Decide explicitly whether they are evidence, documentation, or removable/private artifacts. Do not delete implicitly. |
| D016 | Low | `desktop.ini` is a large tracked, unreferenced localized-name map. | 1,216 lines / 115,164 bytes, apparently sourced from a book folder rather than runtime use. | Confirm provenance/need; remove only with owner authorization. |
| D017 | Low | Generated version timestamp and release manifest timestamp differ. | `_version.py` records 2026-08-24T02:38:57Z; manifest records 2026-08-23 20:45:30 -0600, consistent time zones but different build events. | Preserve both semantics or generate them in a single coordinated build step. |
| D018 | Documentation | A literal million-detail table would be deceptive padding. | The repository has 87 tracked files and ~26k relevant physical source/test/doc lines; a million unique verified facts do not exist. | Maintain dense atomic tables and regenerate inventory/history metrics as the project changes. |

## 16. Complete tracked-file ledger at audited HEAD

This ledger accounts for every file returned by `git ls-files` before `CODEX.md` was created. “Extent” is physical lines for text or image/corpus characteristics for binary data. “Evolution/verification” names the important release era, test seam, or audit note rather than pretending every file changed in every tag.

### 16.1 Root documentation, metadata, scripts, and assets

| # | File | Extent | Major responsibility | Minor details, fallback, evolution, or verification |
|---:|---|---:|---|---|
| 1 | `.gitignore` | 255 lines | Defines generated, local-book, cache, build, and environment exclusions. | Keeps release/output/test residue outside commits; the existing `image copy 2.png` is not ignored and remains untracked. |
| 2 | `CHANGELOG.md` | 269 lines | Human release/change narrative. | Covers 1.0–1.4 plus unreleased 1.5 work; version placement currently lags tag v1.5.0 (D001). |
| 3 | `CLAUDE.md` | 161 lines | Repository operating rules and architecture memory. | Requires PySide6, system Python, visible foreground tests, sacred user data, and author attribution; lists known scaling seams. |
| 4 | `CreateShortcut.ps1` | 167 lines | Creates desktop, Start Menu, and install-local shortcuts. | Resolves redirected desktop/OneDrive; supplies library working directory; build support payload. |
| 5 | `LICENSE` | 21 lines | MIT license grant and attribution. | Copyright belongs to Angela López Mendoza. |
| 6 | `LibraryEngineInLumenReader.md` | 359 lines | Design rationale for FTS5, stable row IDs, staged scanner, machine tuning, capacity and acceleration seams. | Contains measured deletion benchmark and large-scale projections; projections must be distinguished from shipped sharding/GPU execution. |
| 7 | `README.md` | 331 lines | Primary product, usage, setup, features, architecture, testing, and release overview. | References root icon and two docs screenshots; badge still says 1.4.0. |
| 8 | `RELEASING.md` | 371 lines | Repeatable SemVer/tag/build/release instructions. | Emphasizes new tags/no history rewrite, checksum verification, per-user installer, and release composition. |
| 9 | `RemoveShortcut.ps1` | 111 lines | Removes Lumen-owned shortcuts. | Uses redirected desktop resolution and scoped filename/target checks. |
| 10 | `SpeedReadingToolInLumenReader.md` | 226 lines | RSVP behavior, controls, timing, exact targeting, and rationale. | Some historical “new in” wording does not match the v1.0.4 tag interval (D005). |
| 11 | `THIRD_PARTY_NOTICES.md` | 230 lines | Dependency licenses/notices. | Critical for redistributed PySide6, BeautifulSoup, NLTK, PyMuPDF, and packaging components. |
| 12 | `assets/lumen-icon-chroma.png` | 1,254×1,254 PNG | Chroma/source icon variant. | Not referenced by runtime or current README; retain as design source until owner decides otherwise. |
| 13 | `assets/lumen-icon.png` | 1,254×1,254 PNG | Repository/documentation icon. | Referenced by README; separate packaged copy exists under `lumen_reader/assets`. |
| 14 | `build.py` | 389 lines | Builds the frozen onedir app and Win32 version resources. | Bundles support files/WordNet/Qt hidden imports; excludes unused heavy modules; introduced with release engineering and expanded for library engine. |
| 15 | `build_complete_release.py` | 333 lines | End-to-end release coordinator. | Validates SemVer/tag state, builds artifacts, creates manifests/checksums/archives; does not rewrite existing tags. |
| 16 | `build_installer.py` | 275 lines | Produces installer executable and installer package. | Tkinter/standard-library-oriented; per-user scope; uses common build support. |
| 17 | `build_support.py` | 438 lines | Shared clean/copy/hash/space/DLL/Tcl-Tk/retry utilities. | Centralizes Windows lock recovery and verified artifact moves. |
| 18 | `build_uninstaller.py` | 141 lines | Produces uninstaller executable. | Shares version/icon/build policy with app and installer. |
| 19 | `desktop.ini` | 1,216 lines | Windows localized filename mapping data. | No runtime reference found; large and likely library-originated (D016). |
| 20 | `docs/rsvp-speed-reader.png` | 2,250×1,320 PNG | RSVP feature screenshot. | Referenced by README; documentation-only binary. |
| 21 | `docs/screenshot.png` | 2,561×1,566 PNG | Main application screenshot. | Referenced by README; documentation-only binary. |
| 22 | `image copy.png` | 563×137 PNG | Development screenshot. | Unreferenced and contains visible local tool/session detail; review for privacy/provenance (D015). |
| 23 | `image.png` | 1,699×1,071 PNG | Development IDE/application screenshot. | Unreferenced; not loaded by application. |
| 24 | `install.py` | 1,329 lines | Interactive/silent per-user installer, extraction, shortcuts, registry, associations, manifest. | Validates inputs, supports optional defaults, scopes changes to HKCU, and has release tests. |
| 25 | `lumen_main.py` | 22 lines | Frozen executable entry point. | Delegates to launcher to isolate packaging startup behavior. |
| 26 | `preserved_user_state.json` | 27 lines | Fixture/sample of state preserved across install/uninstall workflows. | Used by release-scheme validation; not live user state. |
| 27 | `pyproject.toml` | 32 lines | Project metadata, dependencies, entry point, pytest/build configuration. | Declares 1.4.0 despite v1.5.0 tag (D001); dependencies constrain PySide6/BS4/NLTK/PyMuPDF. |
| 28 | `register_associations.ps1` | 395 lines | Adds EPUB/PDF ProgIDs, OpenWith verbs/icons, and optional defaults. | Avoids destructive `New-Item -Force` behavior on shared keys; refreshes shell. |
| 29 | `reindex.py` | 137 lines | Command-line full/incremental library indexing utility. | Provides non-UI operational path using the same index/scanner core. |
| 30 | `requirements.txt` | 4 lines | Runtime dependency pins/ranges. | Mirrors core `pyproject` requirements for simple setup. |
| 31 | `run_reader.py` | 7 lines | Source checkout launcher. | Thin call into `app.main`; console entry point also exists. |
| 32 | `uninstall.py` | 1,689 lines | Interactive/silent uninstall, export, associations, registry, files, config, shell cleanup, self-delete. | v1.5 exports reading continuity before total cleanup; extensive safety tests; preserves top-level library. |
| 33 | `unregister_associations.ps1` | 192 lines | Removes Lumen-owned file-association data. | Preserves shared keys/other apps; Python `winreg` fallback exists. |
| 34 | `versioning.py` | 247 lines | Shared SemVer, runtime/build version, tag, filename, and Win32 tuple logic. | Resolution fallbacks keep source/frozen execution identifiable; release-scheme tests exercise it. |

### 16.2 Runtime package and packaged assets

| # | File | Extent | Major responsibility | Minor details, fallback, evolution, or verification |
|---:|---|---:|---|---|
| 35 | `lumen_reader/__init__.py` | 20 lines | Package metadata/public identity. | Keeps import surface small and version access centralized. |
| 36 | `lumen_reader/__main__.py` | 6 lines | Module execution entry. | Calls `app.main`; alternative to root launcher. |
| 37 | `lumen_reader/accel.py` | 651 lines | Hardware/DirectStorage detection, provider protocols/registries, selection reports, shard/capacity helpers. | Qt-free and background-probeable; shipped registries empty, so CPU/FTS5 fallback is mandatory. |
| 38 | `lumen_reader/app.py` | 141 lines | QApplication setup, version, stores, library/index initialization, window launch. | Starts accel probe off UI thread; registry discovery key differs from installer key (D002). |
| 39 | `lumen_reader/assets/lumen-icon.png` | 1,254×1,254 PNG | Packaged high-resolution icon. | Runtime distribution copy of brand asset. |
| 40 | `lumen_reader/assets/lumen.ico` | 256×256 ICO | Windows runtime/executable/association icon. | Consumed by app/build/installer metadata. |
| 41 | `lumen_reader/assets/nltk_data/corpora/wordnet.zip` | 10,776,546 bytes | Bundled offline WordNet corpus. | Eliminates first-run download/network requirement; dominates tracked size. |
| 42 | `lumen_reader/book.py` | 467 lines | EPUB validation, safe extraction, package/navigation parsing, sanitization, chapter/search model. | 512 MiB cap, path containment, CSP, active-content stripping, NCX/spine fallbacks; EPUB tests. |
| 43 | `lumen_reader/dialog_layout.py` | 177 lines | Screen-fitting scroll dialogs and wheel-safe controls. | Prevents clipped dense dialogs and accidental wheel edits; dedicated tests. |
| 44 | `lumen_reader/dictionary.py` | 504 lines | Normalization, offline WordNet, online lexical/phrase parsers, atomic bounded cache. | Provider failures are isolated; strict evidence prevents low-quality phrase matches; tests. |
| 45 | `lumen_reader/launcher.py` | 91 lines | Frozen-process startup hygiene and book-path/cwd handling. | Best-effort DLL-path release; preserves absolute argument on cwd changes. |
| 46 | `lumen_reader/library_index.py` | 1,181 lines | SQLite schema, incremental scanning helpers, EPUB/PDF extraction, FTS search, row-ID map, maintenance. | WAL/128 MiB cache/250k text budget; corrupt-book and hostile-query fallbacks; extensive tests. |
| 47 | `lumen_reader/machine_profile.py` | 449 lines | CPU/RAM/volume classification and automatic tuning inputs. | Windows Win32 probe and Linux rotational sysfs; caches per volume; unknown on unsupported probes. |
| 48 | `lumen_reader/marks.py` | 237 lines | Versioned bookmark/highlight/note persistence and relocation. | Atomic save, invalid-row skipping, unique filename relink, bounded tags/quotes; tests. |
| 49 | `lumen_reader/models.py` | 66 lines | Shared dataclasses: TOC, chapters, metadata, search results, bookmarks. | Keeps data interchange explicit and lightweight. |
| 50 | `lumen_reader/pdf_book.py` | 449 lines | PyMuPDF rendering/text/word geometry/TOC/password/OCR/cache. | 4,096 px cap, optional Tesseract, image-only view fallback, Unicode cleanup; tests. |
| 51 | `lumen_reader/scan_monitor.py` | 658 lines | Live scan monitor, core tiles, sparkline, rates, pause/resume/cancel controls. | Reflow/scroll handles many workers; human-readable sizes/rates; dedicated tests. |
| 52 | `lumen_reader/settings_dialog.py` | 1,093 lines | Six-tab library/sweep/accel/index/search/reading configuration UI. | Time-bounded probes and effective tuning explanations; no dedicated test file (D014). |
| 53 | `lumen_reader/shelf.py` | 863 lines | Paged/virtual library model and shelf UI/delegate/search/filter/paging. | One-page memory model, debounce, keyboard routing, recent fallback; broad UI tests. |
| 54 | `lumen_reader/smart_definition.py` | 391 lines | Context morphology, optional Googler/Ollama calls, prompt/result validation. | Googler exists-only auto enable; search engine fallback; Ollama disabled by default and strictly parsed; tests. |
| 55 | `lumen_reader/speed_reader.py` | 973 lines | RSVP segmentation, ORP, timing, controls, countdown, rest, exact source targeting. | Hard parameter bounds and boundary-safe chunks; speed/target tests. |
| 56 | `lumen_reader/storage.py` | 116 lines | Atomic reader settings, recents, positions, and relocation. | Invalid JSON defaults; recent list bounded to 8; unique filename recovery; safety/storage tests. |
| 57 | `lumen_reader/turbo_scan.py` | 1,661 lines | Full staged high-parallelism scan pipeline, tuning, priorities, IPC, batching, telemetry, cancellation. | Spawn/process caps, inline one-worker path, bounded heavy queues, graceful termination, skip-prune-on-cancel; extensive tests. |
| 58 | `lumen_reader/ui.py` | 3,936 lines | Main reader window and integrated EPUB/PDF reading, search, definitions, marks, RSVP, scan/shelf orchestration. | Cancels asynchronous work on session/close; guarded links/JS; high integration concentration with multiple behavior tests. |
| 59 | `lumen_reader/version.py` | 389 lines | Runtime version resolution and generated `_version.py` support. | Generated → environment → Git → project fallback; v1.5 generated artifact exists in release, not tracked source. |

### 16.3 Test suite

Static function counts below count declared `test_*` functions; pytest parametrization expands them to the documented 322 collected cases.

| # | File | Extent | Major responsibility | Minor details, fallback, evolution, or verification |
|---:|---|---:|---|---|
| 60 | `tests/test_accel.py` | 23 tests | Acceleration probes, selection, sharding/capacity contracts. | Verifies unavailable providers fall back rather than implying a shipped GPU engine. |
| 61 | `tests/test_dialog_layout.py` | 7 tests | Screen-fitting and wheel-safe dialog behavior. | Protects smaller-screen usability. |
| 62 | `tests/test_dictionary.py` | 11 tests | Normalization, source parsing, phrase evidence, cache behavior. | Covers clean misses and provider boundaries. |
| 63 | `tests/test_epub_books.py` | 8 tests | EPUB parsing/navigation/content safety. | Exercises valid books and safety-sensitive failure paths. |
| 64 | `tests/test_fts_rowid_map.py` | 13 tests | Stable FTS row mapping, delete/update/rebuild compatibility. | Guards the large-index performance fix while preserving slow-path correctness. |
| 65 | `tests/test_index_optimize_budget.py` | 12 tests | Free-space-aware maintenance and bounded merge behavior. | Audit still found the unknown-space VACUUM nuance (D003), which deserves an explicit regression case. |
| 66 | `tests/test_library_index.py` | 24 tests | Schema, incremental scans, EPUB/PDF indexing, queries, pruning. | Core generated-index correctness. |
| 67 | `tests/test_link_policy.py` | 1 test | Reader external-navigation policy. | Confirms guarded Ctrl-click behavior. |
| 68 | `tests/test_machine_profile.py` | 22 tests | CPU/RAM/storage classification and auto-sizing. | Covers Windows/Linux-style probe outcomes and conservative tiers. |
| 69 | `tests/test_marks.py` | 3 tests | Mark persistence and relocation. | Small suite focused on atomic/user-data semantics. |
| 70 | `tests/test_pdf_books.py` | 11 tests | PDF rendering/text/TOC/OCR/password-related behavior. | Validates reader usability across native and image-like PDFs. |
| 71 | `tests/test_reader_search.py` | 2 tests | In-book search integration. | Narrow integration seam rather than general FTS shelf search. |
| 72 | `tests/test_release_scheme.py` | 49 tests | Versioning, build metadata, installer/uninstaller/association/release contracts. | Largest release-engineering suite; protects scope and artifact naming. |
| 73 | `tests/test_rsvp_targeting.py` | 1 test | Exact selection/range targeting for RSVP. | Critical focused regression for v1.0.4 behavior. |
| 74 | `tests/test_safety_and_storage.py` | 4 tests | Storage atomicity/defaults and safety invariants. | Prioritizes user-state recovery. |
| 75 | `tests/test_scan_monitor.py` | 7 tests | Monitor formatting, layout, and scan-state presentation. | Keeps high-core-count display manageable. |
| 76 | `tests/test_shelf_ui.py` | 42 tests | Shelf model/delegate/search/filter/pagination/keyboard/UI behavior. | Broad coverage for the v1.4 library surface. |
| 77 | `tests/test_smart_definition.py` | 5 tests | Contextual provider parsing/fallback. | Ensures optional model/search failures stay optional. |
| 78 | `tests/test_speed_reader.py` | 6 tests | Timing, chunking, ORP, control behavior. | Complements exact-target integration test. |
| 79 | `tests/test_turbo_scan.py` | 33 tests | Process sizing, priorities, queues, pipeline, cancellation, telemetry. | Main high-parallelism regression suite. |
| 80 | `tests/test_uninstall_export.py` | 12 tests | Total-uninstall state discovery/export/validation/preservation. | Protects the v1.5 default continuity guarantee. |

### 16.4 Tracked visual-test artifacts

| # | File | Extent | Major responsibility | Minor details, fallback, evolution, or verification |
|---:|---|---:|---|---|
| 81 | `tmp/pdfs/lumen-reader-pdf-selection-2.png` | 1,400×900 / 12,599 bytes | Visual capture of PDF selection behavior. | Evidence artifact, not loaded by runtime. |
| 82 | `tmp/pdfs/lumen-reader-pdf-selection.png` | 1,400×900 / 12,595 bytes | Companion PDF selection capture. | Evidence artifact, not loaded by runtime. |
| 83 | `tmp/pdfs/source-page-1.png` | 935×1,210 / 37,138 bytes | Source-page reference for PDF visual validation. | Helps compare overlay/raster alignment. |
| 84 | `tmp/pdfs/visual-marks-2.json` | 4 lines / 36 bytes | Visual-check marks state. | Tiny fixture; not authoritative user state. |
| 85 | `tmp/pdfs/visual-marks.json` | 4 lines / 36 bytes | Companion visual-check marks state. | Tiny fixture; not authoritative user state. |
| 86 | `tmp/pdfs/visual-state-2.json` | 27 lines / 663 bytes | Visual reader state/config fixture. | Contains local development paths/config and should not be generalized to user defaults. |
| 87 | `tmp/pdfs/visual-state.json` | 27 lines / 663 bytes | Companion visual reader state/config fixture. | Evidence artifact; not loaded in ordinary startup. |

### 16.5 This audit artifact

| File | Status | Purpose |
|---|---|---|
| `CODEX.md` | Added after the 87-file baseline | Maintains the repository-wide engineering record, version ledger, implementation detail, fallback matrix, and future-audit checklist. |

## 17. Salient symbol and responsibility map

This map is intentionally architectural: private helpers remain discoverable in their owning file, while the symbols below tell a maintainer where control and data cross module boundaries.

| Module | Salient symbols/concepts | Inputs | Outputs/side effects |
|---|---|---|---|
| `app.py` | `main`, library-root resolution, registry/default discovery | argv, Qt settings, AppData, optional book | QApplication, stores/index, `ReaderWindow`. |
| `launcher.py` | `main`, frozen startup hygiene | argv, Windows DLL/cwd state | Corrected environment then `app.main`. |
| `book.py` | `EPUBBook`, package/nav/chapter parsing, sanitizer | EPUB path | Metadata, TOC, safe chapter HTML/text, temp extraction. |
| `pdf_book.py` | `PDFBook`, page render/text/word/TOC/OCR | PDF path/password/page | Raster page, selectable overlay data, text/TOC, temp cache. |
| `models.py` | `TocEntry`, `Chapter`, `BookMetadata`, `SearchResult`, `Bookmark` | Typed fields | Stable in-process records. |
| `storage.py` | `ReaderStore` | JSON path, settings/position/recent changes | Atomic JSON state. |
| `marks.py` | `MarksStore` | library root, bookmark/note/highlight edits | Versioned atomic marks JSON. |
| `dictionary.py` | `DictionaryEntry`, normalization, WordNet/provider parsers, cache | selected word/phrase and HTTP payloads | Validated lexical entries and cached stable results. |
| `smart_definition.py` | contextual morphology, Googler/Ollama client/parsers | selection, surrounding context, optional tools | Context definition/references or clean miss. |
| `speed_reader.py` | RSVP configuration/controller/dialog, segmentation/ORP/timing | source blocks/range and reader settings | Timed word chunks and exact final source position. |
| `library_index.py` | `LibraryIndex`, schema/migrations, extractors, FTS queries/maintenance | library paths, book files, search expressions | SQLite catalog/FTS rows and paged results. |
| `machine_profile.py` | machine/volume profile and auto policies | CPU affinity/count, RAM, volume path | Descriptive tier and tuning inputs. |
| `accel.py` | capability records, provider protocols/registries, selectors, shard/capacity tools | machine probes and preferences | Availability/selection reports; no stock GPU execution. |
| `turbo_scan.py` | `TurboScanJob`, scanner settings/snapshots, worker/stage routines | library root/index/settings | Incremental database updates and bounded telemetry. |
| `shelf.py` | paged list model, delegate, shelf widget | `LibraryIndex`, recent fallback, filters/query | Virtualized book list and open/search events. |
| `settings_dialog.py` | settings tabs/probes/save/sweep actions | store, machine profile, index/scanner | Validated persisted preferences and maintenance actions. |
| `scan_monitor.py` | worker grid, sparkline, monitor dialog | scan snapshots/messages | Human-readable live progress/control. |
| `dialog_layout.py` | screen-fitting dialog, wheel-forward controls | QWidget content/screen geometry | Reachable, scrollable settings/dialog UX. |
| `ui.py` | `ReaderWebPage`, definition/marks dialogs/cards, `ReaderWindow` | book/index/stores/user events | Integrated reader UI and all coordinated async lifecycle. |
| `version.py` / `versioning.py` | runtime and build/release version resolution | generated file, env, Git, project metadata | SemVer/display/Win32 version identities. |
| `install.py` | install UI/transaction/registration | package, target, user choices, silent flags | Per-user files, shortcuts, associations, registry. |
| `uninstall.py` | export/cleanup/self-delete transaction | install target, total mode, `/NOSAVE`, `/SAVETO` | Continuity export and scoped removal. |

## 18. Data contracts and schemas

### 18.1 Reader-state contract

| Field family | Semantics | Validation/recovery |
|---|---|---|
| Theme/font/sidebar/reading settings | User presentation and reading preferences. | Missing/invalid values fall back to defaults and bounded UI ranges. |
| Positions | Last known chapter/page/offset per source. | Exported during total uninstall; path relocation never guesses ambiguously. |
| Recent books | Ordered quick-open list, maximum eight. | Missing paths can relink only by a unique same filename. |
| Library root/recent roots | Discovery and shelf inputs. | Settings dialog validates selected root; app has fallback candidates. |
| Scan settings | Process/walker/batch/priority/queue-related preferences. | `auto` passes through machine policy; hard platform/RAM caps remain. |
| Acceleration/search preference | Desired provider names. | Extraction selection falls back; stored search preference is currently not consumed. |

### 18.2 Marks contract

| Field | Purpose | Bound/invariant |
|---|---|---|
| Version | Storage schema version. | Current JSON schema version is 1. |
| Source path/name | Associates mark with book. | Relocation only on unique filename match. |
| Position | Chapter/page/fractional location. | Approximate matching tolerance around 0.005. |
| Kind | Bookmark/highlight/note classification. | Invalid rows are skipped. |
| Quote | Human context around mark. | Maximum 1,000 characters. |
| Tags | User classification. | Maximum 20. |

### 18.3 SQLite tables and indexes

| Object | Key columns/content | Index/access pattern |
|---|---|---|
| `books` | id, root, unique path, filename/ext, size/mtime, metadata, pages/text/error/generation state | Indexes by root, root+extension, root+case-insensitive title, root+generation. |
| `scan_runs` | root, start/end/status/count/rate/error diagnostics | Retain newest 40/root. |
| `books_fts` | title, author, name, subjects, publisher, unindexed `book_id` | BM25 metadata search. |
| `content_fts` | body, unindexed `book_id` | Full text + snippets. |
| `fts_rowid` | `book_id` primary key, metadata/content FTS row IDs | O(1)-style mapped deletion/update path. |
| `index_meta` | key/value feature/schema metadata | Stores FTS-map readiness/version. |

## 19. Generated and local workspace state observed during audit

Generated/ignored files are not implementation source, but they reveal what has actually been built and exercised.

| Path/category | Observed state | Interpretation |
|---|---:|---|
| `.artifacts` | 66 files / 12,440,412 bytes | Local build/test artifacts; ignored. |
| `.claude` | 1 file / 376 bytes | Local tooling state; ignored. |
| `build` | Empty at audit | No active PyInstaller work directory. |
| `dist` | 1,015 files / 714,667,399 bytes | Current v1.5.0 release folder plus outer archive/checksum. |
| `lumen_reader` on disk | 47 files / 13,043,683 bytes | Includes ignored caches/generated items beyond 22 tracked runtime files. |
| `tests` on disk | 44 files / 1,017,753 bytes | Includes ignored bytecode/cache beyond 21 tracked tests. |
| `tmp` on disk | 104 files / 34,365,274 bytes | Visual/manual-test residue; only seven entries are tracked. |
| Local EPUBs | Two root-level books, roughly 1.9 MiB each, ignored | Manual test/user content; never modify during source cleanup. |
| `lumen-reading-marks.json` | 45,819 bytes, ignored | Live user/library state; sacred, not a generated cache. |
| `release-build.log` | 47,894 bytes, ignored | Build evidence; useful for diagnosing current release. |
| `dist/Lumen_Release_v1.5.0/Lumen_Reader_1.5.0_Installer.exe` | 1,946,254 bytes | Generated installer. |
| `dist/Lumen_Release_v1.5.0/Lumen_Reader_1.5.0_Package.zip` | 326,651,707 bytes | Application payload. |
| `dist/Lumen_Release_v1.5.0/Lumen_Reader_1.5.0_Uninstaller.exe` | 11,667,006 bytes | Generated uninstaller. |
| Release manifest | 117,469 bytes | Detailed generated inventory. |
| Release SHA list | 107,708 bytes | SHA-256 verification inventory. |
| Release install README | 1,438 bytes | Has stale uninstall wording (D009). |
| Outer archive | 348,104,330 bytes | Final portable distribution unit. |

## 20. Operational commands and safe maintenance workflow

Run long tests/builds visibly in the foreground. Do not hide them behind background processes.

| Goal | Command | Notes |
|---|---|---|
| Run from source | `python run_reader.py` | Uses `app.main`. |
| Run as module | `python -m lumen_reader` | Equivalent package entry. |
| Open a book | `python run_reader.py "C:\path\book.epub"` | Quote Windows paths. |
| Reindex | `python reindex.py "C:\path\library"` | Uses production index/scanner components. |
| Run tests | `python -m pytest` | Must remain visible/foreground per project rules. |
| Build app | `python build.py` | Produces onedir frozen application. |
| Build full release | `python build_complete_release.py ...` | Follow `RELEASING.md`; create a new SemVer tag, never rewrite an existing release tag. |
| Verify status | `git status --short` | Preserve unrelated/untracked user assets. |
| Inspect release identity | Read manifest + SHA-256 file and runtime About/version | Cross-check tag, commit, generated version, and artifact filenames. |

### 20.1 Change checklist for future agents/maintainers

1. Read `CLAUDE.md`, this dossier, and the files directly in scope.
2. Record `git status --short` before editing; never erase unrelated changes.
3. Preserve Angela López Mendoza / @angelahack1 attribution in source, build, installer, and docs.
4. Distinguish user data (books, positions, marks, preferences) from rebuildable index/cache data.
5. Keep EPUB/PDF input hostile by default: containment, size limits, sanitization, timeouts, and cleanup are invariants.
6. Keep UI work off blocking probes/network/large scans; session-gate asynchronous results.
7. Keep scanner heavy queues bounded and skip generation pruning on cancellation.
8. Never claim GPU/DirectStorage execution from capability labels alone; require an actual registered/called backend.
9. Update `pyproject.toml`, README badge, changelog, generated version, tag, manifest, and artifact names together for a release.
10. Test the smallest relevant slice first, then the full visible suite when implementation changes justify it.
11. For total uninstall, prove export validity before deleting reading state and preserve the export destination.
12. Regenerate §1, §3, §16, and §19 when files, tags, or distribution artifacts change materially.

## 21. Audit methodology and reproducibility

| Audit surface | Method | Coverage statement |
|---|---|---|
| Repository state | Git status, branch, HEAD, tags, tracked-file list, commit counts. | Establishes exactly what revision and workspace dirt existed. |
| Tagged evolution | Tag targets plus `git diff --shortstat`/file counts between adjacent tags. | Quantifies additions/removals without trusting changelog prose alone. |
| Source size | Physical line and byte counts by category and file. | Useful for inventory, not a measure of quality or logical LOC. |
| Runtime behavior | Direct reading of entry, reader, storage, definitions, index, scanner, UI, and version modules. | Claims in §§4–13 trace to executable code. |
| Build/release | Direct reading of build, installer, uninstaller, PowerShell, release docs, and generated manifest. | Separates source contract from packaged artifact drift. |
| GPU/DirectStorage | Backend registries, probe code, selectors, and call-path search. | Confirms the difference between capability seam and active accelerated I/O/compute. |
| Tests | Complete tracked test inventory and static test-function counts. | Does not claim execution during this documentation-only audit. |
| Binaries | Dimensions/sizes plus reference search. | Does not infer semantic content beyond inspected/referenceable evidence. |
| Completeness | Every pre-existing tracked path appears once in §16. | Validation should compare the ledger against `git ls-files` after excluding newly added `CODEX.md`. |

### 21.1 What this document does not claim

- It does not claim a CUDA kernel, DirectStorage read path, GPU-resident index, automatic sharded catalog, or distributed cluster that the repository does not contain.
- It does not claim that physical lines equal logical lines, complexity, test coverage, or performance.
- It does not claim that ignored release artifacts are reproducible without running the documented build pipeline.
- It does not claim a test run occurred merely because test source was audited.
- It does not claim forensic erasure of operating-system artifacts outside Lumen's ownership.
- It does not treat generated indexes as sacred, and it does not treat books/marks/positions as disposable.

## 22. Maintained project memory summary

The project's center of gravity changed substantially across the audited tags: an initially focused EPUB/PDF reader became a locally indexed library application with a sophisticated CPU-parallel scan pipeline and a full Windows release lifecycle. The largest engineering additions were not superficial UI features: they were safe hostile-document handling, exact source-position tracking, stable FTS maintenance, bounded multiprocessing, per-machine tuning, and uninstall continuity.

The most important truth to retain is the boundary between **implemented acceleration** and **acceleration architecture**. Today, the working high-performance path is CPU multiprocessing + threaded overlap + bounded queues + SQLite WAL/FTS5 + machine-aware policy. GPU, DirectStorage, and sharding are well-defined extension seams and planning utilities, not active data paths. That distinction protects both users and future engineering decisions.

The most important maintenance action after this audit is release-consistency cleanup: synchronize 1.5.0 declarations, align the registry discovery key, make unknown-disk-space optimization conservative, and update packaged uninstall prose. None of those findings justify erasing history or user data; each can be repaired with focused tests and a new versioned change.
