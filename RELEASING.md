# Releasing Lumen Book Reader

*Created by Angela López Mendoza · @angelahack1*

One command produces a signed-by-checksum, self-describing, per-user Windows
release with a wizard, an uninstaller, and selectable `.epub` / `.pdf`
registration:

```powershell
& "C:/Program Files/Python312/python.exe" .\build_complete_release.py --bump minor
```

Everything below explains what that command does and why each piece is there.

---

## The one-liners

| Goal | Command |
|---|---|
| Rebuild the current tagged version | `python build_complete_release.py` |
| Ship a **patch** (1.1.0 → 1.1.1) | `python build_complete_release.py --bump patch` |
| Ship a **minor** (1.1.0 → 1.2.0) | `python build_complete_release.py --bump minor` |
| Ship a **major** (1.1.0 → 2.0.0) | `python build_complete_release.py --bump major` |
| A specific version | `python build_complete_release.py --version 2.0.0-rc1` |
| Iterate on the wizard only | `python build_complete_release.py --skip-app --skip-uninstaller` |
| Build without tagging | `python build_complete_release.py --bump minor --no-tag` |

`--bump` creates a **new annotated git tag** and builds it. Push it when
you are happy:

```powershell
git push origin v1.2.0
```

> **The pipeline never rewrites history.** Adding a tag is the only git write
> it performs. There is no rebase, no amend, no reset, no force-push and no tag
> deletion — and no flag that could add one. If the tag already exists it stops
> and tells you. `tests/test_release_scheme.py` enforces this by inspecting the
> actual `git` invocations in the source, not the comments.

---

## The pipeline

```
                    lumen_reader/version.py          ← ONE source of truth
                              │
                    versioning.py (build-time shim)
                              │
   ┌──────────────────────────┼──────────────────────────┐
   ▼                          ▼                          ▼
build.py               build_uninstaller.py       build_installer.py
   │                          │                          │
   ▼                          ▼                          ▼
pkg.zip  ─────────────► Uninstaller.exe ────────► dist/Lumen_Release_v<ver>/
                                                          │
                                            build_complete_release.py
                                                          │
                                                          ▼
                              SHA256SUMS.txt + RELEASE_MANIFEST.json
                              Lumen_Release_v<ver>_win11x64_<stamp>.zip
                                            + .sha256 sidecar
```

### Stage 1 — `build.py` → `pkg.zip`

