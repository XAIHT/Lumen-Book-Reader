# Library Engine in Lumen Reader

*Lumen Book Reader · created by Angela López Mendoza · @angelahack1*

## Executive summary

Lumen keeps a **searchable index of every book in your library** so the shelf can answer questions about thousands of files instantly, including questions about what is *inside* them. Building that index is the **Turbo Sweep**: a concurrent pipeline that walks the library, decides which books changed, reads them across a fleet of extractor processes, and writes the results into SQLite — all four stages running at the same time, joined by bounded queues.

Three surfaces expose it:

| Surface | Module | What it is |
|---|---|---|
| **⚙ Configuration** (<kbd>Ctrl+,</kbd>) | `lumen_reader/settings_dialog.py` | One window, six tabs, every setting Lumen has |
| **The sweep monitor** (<kbd>F5</kbd>) | `lumen_reader/scan_monitor.py` | A live per-process view of the fleet while it works |
| **The shelf** | `lumen_reader/shelf.py` | Paged search over the index, by metadata or by book text |

Underneath: `lumen_reader/turbo_scan.py` is the pipeline, `lumen_reader/library_index.py` is the index and its schema, and `lumen_reader/accel.py` is the hardware-detection and backend-selection seam.

The design position throughout is the same one the RSVP tool takes: **say what is actually happening**. The acceleration tab never claims a GPU is doing work the CPU is doing, the monitor shows real per-worker telemetry rather than an animated approximation, and a sweep that finds nothing says so instead of reporting success.

---

## Why a pipeline and not a loop

The obvious implementation is a loop: walk the folder, collect the file list, then read each book. It fails on the two libraries people actually have.

**A large library.** Collecting the file list first means holding it in memory and doing nothing else while you do. On a shelf of tens of thousands of books that is a measurable pause before any work begins, and the memory is proportional to the library.

**A remote library.** A network share or NAS answers directory listings slowly. A walk-then-read loop leaves every core idle for the entire walk, then leaves the network idle for the entire read.

So the sweep is staged, and the stages overlap:

```
  ┌──────────┐   walk queue   ┌──────────┐   job queue   ┌─────────────┐
  │ walkers  │ ─────────────► │  triage  │ ────────────► │  extractor  │
  │ (threads)│                │ (thread) │               │    fleet    │
  └──────────┘                └──────────┘               │ (processes) │
       │                           │                     └─────────────┘
       │ os.scandir                │ size + mtime_ns             │
       │ one thread per            │ against the index           │ result queue
       │ subtree                   │ unchanged → skipped         ▼
       │                           │                      ┌──────────┐
       ▼                           ▼                      │  writer  │
   folders swept              already current             │ (thread) │
                                                          └──────────┘
                                                                │
                                                                ▼
                                                          library-index.db
```

Two consequences matter, and both are user-visible:

- **The fleet starts reading before the walk has finished.** The first book reaches an extractor while the walker is still several folders deep. On a slow share this is the difference between minutes of idle cores and none.
- **Resident memory is set by the queue depths, not by the size of the library.** Nothing in the pipeline holds the complete file list. A NAS with millions of books costs the same memory as a shelf with a hundred.

Threads do the I/O-bound stages (walking, triage, writing — all of which release the GIL in `os.scandir` and SQLite). Processes do the CPU-bound stage, because EPUB unzipping plus HTML parsing and PDF text extraction are exactly what the GIL prevents from scaling in threads.

### Triage: the cheap answer first

Before a book is handed to an extractor, triage asks the index for the `size` and `mtime_ns` it recorded last time. If both match, the book is **already current** and is never opened. Only its `seen_gen` is stamped.

This is why a second sweep of an unchanged library costs one walk and nothing else, and why adding ten books to a shelf of ten thousand reads ten books.

### Generations and pruning

Every sweep of a root takes a new **generation** number. Books seen during the sweep are stamped with it. When the sweep completes — and only when it completes, never when it is cancelled — `prune_generation()` deletes rows for that root that were *not* stamped, which is precisely the set of books that have disappeared from disk.

Pruning is a setting (**Forget books that are no longer on disk**, on by default). Turning it off is the right answer when the library lives on a drive that is not always mounted, because an unmounted share and an empty folder look identical to a directory walk.

### Single-worker mode

Asking for one process skips the fleet entirely and extracts in-process: identical bookkeeping, none of the spawn cost. The monitor says so — *"Single-worker mode: extraction runs in-process, no fleet spawned."*

---

## The index

`%APPDATA%\Lumen Reader\library-index.db`, a rebuildable cache kept with Lumen's own state rather than inside your library — which may well be read-only, or synced by something that would not enjoy a multi-gigabyte database appearing in it.

SQLite in WAL mode, `synchronous=NORMAL`, `temp_store=MEMORY`, and a 128 MB page cache. WAL is what lets the writer thread commit while the shelf reads. Autocheckpoint runs every 2,000 pages, retained journals are limited to 256 MiB, and a new sweep visibly checkpoints an oversized WAL left by an interrupted writer before it dispatches books.

| Table | Holds |
|---|---|
| `books` | One row per file: path, size, `mtime_ns`, title, author, publisher, language, subjects, description, pages, whether it has text, whether it read cleanly, and `seen_gen` |
| `books_fts` | FTS5 over title, author, filename, subjects, publisher |
| `content_fts` | FTS5 over the extracted body text |
| `fts_rowid` | Where each book sits in each FTS table (see below) |
| `scan_runs` | One row per terminal sweep: generation, status/error, found, extracted, committed, already-current, unreadable, rejected, cancelled, and unaccounted counts |
| `index_meta` | Small durable facts about the index itself |

Both FTS5 tables tokenize with `unicode61 remove_diacritics 2`, so *Gödel* matches *Godel*.

### The writer boundary — one malformed book cannot stop the fleet

The writer is the only database writer, so its correctness is the pipeline's liveness property. Extractors publish through a cancellation-aware bounded queue and remain in the shared `publishing` state until that queue accepts the record. If the writer suffers an operational SQLite failure, it publishes one fatal error, releases every producer, and the conductor drains and joins every stage. The monitor stays attached in `failing` while cleanup is in progress and changes to terminal `error` only when cleanup and durable scan recording finish.

Each result is normalized to strict UTF-8 twice: once where EPUB/PDF extraction returns document text, and again immediately before SQLite. Lone UTF-16 surrogates exposed by malformed MuPDF metadata are removed without damaging valid non-ASCII text. The exact field names repaired are logged.

Each individual books/FTS/row-map update runs under a savepoint. A data error rolls back only that record and writes a small `ok=false` fallback row where possible; a path that cannot itself be represented is rejected and explicitly counted for retry. Database operational errors are not treated as bad books and are never swallowed. Batch counters move only after the outer transaction commits, so `INDEXED OK`, unreadable, bytes, throughput, and percentage cannot get ahead of durable SQLite state.

### The `fts_rowid` map — and why it exists

This is the most expensive lesson in the engine, and it is worth stating plainly because the symptom was so misleading.

`book_id` in an FTS5 table **must** be `UNINDEXED`. Indexing it would tokenize integers into the same vocabulary as the prose and poison every search. But that leaves `DELETE FROM content_fts WHERE book_id = ?` with no index to use, and SQLite plans it as:

```
SCAN content_fts VIRTUAL TABLE INDEX 0:
```

A full pass over the entire full-text index — which, on a real library, *is* the database. Re-indexing one changed book cost a scan of all of them.

Measured on a 235 MB index: **218 ms per book by `book_id`, 2.1 ms by rowid — 105×.** And the gap grows with the index, because one side is O(n) and the other is not. On a 10.4 GB index it worked out at roughly ten seconds a book, which is how a sweep of 304 changed books came to commit seventeen of them and then appear to hang forever.

So Lumen keeps the rowids itself: one `fts_rowid` row per book holding its position in each FTS table, and every delete becomes a rowid lookup. The map is built once, on the first sweep after the upgrade, where the monitor's log makes the work visible instead of silent. `index_meta.fts_map_built` records that it has happened — which cannot be inferred from the map's contents, because an empty map is also what a library with no books looks like.

`tests/test_fts_rowid_map.py` guards the whole story: the map is built once, the upgrade is idempotent, and an upgraded index can be swept again.