Freezes `lumen_main.py` (the root shim around `lumen_reader/launcher.py` —
PyInstaller runs its entry script as `__main__` with no package context, so
a relative import there would raise on the user's machine) with PyInstaller
`--onedir --windowed`,
stages the frozen tree plus the PowerShell registrars, the icon, the licence
and `preserved_user_state.json` into `dist/payload/`, and zips that.

**`--onedir` is mandatory.** Lumen renders pages in QtWebEngine, whose helper
process `QtWebEngineProcess.exe` cannot be relaunched reliably out of a
`--onefile` temp extraction.

`pkg.zip`'s contents *are* the installed directory, one for one. The build
then **verifies** the archive: `Lumen.exe`, the `_internal/` tree, the icon,
all four `.ps1` scripts and the preserve list must be present, and
`ZipFile.testzip()` must come back clean, or the build aborts.

### Stage 2 — `build_uninstaller.py` → `Uninstaller.exe`

`--onefile` here, and only here. The uninstaller is copied *into* the
installation and must later delete the folder it is running from; a single
executable can be scheduled for deletion in one step, whereas a `--onedir`
uninstaller would have to delete its own `_internal/` while Windows holds
those DLLs open.

### Stage 3 — `build_installer.py` → the release folder

Freezes `install.py` `--onedir`, renames `dist/Installer/` to
`dist/Lumen_Release_v<version>/`, and **moves** `pkg.zip` and
`Uninstaller.exe` into it under SHA-256 verification — hashed before, hashed
after, refuses to continue on a mismatch. Moving rather than copying keeps a
half-gigabyte package from existing twice on disk.

**`pkg.zip` stays outside the executable on purpose.** Bundling it would make
the PyInstaller bootloader extract 500 MB to `%TEMP%` on every launch; the
window would take 10–20 seconds to appear, the user would conclude nothing
happened and double-click again, and the second instance would lock the
first's DLLs. With the package beside the exe, the wizard opens instantly.

### Stage 4 — `build_complete_release.py` → the distributable

Writes `SHA256SUMS.txt` (standard `sha256sum -c` format) and
`RELEASE_MANIFEST.json` (version, commit, build host, sizes, per-file digests,
which file types it registers) *into* the folder, then zips it and writes a
`.sha256` sidecar for the archive.

---

## Versioning

`lumen_reader/version.py` is the single source of truth, at build time **and**
at runtime. Resolution order:

1. `lumen_reader/_version.py` — generated by `build.py`, gitignored
2. `$LUMEN_VERSION` — exported so all three artefacts share one version
3. `git describe --tags --abbrev=0 --match 'v[0-9]*'`
4. `pyproject.toml`
5. `0.0.0+unknown`

The same module renders the Win32 `VERSIONINFO` resource PyInstaller embeds,
and both wizards read their own version back out of it with
`GetFileVersionInfoW`. So the header badge, Explorer's *Properties ▸ Details*
sheet and the *Installed apps* entry are the same string **by construction**.

If `pyproject.toml` declares a version newer than the newest tag, every build
prints a loud warning naming the tag command you need. That closes the trap
where `pyproject.toml` says `1.1.0`, the newest tag is `v1.0.4`, and the
installer quietly stamps `1.0.4` on `1.1.0` code forever.

---

## What the installer actually does to Windows

Everything is **HKEY_CURRENT_USER**. No administrator rights, no effect on
other users of the machine. `install.py` carries an `asInvoker` manifest so
Windows' installer-detection heuristics do not raise a UAC prompt the install
neither needs nor uses.

| Surface | Key | Why it exists |
|---|---|---|
| Add/Remove Programs | `…\CurrentVersion\Uninstall\LumenBookReader` | Settings ▸ Apps ▸ Installed apps, with a working Uninstall button, size, icon, publisher and date |
| Discovery | `Software\XAIHT\Lumen Book Reader` | A stable machine-readable answer to "where is Lumen, which version, what did it claim" — for a future updater and for the uninstaller when the manifest is gone |
| App Paths | `…\CurrentVersion\App Paths\Lumen.exe` | <kbd>Win</kbd>+<kbd>R</kbd> → `lumen`, and `ShellExecute("Lumen.exe")` |
| Application | `Software\Classes\Applications\Lumen.exe` | Puts Lumen in *Open with ▸ Choose another app* for `.epub`/`.pdf` even when it owns neither |
| ProgIDs | `Software\Classes\Lumen.EpubBook`, `…\Lumen.PdfDocument` | The document types: friendly name, icon, and the `open` / `Read in Lumen` verbs |
| Extension link | `Software\Classes\<ext>\OpenWithProgids` | **Additive.** Adds Lumen to the menu without taking the default |
| Capabilities | `Software\XAIHT\Lumen Book Reader\Capabilities` + `Software\RegisteredApplications` | Lists Lumen in Settings ▸ Apps ▸ **Default apps**, the only route Windows actually blesses |
| Explorer cache | `…\Explorer\FileExts\<ext>\OpenWithProgids` | Explorer keeps its own copy; without it the menu can take a reboot to notice |

### Ticking a type ≠ stealing a type

The dialog offers `.epub` and `.pdf` as **separate tick-boxes**, and
*"Also make Lumen the default app"* as a **separate, unticked** switch.
Ticking `.pdf` adds Lumen to its Open-with menu. Only the second switch writes
the extension's `(Default)` value and clears Explorer's `UserChoice`.

`UserChoice` is hash-protected, so it cannot be forged — a user may delete
their own copy, which returns the choice to our ProgID, but Windows may still
re-prompt. That is Windows asserting the user's right to choose, and the
correct answer is the *Default apps* page, not a louder hack. The wizard says
so, in the dialog.

### Shortcuts start in the *library*, not the install folder

Lumen builds its shelf from the current directory and writes reading marks
into `lumen-reading-marks.json` there. A shortcut rooted at the install folder would
open an empty shelf and drop the reader's notes among the program files. So
the wizard asks for a **library folder** and every shortcut's
`WorkingDirectory` points at it.

For the same reason, `lumen_reader/launcher.py` — the frozen entry point —
`chdir`s to a book's own folder when Explorer opens one through the file
association, and passes `app.main` an absolute path.

---

## The mirror

Every single thing the installer writes, the uninstaller removes:

| install | uninstall |
|---|---|
| create the install directory | remove the install directory |
| extract `pkg.zip` | delete the files (keeping the preserve list) |
| write `LumenInstall.json` | read it, act on it, delete it |
| copy `Uninstaller.exe` | schedule its own deletion after exit |
| register ARP + discovery | delete ARP + discovery |
| `CreateShortcut.ps1` | `RemoveShortcut.ps1` |
| `register_associations.ps1` | `unregister_associations.ps1` |
| refresh the shell | refresh the shell |

**`tests/test_release_scheme.py` is the enforcement.** It fails if a ProgID is
registered but never unregistered, if the two wizards disagree about the
product's identity by a single character, if the progress weights stop summing
to 1.0, if the unregistrar would delete a *shared* extension key outright, if
the registrar claims a default outside the `SetAsDefault` branch, or if either
wizard grows an import it will not have when frozen.

### Asymmetries that are deliberate

* **The registrar writes conditionally; the unregistrar removes
  unconditionally.** An uninstall must be complete even when
  `LumenInstall.json` is missing or describes an older selection.
* **The unregistrar sweeps historical ProgIDs** (`Lumen.Book`,
  `LumenReader.*`). A leftover entry from a renamed release would haunt the
  Open-with menu forever.
* **It only clears an extension's default when that default is still ours.**
  If Acrobat holds `.pdf`, Acrobat keeps it.
* **`uninstall.py` has a pure-`winreg` fallback** for machines where policy
  blocks PowerShell. An association left pointing at a deleted `.exe` is worse
  than a noisy uninstall.

---

## User state

`preserved_user_state.json` is the **one** list, read by three programs.

* `preserve_on_reinstall` — `install.py` skips these when extracting over an
  existing installation, but **only when they already exist**, so a fresh
  install still receives every seed file.
* `keep_on_uninstall` — `uninstall.py` leaves these behind.
* `appdata_dirs` — where reading state lives.

Both programs fall back to a built-in copy if the file cannot be read, and the
test asserts each fallback is a subset of the shared list. **Fail-safe, never
fail-open:** keeping a file by mistake is recoverable; deleting a user's
settings because a JSON file did not parse is not.

Reading positions, bookmarks, notes and tags live in
`%APPDATA%\Lumen Reader`, outside the installation. They **survive an
uninstall** unless the user explicitly ticks the box. Books are never touched,
ever.

---

## Requirements

* Windows 10/11 x64
* **System** Python 3.12 (`C:\Program Files\Python312\python.exe`) with
  `tkinter` — the wizards are Tkinter GUIs
* `pip install -r requirements.txt` into that same interpreter
* PyInstaller (installed automatically if missing)

`build.py` probes every runtime dependency by name *before* PyInstaller starts,
so a missing `PyMuPDF` fails in two seconds with the exact `pip` line to run,
rather than three minutes into an analysis pass.

---

## Verifying a release

```powershell
cd Lumen_Release_v1.2.0
Get-FileHash Installer.exe -Algorithm SHA256      # compare against SHA256SUMS.txt
Get-Content RELEASE_MANIFEST.json | ConvertFrom-Json
```

`RELEASE_MANIFEST.json` records the version, the commit, the build host, the
total size, a digest for every file, and the file types the installer will
offer to register.