---

## Searching

The shelf searches the index, not the disk. Two modes and a union:

| Mode | Ranked by | Returns |
|---|---|---|
| **Titles & authors** (`meta`) | `bm25(books_fts)` | Metadata matches |
| **Inside books** (`content`) | `bm25(content_fts)` | Matches with a highlighted 18-word snippet |
| `all` | Title order | The union of both, so a topic hit and a title hit surface together |

Results are paged, and filter chips narrow to **EPUB only** or **PDF only** (`b.ext IN (…)`, applied inside SQL rather than after the fact, so paging stays correct).

The search box accepts FTS5 query syntax, which is why the placeholder shows it:

- `machine learning` — books matching both terms
- `"exact phrase"` — a phrase match
- `ext:pdf` — a column-qualified term
- `neural*` — a prefix match

Defaults — which mode a fresh search starts in, the debounce, the page size, and the snippet width — live in **Configuration ▸ Search & shelf**.

---

## Telemetry, and how the monitor knows

Each extractor process owns a small shared-memory block: six integers (`pid`, state, done, failed, bytes, started-at) plus a 512-byte path buffer. State is one of idle, extracting, publishing, or stopped. The worker writes it; the monitor reads it.

It is deliberately **lock-free**. The only consumer is a display, so a torn read costs one frame of a filename rather than a stalled extractor. Nothing about the sweep's correctness depends on this block — it exists so the reader can watch.

### What the monitor shows

- **One tile per extractor**, in a grid that reflows by window width and scrolls rather than hiding tiles: PID, live state, the book that process is reading *at this instant*, and how many it has finished.
- **Six headline counters**: books found, indexed successfully, already current, unreadable, folders swept, bytes seen.
- **A throughput sparkline** and an estimate of time remaining.
- **A log** of what the pipeline is doing, including one-off work like the FTS map upgrade.
- **Pause**, **Resume**, **Stop the sweep**, **Open this folder**, and **Close**.

`tests/test_scan_monitor.py` asserts the grid's invariants directly: no tile is ever painted outside the grid, a narrower window means more rows rather than lost tiles, the fleet lives in a scroll area, a short window scrolls instead of hiding, the monitor opens big enough for a full fleet, and it survives a snapshot with no workers at all.

---

## Process priority and thread boost

On Windows the sweep can raise its own priority class (`apply_process_priority`) and boost the walker threads (`boost_current_thread`). *Realtime* is offered but never chosen automatically: it starves the input queue and makes the machine feel broken.

Priority is a per-sweep setting in **Configuration ▸ Sweep engine**, and `current_priority()` reports what the process actually holds, so the setting is checkable rather than aspirational.

The default is **Automatic**, and what that resolves to is the subject of the next section.

---

## Sizing the sweep to the machine that is running it

This engine was written and measured on a 22-processor workstation with NVMe. Its original defaults — one HIGH-priority extractor per logical processor, walker threads at twice the core count — are correct there and were actively hostile everywhere else. On a four-core laptop with a 7200 rpm disk they failed in three separate ways, and none of them involved a GPU:

| What the old default did | Why it hurt |
|---|---|
| Four HIGH-priority processes on four cores | The reader's own Qt thread runs at Normal and now never wins. The window stops repainting, and a window that will not repaint reads as a hung program, not a busy one. |
| Four extractors + eight walkers on one spindle | A 7200 rpm disk has **one head** and serves ~100 random IOPS. Concurrent streams do not read four books at once, they drag the head between four regions. Sequential 150 MB/s collapses to under 2 MB/s. It is *slower* than one worker, with four cores burnt to achieve it. |
| Queue depths as multiples of the core count | Each in-flight result carries a whole book's extracted text. Sized for 64 GB, that is a swap storm on 8 GB. |

`lumen_reader/machine_profile.py` answers four questions — logical processors, installed memory, what kind of volume, and whether it is removable — and `ScanConfig` consumes them wherever a knob is left on `auto`.

### What it measures, and how

- **Rotational media** is read with `IOCTL_STORAGE_QUERY_PROPERTY` against a volume handle opened with **zero** access rights. That is the one form of the call an ordinary user may make: `GENERIC_READ` on `\\.\C:` needs Administrator, and a probe that needs elevation always returns "unknown" for exactly the people this exists to serve. The same handle also yields the bus type, so NVMe, SATA SSD and spinning disk are told apart in one call — no subprocess, no WMI, microseconds.
- **Memory** is `GlobalMemoryStatusEx` via ctypes, and `sysconf` off Windows. Deliberately not `psutil`: sizing must be correct in the frozen build, where no optional dependency is installed.
- **Processors** is the affinity-limited count, not the installed one. A machine that has pinned Lumen to four processors has four.

Every probe degrades to a safe "could not tell" rather than raising. A hardware probe must never be able to stop a book from opening.

### What it decides

| Measurement | Effect on `auto` |
|---|---|
| Volume pays a seek penalty (HDD, card reader, USB) | Fleet capped at **2** processes, walkers at **2**, priority **Normal** |
| ≤ 8 logical processors | Fleet leaves **one core** for the reader |
| ≤ 4 logical processors | Priority never above **Normal** |
| 5–8 logical processors | Priority **Above normal** |
| > 8 logical processors, solid-state | **One HIGH-priority process per core** — the original contract, unchanged |
| < 8 GB RAM | Fleet capped at 4, result queue at 16 books in flight |
| < 4 GB RAM | Fleet capped at 2, result queue at 8 |
| Network share | Fleet capped at 8 — that is latency, not seeks, so workers wait in parallel rather than fighting over a head |

Three properties of this design are deliberate and worth stating:

- **The fleet is sized against the volume the *books* are on**, not the one Lumen is installed on. A library on an external drive tunes to that drive even when the program runs from NVMe.
- **The text budget is never reduced.** Memory pressure is relieved by holding fewer books in flight, not by indexing less of each one. A slower sweep is a fair trade; a quietly less searchable library is not.
- **Unknown storage is not treated as a spindle.** Throttling every machine whose disk we failed to identify would be the worse bug; the processor and memory guards still apply.

### It says so out loud

`ScanConfig.tuning_notes()` returns the reasoning, and it is shown in **Configuration ▸ Sweep engine** under the fleet summary and written into the sweep log as `Machine:` lines:

```
22 extractor processes at HIGH priority (1.00 per logical processor; …)
    · 22 logical processors  ·  16 GB RAM  ·  NVMe solid-state
    · C: is on an NVMe bus.
    · Priority HIGH: this machine has processors to spare.
```

A fleet smaller than the core count looks like a bug until the sentence explaining the disk is sitting next to it. "Lumen decided your computer is slow", unexplained, is indistinguishable from Lumen being broken.

### The user still decides

`auto` applies **only** to a knob left at its default. An explicit setting is obeyed exactly, including one that is bad for the machine — 12 processes at Realtime on a laptop is available to anyone who asks for it. Second-guessing a deliberate choice is how a settings window stops being trusted.

`tests/test_machine_profile.py` pins all of the above by *injecting* the machine rather than detecting it: a test that only passes on the workstation that runs it proves nothing about anyone else's hardware.

---

## Acceleration: one build, adapted at runtime

`lumen_reader/accel.py` exists so that there is exactly one Lumen, which does the fastest thing that is genuinely available on the machine it finds itself on.

### What it detects

| Probe | How | Used for |
|---|---|---|
| CPU topology | `os.cpu_count` and the physical/logical split | Fleet sizing, walker count |
| NVIDIA GPUs | `nvidia-smi` if it is on `PATH` | GPU backends, VRAM capacity |
| DirectStorage runtime | Whether `dstorage.dll` and `dstoragecore.dll` load | NVMe→VRAM streaming |
| Storage bus | PowerShell `Get-PhysicalDisk … BusType` | Whether DirectStorage is worth anything here |

Detection shells out, which costs seconds. It therefore runs on a **background thread started at application launch** (`accel.start_background_probe()`, called from `app.main`), so opening Configuration is instant on every machine — most of all on one with no GPU stack at all, where the probe is pure waiting. The Acceleration tab fills in when the answer lands.

### Backends are a registry, not an `if`

Extraction and search are `Protocol`s with named implementations:

| Stage | Names |
|---|---|
| Extraction | `auto`, `cpu-fleet`, `gpu-directstorage` |
| Search | `auto`, `sqlite-fts5`, `gpu-resident` |

`auto` is the whole point. `resolve_extraction_backend()` returns both the backend that will run **and the reason it won**, and `TurboScanner` resolves it before anything spawns, so the monitor can state which engine is doing the work.

### Honesty is a requirement, not a nicety

Today no GPU kernel is registered in this build, so both `auto` paths resolve to the CPU fleet and SQLite FTS5 — which is what actually runs. Rather than hide that, `extraction_backend_status()` and `search_backend_status()` distinguish the two reasons a fast path is unavailable:

> *"Not on this machine: no CUDA-capable GPU detected."*

> *"Hardware is ready (24 GB VRAM), but no resident index kernel is registered in this build. Registering one switches Lumen over — see `accel.register_search_backend`."*

And a downgrade is always reported through `BackendChoice.fallback_from` rather than swallowed. Silently falling back is how a reader ends up believing the GPU is doing something it is not.

### Capacity, measured

`index_bytes_per_book()` and `capacity_report()` answer "how big does this get?" from measured constants rather than optimism: `INDEX_BYTES_METADATA = 1,200` bytes a row, and FTS5 postings at `1.15×` the text they cover.

The check: a real **27,956-book index came to 7.79 GB** at a 250,000-character text budget — 292 KB a book. The formula lands on 289 KB for the same inputs.

`PRACTICAL_SHARD_BYTES` is 2 TB. SQLite itself allows 2⁶³−1, but FTS5 segment merges stop being pleasant long before that, which is why the answer past that point is more shards rather than a bigger file.

> **Status of sharding.** The addressing function is real and tested — `shard_for()` is an explicit FNV-1a (never `hash()`, whose per-process randomization would send the same book to a different shard on every launch), and `shard_path()` names the files. `capacity_report()` uses both to price a multi-shard index. **The sweep and the search still use one database file.** The shard count in Configuration is a capacity projection and a reserved addressing seam, not yet a live multi-file index. It is documented here as a seam so that nobody reads the capacity number as a description of today's storage layout.

---

## The Configuration window

<kbd>Ctrl+,</kbd>, or **⚙ Configuration** in the header, or **Change folder & settings** on the shelf. One window, six tabs.

### Library

**The library folder** first, because it is the setting that matters most. Lumen reads the path back from disk *as you type* and reports how many books are actually there, so a wrong path is obvious before you save it rather than after a sweep comes back empty. A folder that does not exist is refused; an empty one warns. A **Recent libraries** dropdown remembers the last twelve.

Also here: **what counts as a book** (`.epub` and `.pdf`, plus any extra suffixes you add) and **what to leave out** — skipped directory names, exclude globs, a depth limit, whether to follow symlinks, and minimum/maximum file sizes for shelves littered with zero-byte placeholders.

### Sweep engine

The extractor fleet (how many processes, at what priority), how many directory walkers, how deeply each book is read (the text budget, and a PDF page cap), pipeline plumbing (queue depths and batch sizes), and when Lumen sweeps — on startup, and whether to prune.

### Acceleration & scale

What this machine has, which backends were chosen and why, and the capacity projection described above.

### Index

Where the database is, how big it is, when it was last swept, **Optimise and compact**, and **Libraries in this index** — every root the index knows about, with **Forget this library** and a one-click **Forget the *n* missing libraries** for roots whose folder has gone.

### Search & shelf

Which mode a search starts in, the debounce, the page size, and the snippet width.

### Reading

Theme, EPUB type size, sidebar visibility — and a pointer to the settings that legitimately live in their own windows (**◇ Definer**, **⚡ Speed Reader Studio**).

Saving writes `scan`, `search`, `accel`, `library_root`, `recent_roots`, `theme`, `font_size` and `sidebar_visible` into reader state. **Save and sweep now** does that and starts a sweep in one step.

---

## Measured behaviour

On a real **9,335-book, 15.9 GB library** (8,207 EPUB, 1,128 PDF) on a 16-core / 22-thread machine:

- **9,335 books indexed in about four minutes**, all 22 extractor processes busy.
- Title search afterwards answers in **single-digit milliseconds**.
- A second sweep of the unchanged library costs **one walk** — every book is triaged as already current.

Index scale, measured separately: **27,956 books → 7.79 GB** at a 250,000-character text budget.

---

## Implementation inventory

| File | Responsibility |
|---|---|
| `lumen_reader/turbo_scan.py` | `ScanConfig`, `TurboScanner`, the four pipeline stages, worker telemetry, `extractor_main`, process priority |
| `lumen_reader/library_index.py` | `LibraryIndex`, the schema, the FTS rowid map, generations and pruning, `search`, `optimize`, root management |
| `lumen_reader/accel.py` | Hardware probes, the backend registry and resolution, shard addressing, the capacity model |
| `lumen_reader/scan_monitor.py` | `ScanMonitorDialog`, `CoreGrid`, `Sparkline` |
| `lumen_reader/settings_dialog.py` | `ConfigurationDialog` and its six tabs |
| `lumen_reader/shelf.py` | The shelf, paged index-backed search, sweep status |
| `lumen_reader/ui.py` | `scan_config()`, `rescan_library()`, `show_configuration()`, the <kbd>Ctrl+,</kbd> and <kbd>F5</kbd> shortcuts |
| `reindex.py` | A headless index rebuild and query tool: a library folder, `--no-text`, `--budget`, `--workers`, and `--search` / `--inside` to query the index and exit without opening the reader |

## Validation

| Test file | Covers |
|---|---|
| `tests/test_turbo_scan.py` | Config round-trips, fleet sizing, pipeline behaviour, malformed-record isolation, fatal-writer shutdown, backend resolution and fallback |
| `tests/test_library_index.py` | Schema/migration, strict-Unicode extraction, WAL recovery, triage, generations, pruning, search modes, re-sweeping an upgraded index |
| `tests/test_fts_rowid_map.py` | The map is built once, on the first sweep after the upgrade, and is idempotent |
| `tests/test_scan_monitor.py` | Grid geometry invariants under every fleet size and window size |
| `tests/test_accel.py` | Shard distribution and stability, shard paths, capacity growth, backend status text |

## Known limits

- **Sharding is addressed but not stored.** See the status note above.
- **The search-backend preference is stored but not yet consumed.** `library_index.search()` uses SQLite FTS5 unconditionally. That is the correct outcome today, since no other kernel is registered — but the preference is not wired to the search path, and would need to be the day one is.
- **DirectStorage detection is not DirectStorage support.** The probe reports whether the runtime could load and whether there is NVMe underneath. No GPU extraction kernel ships in this build.
- **`nvidia-smi` only.** AMD and Intel GPUs are not detected, and would report as "no CUDA-capable GPU".
- **Priority control is Windows-only.** On other platforms `apply_process_priority` is a no-op that reports what it actually did.
- **Seek-penalty detection is Windows and Linux only.** Windows uses the storage IOCTL, Linux reads `/sys/block/<dev>/queue/rotational`. On macOS and elsewhere the volume reports `unknown`, which is deliberately *not* treated as rotational — the processor and memory guards still apply, but a Mac with a spinning external drive gets a wider fleet than it should.
- **The seek-penalty query is per volume, not per file.** A library spanning two drives is tuned for the volume its root is on.
- **A RAID or storage pool of spinning disks reports as one seek-bound volume.** Several heads are available but Lumen sizes for one, so such a library sweeps more slowly than the array can manage. Set the process count explicitly to override.
- **`result_queue_depth` is now machine-aware but still has no control in the Configuration window**, so saving settings resets an explicit value to auto.
- **The index is a cache, never a source of truth.** Deleting `library-index.db` costs one sweep and nothing else. Your books, reading positions, notes and marks are untouched by anything in this document.

## Sensible future extensions

- Register a real GPU extraction kernel behind `accel.register_extraction_backend` and let `auto` pick it up with no other change.
- Wire the search-backend preference through to `LibraryIndex.search()`, so a registered resident-index kernel is reachable.
- Make the shard count live: open one connection per shard, sweep and search them in parallel, and let `shard_path()` name the files it already knows how to name.
- Watch the library folder for changes and sweep incrementally, instead of only on demand and at startup.
