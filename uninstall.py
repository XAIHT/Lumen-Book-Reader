# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""uninstall.py - the Lumen Book Reader removal wizard.

The mirror image of ``install.py``. Everything the installer writes, this
removes; the order is reversed, the palette is the same, and the step
checklist reads like the install run played backwards:

    install                              uninstall
    ─────────────────────────────────    ────────────────────────────────
    create the install directory     ->  remove the install directory
    extract pkg.zip                  ->  delete the files (keeping user state)
    write LumenInstall.json          ->  read LumenInstall.json, then delete it
    copy Uninstaller.exe             ->  schedule Uninstaller.exe's own deletion
    register ARP + discovery         ->  delete ARP + discovery
    CreateShortcut.ps1               ->  RemoveShortcut.ps1
    register_associations.ps1        ->  unregister_associations.ps1
    refresh the shell                ->  refresh the shell

Removal is TOTAL. When this wizard finishes, nothing on the machine says Lumen
was ever installed: not a file, not a shortcut, not a registry value, not an
association, not a shell cache entry. The old build kept ``%APPDATA%\\Lumen
Reader`` unless the user ticked a box, which meant an uninstall left the whole
record of their configuration behind - the opposite of what "uninstall" means.

Exactly two things survive, and each for a reason that outranks tidiness:

  * The user's books. Lumen never puts books in the install folder, but if the
    library folder happens to sit inside it, the wizard says so out loud and
    refuses to touch it. Erasing our own traces must never erase someone's
    library; a reinstall is an afternoon, a deleted book collection is not.
  * Reading positions, marks, notes, quotes and tags - but they do not stay
    where they were. They are EXPORTED first, to a folder the user chooses, as
    one plain JSON file they can keep, move or read without Lumen. Then the
    original is erased with everything else.

That ordering is the whole design: ASK, EXPORT, VERIFY, THEN ERASE. Nothing is
deleted until the export has been written and re-read from disk, because the
only unrecoverable outcome here is erasing a person's reading history and
handing them a file that turns out to be empty.

Self-contained: standard library only, exactly like the installer.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Any

# ─── Product identity (must match install.py) ────────────────────────────────
PRODUCT_NAME = "Lumen Book Reader"
FOLDER_NAME = "Lumen Book Reader"
EXE_NAME = "Lumen.exe"
ICON_NAME = "lumen.ico"
ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LumenBookReader"
DISCOVERY_KEY = r"Software\XAIHT\Lumen Book Reader"
MANIFEST_NAME = "LumenInstall.json"
APPDATA_DIR_NAME = "Lumen Reader"        # QApplication.setOrganizationName

#: Must match ``lumen_reader.marks.MARKS_FILENAME``.  It is duplicated rather
#: than imported because this wizard is deliberately standard-library only - it
#: has to run after the application it is deleting is already gone.
MARKS_FILENAME = "lumen-reading-marks.json"


# ─── Version resolution (identical contract to install.py) ───────────────────

def _read_exe_product_version(exe_path: str) -> str:
    """Read the Win32 ``ProductVersion`` string out of an EXE's VERSIONINFO."""
    if sys.platform != "win32":
        return ""
    try:
        ver = ctypes.windll.version

        get_size = ver.GetFileVersionInfoSizeW
        get_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        get_size.restype = wintypes.DWORD

        get_info = ver.GetFileVersionInfoW
        get_info.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                             wintypes.DWORD, ctypes.c_void_p]
        get_info.restype = wintypes.BOOL

        query = ver.VerQueryValueW
        query.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                          ctypes.POINTER(ctypes.c_void_p),
                          ctypes.POINTER(wintypes.UINT)]
        query.restype = wintypes.BOOL

        handle = wintypes.DWORD(0)
        size = get_size(exe_path, ctypes.byref(handle))
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not get_info(exe_path, 0, size, buf):
            return ""
        for codepage in ("040904B0", "040904E4", "000004B0"):
            value = ctypes.c_void_p(0)
            length = wintypes.UINT(0)
            sub = f"\\StringFileInfo\\{codepage}\\ProductVersion"
            if query(buf, sub, ctypes.byref(value), ctypes.byref(length)):
                if value.value and length.value > 0:
                    return ctypes.wstring_at(value.value, length.value).rstrip("\x00")
    except Exception:
        return ""
    return ""


def _derive_version_from_git() -> str:
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    tag = (result.stdout or "").strip()
    return tag[1:] if tag.startswith("v") else tag


def resolve_version() -> str:
    """Frozen -> our own VERSIONINFO; then the installed app's; then git."""
    if getattr(sys, "frozen", False):
        version = _read_exe_product_version(sys.executable)
        if version:
            return version
    return _derive_version_from_git()


# ─── Diagnostics ─────────────────────────────────────────────────────────────
# A frozen --windowed wizard has no stdout: CPython's print() silently returns
# when sys.stdout is None, so every diagnostic in this file would evaporate
# exactly when someone needs it. Point stdout at a log file instead, and put
# the path in the failure dialog so the user can tell us what happened.

LOG_NAME = "uninstall.log"


def redirect_output_to_log(name: str = LOG_NAME) -> str:
    """Send print()/traceback output to a log file. Returns its path."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    directory = os.path.join(base, PRODUCT_NAME, "logs")
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return ""
    stamp = datetime.now().isoformat(timespec="seconds")
    handle.write(f"\n===== {stamp}  {PRODUCT_NAME} uninstall =====\n")
    sys.stdout = handle
    sys.stderr = handle
    return path


LOG_PATH = ""


# ─── Colour palette (identical to install.py - the pair must look like a pair)
BG_DARK       = "#0d1117"
BG_PANEL      = "#141b24"
BG_CARD       = "#111820"
BG_INPUT      = "#1b2530"
FG_PRIMARY    = "#e8e3d8"
FG_SECONDARY  = "#8d9aa8"
FG_DIM        = "#5a6673"
ACCENT        = "#63d1ad"
ACCENT_HOVER  = "#7ee9c4"
ACCENT_DEEP   = "#3fa588"
GOLD          = "#f2bd4d"
SUCCESS       = "#63d1ad"
WARNING       = "#f2bd4d"
ERROR         = "#ff7c52"
BTN_BG        = "#3a2028"          # the removal button reads warm, not green
BTN_HOVER     = "#552f3a"
BTN_CANCEL_BG = "#1b2530"
BTN_CANCEL_HV = "#26303c"
PROGRESS_BG   = "#1b2530"
BORDER_COLOR  = "#26303c"
FONT_FAMILY   = "Segoe UI"


class LumenUninstaller:
    """The dark-themed removal wizard for Lumen Book Reader."""

    STEPS = [
        ("Saving your reading data…",           0.10),
        ("Removing shortcuts…",                 0.07),
        ("Unregistering file associations…",    0.13),
        ("Removing the Windows registration…",  0.08),
        ("Deleting application files…",         0.45),
        ("Erasing configuration and caches…",   0.09),
        ("Refreshing the Windows shell…",       0.08),
    ]

    # Mirrors install.py's fallback. FAIL-SAFE: when the shared list cannot be
    # read we still keep these, because deleting a user's library because a
    # JSON file did not parse is not a recoverable mistake.
    _KEEP_FALLBACK = ("library",)

    def __init__(self, root: tk.Tk):
        self.root = root
        self.version = resolve_version()
        title = f"{PRODUCT_NAME} Uninstaller"
        if self.version:
            title += f"  v{self.version}"
        self.root.title(title)
        self.root.configure(bg=BG_DARK)
        self.root.resizable(False, False)

        w, h = 720, 700
        sx = (self.root.winfo_screenwidth() - w) // 2
        sy = max((self.root.winfo_screenheight() - h) // 2 - 20, 0)
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        detected, source = self._detect_install_path()
        self.install_path = tk.StringVar(value=detected)
        self._detection_source = source

        # Where the export goes, and whether it happens at all.  The DEFAULT is
        # to save: an uninstall that silently discards someone's reading history
        # because they did not notice a checkbox is the mistake this wizard
        # exists to avoid.  Opting out is possible, but it must be deliberate.
        self.save_path = tk.StringVar(value=self._default_save_dir())
        self.skip_save_var = tk.BooleanVar(value=False)

        self._progress_value = 0.0
        self._uninstalling = False
        self._summary: list[str] = []
        self._kept: list[str] = []
        self._export_path = ""
        self._export_counts: dict[str, int] = {}

        self._build_ui()

    # ─── Where is Lumen? Three independent answers, in order of trust ────
    @staticmethod
    def _base_dir() -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.abspath(os.path.dirname(__file__))

    @classmethod
    def _detect_install_path(cls) -> tuple[str, str]:
        """Return ``(path, how_we_found_it)``.

        1. We are almost always sitting IN the installation - Uninstaller.exe is
           copied there. Trust that first.
        2. ``LumenInstall.json`` beside us.
        3. The discovery key the installer wrote.
        4. The Add/Remove entry's ``InstallLocation``.
        """
        base = cls._base_dir()
        if os.path.isfile(os.path.join(base, EXE_NAME)):
            return base, "the folder this uninstaller is running from"

        config_path = os.path.join(base, MANIFEST_NAME)
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8-sig") as fh:
                    install_dir = json.load(fh).get("InstallDir", "")
                if install_dir and os.path.isdir(install_dir):
                    return install_dir, MANIFEST_NAME
            except Exception:
                pass

        if sys.platform == "win32":
            try:
                import winreg
                for key_path, value_name in (
                    (DISCOVERY_KEY, "InstallLocation"),
                    (ARP_KEY, "InstallLocation"),
                ):
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                            value, _ = winreg.QueryValueEx(key, value_name)
                        if value and os.path.isdir(value):
                            return value, f"the registry ({key_path})"
                    except FileNotFoundError:
                        continue
            except Exception:
                pass

        return "", "not found"

    # ─── UI construction ─────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self._build_header()

        body = tk.Frame(self.root, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=26, pady=(16, 20))
        card = tk.Frame(body, bg=BG_PANEL, highlightbackground=BORDER_COLOR,
                        highlightthickness=1)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=BG_PANEL)
        inner.pack(fill="both", expand=True, padx=22, pady=16)

        self._build_path_section(inner)
        self._rule(inner)
        self._build_removal_list(inner)
        self._rule(inner)
        self._build_data_section(inner)
        self._build_progress_section(inner)
        self._build_buttons(inner)

    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg=BG_CARD, height=96)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ERROR, height=3).pack(fill="x")

        row = tk.Frame(hdr, bg=BG_CARD)
        row.pack(fill="both", expand=True)
        tk.Label(row, text="◇", font=(FONT_FAMILY, 26), bg=BG_CARD, fg=ERROR
                 ).pack(side="left", padx=(22, 12), pady=(10, 0))
        titles = tk.Frame(row, bg=BG_CARD)
        titles.pack(side="left", pady=(16, 0))
        tk.Label(titles, text=PRODUCT_NAME, font=(FONT_FAMILY, 19, "bold"),
                 bg=BG_CARD, fg=FG_PRIMARY).pack(anchor="w")
        tk.Label(titles, text="Removal wizard", font=(FONT_FAMILY, 10),
                 bg=BG_CARD, fg=FG_SECONDARY).pack(anchor="w")
        self._build_version_badge(row)

    def _build_version_badge(self, parent: tk.Frame) -> None:
        if not self.version:
            return
        outer = tk.Frame(parent, bg=ERROR, highlightthickness=0, bd=0)
        outer.pack(side="right", padx=(0, 24), pady=(22, 0))
        badge = tk.Frame(outer, bg=BG_INPUT)
        badge.pack(padx=1, pady=1)
        tk.Label(badge, text="VERSION", font=(FONT_FAMILY, 7, "bold"),
                 bg=BG_INPUT, fg=FG_SECONDARY).pack(padx=15, pady=(5, 0))
        tk.Label(badge, text=f"v{self.version}", font=(FONT_FAMILY, 12, "bold"),
                 bg=BG_INPUT, fg=ERROR).pack(padx=15, pady=(0, 5))

    def _rule(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", pady=9)

    def _build_path_section(self, inner: tk.Frame) -> None:
        tk.Label(inner, text="INSTALLATION TO REMOVE", font=(FONT_FAMILY, 9, "bold"),
                 bg=BG_PANEL, fg=FG_SECONDARY).pack(anchor="w")
        hint = (f"Found via {self._detection_source}."
                if self.install_path.get()
                else "Lumen was not found automatically - please point to its folder.")
        tk.Label(inner, text=hint, font=(FONT_FAMILY, 8), bg=BG_PANEL,
                 fg=FG_DIM).pack(anchor="w", pady=(0, 6))

        row = tk.Frame(inner, bg=BG_PANEL)
        row.pack(fill="x", pady=(0, 4))
        self.path_entry = tk.Entry(
            row, textvariable=self.install_path, font=(FONT_FAMILY, 10),
            bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=ERROR, relief="flat",
            bd=0, highlightthickness=1, highlightbackground=BORDER_COLOR,
            highlightcolor=ERROR,
        )
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        self.browse_btn = self._make_button(row, "Browse", self._browse,
                                            width=10, small=True)
        self.browse_btn.pack(side="right")

    def _build_removal_list(self, inner: tk.Frame) -> None:
        tk.Label(inner, text="THIS WILL REMOVE", font=(FONT_FAMILY, 9, "bold"),
                 bg=BG_PANEL, fg=FG_SECONDARY).pack(anchor="w", pady=(0, 6))
        items = [
            "The application files in the folder above",
            "Desktop and Start menu shortcuts",
            "The .epub and .pdf associations Lumen registered",
            "Lumen's entry in Settings ▸ Apps ▸ Installed apps",
            "Every Lumen registry key under HKEY_CURRENT_USER",
            "All configuration: theme, fonts, sweep, search, acceleration",
            "The library index and every cache, wherever they live",
            "Explorer's recent-files and icon-cache traces of Lumen",
        ]
        for text in items:
            tk.Label(inner, text=f"   ✗   {text}", font=(FONT_FAMILY, 9),
                     bg=BG_PANEL, fg=FG_PRIMARY, anchor="w").pack(fill="x")

    def _build_data_section(self, inner: tk.Frame) -> None:
        tk.Label(inner, text="YOUR BOOKS AND YOUR READING", font=(FONT_FAMILY, 9, "bold"),
                 bg=BG_PANEL, fg=FG_SECONDARY).pack(anchor="w")
        tk.Label(
            inner,
            text="Your books are never touched. Where you were in every book — plus "
                 "your marks, notes, quotes and tags — is SAVED to the folder below "
                 "as one file you keep, and everything else Lumen ever wrote is then "
                 "erased from this machine.",
            font=(FONT_FAMILY, 8), bg=BG_PANEL, fg=FG_DIM, justify="left",
            # Let Tk wrap it. A hand-placed newline only looks right at one font
            # size on one DPI, and the installer already shipped a label that
            # overflowed its card and clipped the sentence at both ends.
            wraplength=600, anchor="w",
        ).pack(anchor="w", pady=(0, 6), fill="x")

        row = tk.Frame(inner, bg=BG_PANEL)
        row.pack(fill="x")
        self.save_entry = tk.Entry(
            row, textvariable=self.save_path, font=(FONT_FAMILY, 9),
            bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER_COLOR, highlightcolor=ACCENT,
        )
        self.save_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._make_button(row, "Choose…", self._browse_save, width=10,
                          small=True).pack(side="left", padx=(8, 0))

        self.skip_check = tk.Checkbutton(
            inner,
            text="Don't save it — erase my reading history too (cannot be undone)",
            variable=self.skip_save_var, command=self._sync_save_row,
            font=(FONT_FAMILY, 9),
            bg=BG_PANEL, fg=FG_PRIMARY, activebackground=BG_PANEL,
            activeforeground=ERROR, selectcolor=BG_INPUT, highlightthickness=0,
            bd=0, cursor="hand2", anchor="w",
        )
        self.skip_check.pack(anchor="w", pady=(6, 0))

    def _sync_save_row(self) -> None:
        """Grey the destination out when the user has opted out of saving.

        A live entry box under a ticked "don't save" is a contradiction, and the
        one thing this screen cannot afford is a user who is unsure whether
        their reading history is being kept.
        """
        skipping = bool(self.skip_save_var.get())
        self.save_entry.configure(
            state="disabled" if skipping else "normal",
            fg=FG_DIM if skipping else FG_PRIMARY,
        )

    def _browse_save(self) -> None:
        if self.skip_save_var.get():
            return
        current = self.save_path.get().strip() or self._default_save_dir()
        chosen = filedialog.askdirectory(
            title="Where should your reading data be saved?",
            initialdir=current if os.path.isdir(current) else self._default_save_dir(),
        )
        if chosen:
            self.save_path.set(os.path.normpath(chosen))

    def _build_progress_section(self, inner: tk.Frame) -> None:
        self.progress_frame = tk.Frame(inner, bg=BG_PANEL)
        tk.Frame(self.progress_frame, bg=BORDER_COLOR, height=1).pack(fill="x", pady=(10, 8))
        self.step_label = tk.Label(self.progress_frame, text="Waiting…",
                                   font=(FONT_FAMILY, 10), bg=BG_PANEL,
                                   fg=FG_PRIMARY, anchor="w")
        self.step_label.pack(fill="x", pady=(0, 4))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "LumenRemove.Horizontal.TProgressbar",
            troughcolor=PROGRESS_BG, background=ERROR,
            darkcolor=ERROR, lightcolor=GOLD, bordercolor=BG_PANEL, thickness=16,
        )
        self.progress_bar = ttk.Progressbar(
            self.progress_frame, style="LumenRemove.Horizontal.TProgressbar",
            orient="horizontal", mode="determinate", maximum=100,
        )
        self.progress_bar.pack(fill="x", pady=(0, 2))
        self.pct_label = tk.Label(self.progress_frame, text="0 %",
                                  font=(FONT_FAMILY, 9, "bold"), bg=BG_PANEL,
                                  fg=ERROR, anchor="e")
        self.pct_label.pack(fill="x")

        checklist = tk.Frame(self.progress_frame, bg=BG_PANEL)
        checklist.pack(fill="x", pady=(6, 0))
        self.check_labels: list[tk.Label] = []
        for desc, _weight in self.STEPS:
            lbl = tk.Label(checklist, text=f"   ○  {desc}", font=(FONT_FAMILY, 9),
                           bg=BG_PANEL, fg=FG_DIM, anchor="w")
            lbl.pack(fill="x")
            self.check_labels.append(lbl)

    def _build_buttons(self, inner: tk.Frame) -> None:
        row = tk.Frame(inner, bg=BG_PANEL)
        row.pack(side="bottom", fill="x", pady=(12, 0))
        self.cancel_btn = self._make_button(row, "Keep Lumen", self._on_cancel,
                                            cancel=True)
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.uninstall_btn = self._make_button(row, "◇  Uninstall",
                                               self._start_uninstall)
        self.uninstall_btn.pack(side="right")
        self.path_entry.bind("<Return>", self._on_enter_key)
        self.root.bind("<Return>", self._on_enter_key)
        self.root.bind("<Escape>", lambda _e: self._on_cancel())

    def _make_button(self, parent, text, command, width=14, small=False,
                     cancel=False) -> tk.Button:
        bg = BTN_CANCEL_BG if cancel else BTN_BG
        hover = BTN_CANCEL_HV if cancel else BTN_HOVER
        fg = FG_SECONDARY if cancel else FG_PRIMARY
        font = (FONT_FAMILY, 9) if small else (FONT_FAMILY, 10, "bold")
        btn = tk.Button(
            parent, text=text, command=command, font=font, bg=bg, fg=fg,
            activebackground=hover, activeforeground=FG_PRIMARY, relief="flat",
            bd=0, cursor="hand2", padx=14, pady=6, width=width,
        )
        btn.bind("<Enter>", lambda _e, b=btn, c=hover: b.config(bg=c))
        btn.bind("<Leave>", lambda _e, b=btn, c=bg: b.config(bg=c))
        return btn

    # ─── Paths ───────────────────────────────────────────────────────────
    @staticmethod
    def _appdata_path() -> str:
        roaming = os.environ.get("APPDATA", "")
        if roaming:
            return os.path.join(roaming, APPDATA_DIR_NAME)
        return os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                            APPDATA_DIR_NAME)

    # ─── The export: everything the user made, in one portable file ──────
    @staticmethod
    def _shell_folder(name: str) -> str:
        """A real shell folder, resolved the way Windows actually stores it.

        ``~\\Desktop`` is a guess, and on any machine with OneDrive folder
        protection turned on it is the WRONG guess - the real Desktop is
        ``%USERPROFILE%\\OneDrive\\Desktop`` and the literal one is an empty
        leftover.  Offering to save someone's reading history into a folder
        they never look at is the same as not saving it, so the redirect is
        read from the registry where Explorer keeps it.
        """
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\User Shell Folders",
                ) as key:
                    raw, _kind = winreg.QueryValueEx(key, name)
                resolved = os.path.expandvars(str(raw))
                if resolved and os.path.isdir(resolved):
                    return resolved
            except Exception:
                pass
        fallback = os.path.join(os.path.expanduser("~"), name)
        return fallback if os.path.isdir(fallback) else os.path.expanduser("~")

    @classmethod
    def _default_save_dir(cls) -> str:
        return cls._shell_folder("Desktop")

    def _collect_reading_data(self) -> tuple[dict, dict[str, int]]:
        """Gather positions, marks, notes, quotes and tags - and nothing else.

        Deliberately selective.  ``reader-state.json`` mixes the two things this
        uninstall treats oppositely: ``books`` and ``recent_books`` are the
        user's reading, while ``theme``, ``font_size``, ``scan``, ``search`` and
        ``accel`` are configuration the user asked to see erased.  Copying the
        whole file would smuggle the configuration back out in the export.

        ``books`` is keyed by a hash of ``path|size|mtime``, so it is exported
        verbatim: re-computing that key needs the same three inputs, and any
        rewriting here would silently break the match on reinstall.
        """
        payload: dict[str, Any] = {
            "_what": "Lumen Book Reader - your reading data, exported at uninstall",
            "_created_by": "Angela López Mendoza · @angelahack1",
            "_exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "_restore": (
                "Keep this file. Reinstall Lumen, then use it to bring back where "
                "you were in every book along with your marks, notes and tags."
            ),
            "positions": {},
            "recent_books": [],
            "reading_marks": [],
        }
        counts = {"positions": 0, "recent": 0, "marks": 0, "sources": 0}

        state = os.path.join(self._appdata_path(), "Lumen", "reader-state.json")
        for candidate in (state, os.path.join(self._appdata_path(), "reader-state.json")):
            doc = self._read_json(candidate)
            if not isinstance(doc, dict):
                continue
            books = doc.get("books")
            if isinstance(books, dict) and books:
                payload["positions"].update(books)
                counts["sources"] += 1
            recent = doc.get("recent_books")
            if isinstance(recent, list) and not payload["recent_books"]:
                payload["recent_books"] = recent
        counts["positions"] = len(payload["positions"])
        counts["recent"] = len(payload["recent_books"])

        seen_marks: set[str] = set()
        for marks_file in self._find_marks_files():
            doc = self._read_json(marks_file)
            entries = doc.get("marks") if isinstance(doc, dict) else doc
            if not isinstance(entries, list):
                continue
            counts["sources"] += 1
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Same mark can sit in the library copy and an old migration
                # copy; keep one, and keep the first (newest path searched).
                key = str(entry.get("id") or "") or json.dumps(entry, sort_keys=True)
                if key in seen_marks:
                    continue
                seen_marks.add(key)
                payload["reading_marks"].append(entry)
        counts["marks"] = len(payload["reading_marks"])
        return payload, counts

    @staticmethod
    def _read_json(path: str) -> Any:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except Exception:
            return None

    def _find_marks_files(self) -> list[str]:
        """Every ``lumen-reading-marks.json`` this machine still has.

        Marks live *beside the library*, not in AppData, so there is no single
        place to look.  The recent-books list is the only record of where the
        user's libraries actually were, which makes it the map for this search.
        """
        found: list[str] = []

        def offer(path: str) -> None:
            if path and os.path.isfile(path) and path not in found:
                found.append(path)

        appdata = self._appdata_path()
        for base in (os.path.join(appdata, "Lumen"), appdata):
            offer(os.path.join(base, MARKS_FILENAME))

        state = self._read_json(os.path.join(appdata, "Lumen", "reader-state.json"))
        recent = state.get("recent_books") if isinstance(state, dict) else None
        for item in recent or []:
            book = item.get("path") if isinstance(item, dict) else None
            if book:
                offer(os.path.join(os.path.dirname(str(book)), MARKS_FILENAME))

        target = self.install_path.get().strip()
        if target:
            offer(os.path.join(target, MARKS_FILENAME))
            offer(os.path.join(target, "library", MARKS_FILENAME))
        return found

    def _export_reading_data(self, destination: str) -> tuple[str, dict[str, int]]:
        """Write the export, then read it back before anyone deletes anything.

        The read-back is not paranoia: this file is about to become the only
        copy that exists.  A full disk, a synced folder that rejects the write,
        or an antivirus that quarantines it mid-save all produce a plausible
        success and an unusable file, and by then the original is gone.
        """
        payload, counts = self._collect_reading_data()
        os.makedirs(destination, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(destination, f"Lumen-reading-data-{stamp}.json")
        serial = 2
        while os.path.exists(path):
            path = os.path.join(destination,
                                f"Lumen-reading-data-{stamp}-{serial}.json")
            serial += 1

        temporary = path + ".part"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

        verified = self._read_json(path)
        if not isinstance(verified, dict) or len(verified.get("positions", {})) != counts["positions"]:
            raise OSError(
                f"the export at {path} could not be read back correctly - "
                f"nothing has been deleted"
            )
        print(f"[UNINSTALL] Exported reading data -> {path} "
              f"({counts['positions']} positions, {counts['marks']} marks)")
        return path, counts

    def _browse(self) -> None:
        current = self.install_path.get().strip()
        chosen = filedialog.askdirectory(
            title=f"Locate the {FOLDER_NAME} folder",
            initialdir=current if os.path.isdir(current) else os.path.expanduser("~"),
        )
        if chosen:
            self.install_path.set(os.path.normpath(chosen))

    def _validate(self) -> str | None:
        raw = self.install_path.get().strip()
        if not raw:
            messagebox.showwarning(
                "No folder selected",
                f"Please point the uninstaller at the {FOLDER_NAME} folder.")
            return None
        if not os.path.isdir(raw):
            messagebox.showerror("Folder not found",
                                 f"This folder does not exist:\n{raw}")
            return None

        target = os.path.abspath(raw)

        # REFUSE to delete something that is not a Lumen installation. Someone
        # who browses to C:\Users\<name>\Documents by mistake must not lose it.
        looks_right = (
            os.path.isfile(os.path.join(target, EXE_NAME))
            or os.path.isfile(os.path.join(target, MANIFEST_NAME))
            or os.path.isfile(os.path.join(target, "unregister_associations.ps1"))
        )
        if not looks_right:
            messagebox.showerror(
                "That is not a Lumen installation",
                f"{target}\n\ndoes not contain {EXE_NAME} or {MANIFEST_NAME}.\n\n"
                "The uninstaller will not delete a folder it cannot identify as "
                "Lumen. Please choose the correct folder.",
            )
            return None

        # The destination has to be usable BEFORE anything is deleted - a
        # save folder that turns out to be unwritable after the erase is the
        # one failure this design cannot recover from.
        if not self.skip_save_var.get():
            destination = self.save_path.get().strip()
            if not destination:
                messagebox.showwarning(
                    "Nowhere to save your reading data",
                    "Choose a folder for your reading data, or tick the box to "
                    "erase it along with everything else.")
                return None
            try:
                os.makedirs(destination, exist_ok=True)
                probe = os.path.join(destination, ".lumen-write-test")
                with open(probe, "w", encoding="utf-8") as handle:
                    handle.write("ok")
                os.remove(probe)
            except OSError as exc:
                messagebox.showerror(
                    "That folder cannot be written to",
                    f"{destination}\n\n{exc}\n\nNothing has been removed. Please "
                    "choose a folder you can write to.")
                return None

        # A "delete everything" confirmation must state what is about to go.
        if self.skip_save_var.get():
            fate = ("\n\nYour reading positions, marks, notes, quotes and tags "
                    "will be ERASED with everything else. This cannot be undone.")
        else:
            fate = ("\n\nYour reading positions, marks, notes, quotes and tags "
                    f"will first be saved to:\n{self.save_path.get().strip()}")
        confirmed = messagebox.askyesno(
            "Remove Lumen Book Reader?",
            f"This will delete:\n{target}\n\nAND every other trace of Lumen on "
            f"this machine — all configuration, the library index, every cache, "
            f"and every registry key."
            + fate
            + "\n\nYour books are not touched. Continue?",
            icon="warning", default="no",
        )
        return target if confirmed else None

    def _on_enter_key(self, _event=None):
        self._start_uninstall()
        return "break"

    def _on_cancel(self) -> None:
        if self._uninstalling:
            leave = messagebox.askyesno(
                "Removal in progress",
                "Lumen is still being removed.\n\n"
                "Quitting now leaves a partial installation behind. Quit anyway?")
            if not leave:
                return
        self.root.destroy()

    # ─── Kick off ────────────────────────────────────────────────────────
    def _start_uninstall(self) -> None:
        if self._uninstalling:
            return
        target = self._validate()
        if target is None:
            return
        self._uninstalling = True
        for widget in (self.uninstall_btn, self.browse_btn, self.path_entry,
                       self.skip_check, self.save_entry):
            widget.config(state="disabled")
        self.progress_frame.pack(
            fill="x", before=self.progress_frame.master.winfo_children()[-1])
        threading.Thread(target=self._run_uninstall, args=(target,),
                         daemon=True).start()

    # ─── Progress plumbing ───────────────────────────────────────────────
    def _set_progress(self, value: float, status: str | None = None) -> None:
        self._progress_value = value
        self.root.after(0, self._update_progress_ui, value, status)

    def _update_progress_ui(self, value: float, status: str | None) -> None:
        pct = min(int(value * 100), 100)
        self.progress_bar["value"] = pct
        self.pct_label.config(text=f"{pct} %")
        if status:
            self.step_label.config(text=status)

    def _mark_step(self, idx: int, success: bool = True, note: str = "") -> None:
        colour = SUCCESS if success else WARNING
        icon = "✓" if success else "!"
        desc = self.STEPS[idx][0] + (f"   {note}" if note else "")
        self.root.after(0, lambda: self.check_labels[idx].config(
            text=f"   {icon}  {desc}", fg=colour))

    def _activate_step(self, idx: int) -> None:
        desc = self.STEPS[idx][0]
        self.root.after(0, lambda: self.check_labels[idx].config(
            text=f"   ▸  {desc}", fg=ERROR))

    # ─── The uninstall pipeline (background thread) ──────────────────────
    def _run_uninstall(self, target: str) -> None:
        try:
            cumulative = 0.0
            self._summary = []
            notes: list[str] = []

            # Step OUT of the folder before deleting it. Windows will not remove
            # a directory that is any process's current directory, and
            # Uninstaller.exe is normally launched from inside the very folder
            # it is about to delete - so without this the final rmdir can never
            # succeed and an empty installation folder is left behind.
            try:
                os.chdir(os.environ.get("SystemRoot") or "C:\\")
            except OSError:
                pass

            # ── 0. Export FIRST ─────────────────────────────────────────
            # Before a single file is touched. If this raises, the exception
            # handler below aborts the whole uninstall with everything still
            # in place - which is the only correct outcome when the user's
            # only copy could not be written.
            idx = 0
            self._activate_step(idx)
            if self.skip_save_var.get():
                self._mark_step(idx, True, "(skipped, as you asked)")
            else:
                self._set_progress(0.0, "Saving your reading data…")
                self._export_path, self._export_counts = self._export_reading_data(
                    self.save_path.get().strip() or self._default_save_dir()
                )
                self._mark_step(
                    idx, True,
                    f"({self._export_counts['positions']} positions, "
                    f"{self._export_counts['marks']} marks)",
                )
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)

            # ── 1. Shortcuts ────────────────────────────────────────────
            idx = 1
            self._activate_step(idx)
            self._set_progress(cumulative, "Removing shortcuts…")
            ok, detail = self._run_ps1("RemoveShortcut.ps1", target)
            if not ok:
                notes.append(f"shortcuts: {detail}")
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, ok, "" if ok else "(see log)")

            # ── 2. File associations ────────────────────────────────────
            idx = 2
            self._activate_step(idx)
            self._set_progress(cumulative, "Unregistering file associations…")
            ok, detail = self._run_ps1("unregister_associations.ps1", target)
            if not ok:
                # Fall back to a pure-Python sweep. An association left behind
                # points at an .exe we are about to delete, which means broken
                # icons and a dead double-click for the rest of time.
                ok = self._purge_associations_via_winreg()
                if not ok:
                    notes.append(f"associations: {detail}")
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, ok, "" if ok else "(see log)")

            # ── 3. Registry: ARP + discovery ────────────────────────────
            idx = 3
            self._activate_step(idx)
            self._set_progress(cumulative, "Removing the Windows registration…")
            self._delete_registry_tree(ARP_KEY)
            self._delete_registry_tree(DISCOVERY_KEY)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx)

            # ── 4. Files ────────────────────────────────────────────────
            idx = 4
            self._activate_step(idx)
            weight = self.STEPS[idx][1]
            removed, failed = self._remove_files(target, cumulative, weight)
            cumulative += weight
            self._set_progress(cumulative)
            self._mark_step(idx, not failed,
                            f"({removed} removed)" if not failed
                            else f"({failed} in use)")
            if failed:
                notes.append(f"{failed} file(s) were locked and will be removed "
                             f"when you next restart Windows")

            # ── 5. Configuration and caches: unconditional ──────────────
            # Not a choice any more. Everything worth keeping left in the
            # export at step 0; what is here now is configuration and a
            # rebuildable index, and an uninstall that leaves those behind is
            # not an uninstall.
            idx = 5
            self._activate_step(idx)
            self._set_progress(cumulative, "Erasing configuration and caches…")
            erased = self._erase_all_traces()
            self._mark_step(idx, True, f"({erased} location(s) erased)")
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)

            # ── 6. Shell refresh ────────────────────────────────────────
            idx = 6
            self._activate_step(idx)
            self._set_progress(cumulative, "Refreshing the Windows shell…")
            self._refresh_shell()
            self._set_progress(1.0, "Removal complete")
            self._mark_step(idx)

            self._summary = self._build_summary(target, notes)
            self.root.after(0, self._show_success, target)

        except Exception as exc:            # noqa: BLE001 - surfaced to the user
            self.root.after(0, self._show_error, str(exc))

    # ─── Preserve list ───────────────────────────────────────────────────
    def _keep_names(self, target: str) -> set[str]:
        """Top-level names inside the install directory that must NOT be deleted."""
        candidate = os.path.join(target, "preserved_user_state.json")
        try:
            with open(candidate, "r", encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            names = doc.get("keep_on_uninstall")
            if isinstance(names, list):
                return {str(n).strip().lower() for n in names if str(n).strip()}
        except Exception:
            pass
        return {n.lower() for n in self._KEEP_FALLBACK}

    # ─── File removal ────────────────────────────────────────────────────
    @staticmethod
    def _on_rmtree_error(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWUSR | stat.S_IREAD)
            func(path)
        except Exception:
            pass

    def _remove_files(self, target: str, cumulative: float,
                      weight: float) -> tuple[int, int]:
        """Delete the installation, keeping whatever the shared list protects.

        ``Uninstaller.exe`` is running from inside the folder it is deleting, so
        Windows will not let it remove itself. It is left for last and handed to
        a detached ``cmd`` that waits for our process to exit - the standard
        self-delete, and the only one that leaves no stub behind.
        """
        keep = self._keep_names(target)
        self._kept = []
        removed = 0
        failed = 0

        self_exe = ""
        if getattr(sys, "frozen", False):
            self_exe = os.path.abspath(sys.executable)

        entries = sorted(os.listdir(target))
        total = max(len(entries), 1)
        for i, name in enumerate(entries, 1):
            path = os.path.join(target, name)
            if name.lower() in keep:
                self._kept.append(name)
                self._set_progress(cumulative + weight * (i / total),
                                   f"Keeping {name}…")
                continue
            if self_exe and os.path.abspath(path) == self_exe:
                # Deleted after we exit; see _schedule_self_delete.
                continue
            self._set_progress(cumulative + weight * (i / total),
                               f"Removing {name}…")
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, onerror=self._on_rmtree_error)
                else:
                    os.chmod(path, stat.S_IWRITE)
                    os.remove(path)
                removed += 1
            except Exception as exc:
                print(f"[UNINSTALL] could not remove {path}: {exc}")
                failed += 1

        if self._kept:
            print(f"[UNINSTALL] Kept: {', '.join(self._kept)}")
        return removed, failed

    @staticmethod
    def _remove_tree(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        try:
            shutil.rmtree(path, onerror=LumenUninstaller._on_rmtree_error)
            print(f"[UNINSTALL] Removed {path}")
            return not os.path.isdir(path)
        except Exception as exc:
            print(f"[UNINSTALL] could not remove {path}: {exc}")
            return False

    def _schedule_self_delete(self, target: str) -> None:
        """Delete Uninstaller.exe (and the now-empty folder) after we exit.

        Only ever runs when we are frozen and living inside *target*. The
        detached cmd waits on ``ping`` rather than ``timeout``, because
        ``timeout`` needs a console input handle a detached process does not
        have - the exact reason Tlamatini's first self-delete quietly did
        nothing.
        """
        if not getattr(sys, "frozen", False):
            return
        exe = os.path.abspath(sys.executable)
        if not os.path.abspath(exe).lower().startswith(os.path.abspath(target).lower()):
            return
        folder = os.path.abspath(target)
        # Somewhere OUTSIDE the folder being deleted. A process whose current
        # directory is inside a folder holds that folder open, and `rmdir` then
        # fails no matter how long you wait - so neither this uninstaller (see
        # _run_uninstall) nor the helper may stand in it.
        neutral = os.environ.get("SystemRoot") or "C:\\"
        # The helper leaves a trace. A detached process that fails after we have
        # exited is otherwise completely silent - which is how a self-delete
        # that deleted NOTHING was mistaken for a locking race twice over.
        trace = os.path.join(tempfile.gettempdir(), "lumen-self-delete.log")
        # A SCRIPT FILE, not a one-liner. In a single `cmd /c` command line,
        # `if exist X (...) & tail` binds the tail to the IF: when the condition
        # is FALSE cmd silently discards everything after it, to the end of the
        # line, and exits 0. The retry chain used exactly that shape, so the
        # moment the first `del` SUCCEEDED the following `if exist` went false
        # and took the rmdir down with it - the folder could only ever be
        # removed on the path where the delete had already failed. In a batch
        # file every statement owns its own line and no `if` can eat the rest.
        script = os.path.join(tempfile.gettempdir(), "lumen-self-delete.cmd")
        body = f"""@echo off
setlocal
set "EXE={exe}"
set "FOLDER={folder}"
set "TRACE={trace}"

rem Escalating waits, because one fixed delay is only a guess about how long
rem PyInstaller's onefile bootloader needs to tear down its temp directory and
rem release the exe. `ping` and not `timeout`: timeout needs a console input
rem handle, which a detached process does not have.
ping 127.0.0.1 -n 3 >nul
del /f /q "%EXE%" >>"%TRACE%" 2>&1
if exist "%EXE%" (
    ping 127.0.0.1 -n 6 >nul
    del /f /q "%EXE%" >>"%TRACE%" 2>&1
)
if exist "%EXE%" (
    ping 127.0.0.1 -n 11 >nul
    del /f /q "%EXE%" >>"%TRACE%" 2>&1
)
if exist "%EXE%" (
    echo SELF-DELETE FAILED: "%EXE%" survived >>"%TRACE%"
) else (
    echo SELF-DELETE OK: "%EXE%" >>"%TRACE%"
)

rem Plain rmdir, NOT rmdir /s: it must succeed only if the folder came out
rem empty. Whatever the user asked us to keep is still in there, and /s would
rem take it along with the folder.
rmdir "%FOLDER%" >>"%TRACE%" 2>&1
if exist "%FOLDER%" (
    echo FOLDER REMAINS: "%FOLDER%" >>"%TRACE%"
) else (
    echo FOLDER REMOVED: "%FOLDER%" >>"%TRACE%"
)

rem Last act: remove this script. cmd reads a batch file line by line, so a
rem batch file may delete itself as its final statement.
del /f /q "%~f0" >nul 2>&1
"""
        try:
            with open(script, "w", encoding="ascii", errors="replace") as fh:
                fh.write(body)
            # ONE STRING, never a list. subprocess.list2cmdline() renders a list
            # into a single quoted argument and escapes every inner quote as \",
            # following the MSVC runtime's rules - which cmd.exe does not use.
            # cmd then receives mangled paths, matches nothing, and exits 0
            # having done nothing at all. That silent no-op fails identically
            # under every combination of creation flags and works under all of
            # them once the command line is passed through verbatim.
            subprocess.Popen(
                f'cmd /c "{script}"',
                cwd=neutral,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            print(f"[UNINSTALL] Scheduled self-deletion of {os.path.basename(exe)}")
            print(f"[UNINSTALL] Self-delete trace: {trace}")
        except Exception as exc:
            print(f"[UNINSTALL] could not schedule self-deletion: {exc}")

    # ─── Registry removal ────────────────────────────────────────────────
    @staticmethod
    def _delete_registry_tree(key_path: str) -> bool:
        """Recursively delete an HKCU key. A missing key counts as success."""
        if sys.platform != "win32":
            return True
        try:
            import winreg
        except Exception:
            return False

        def _delete(sub: str) -> bool:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0,
                                    winreg.KEY_ALL_ACCESS) as key:
                    while True:
                        try:
                            child = winreg.EnumKey(key, 0)
                        except OSError:
                            break
                        _delete(f"{sub}\\{child}")
            except FileNotFoundError:
                return True
            except OSError as exc:
                print(f"[UNINSTALL] could not open {sub}: {exc}")
                return False
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                print(f"[UNINSTALL] could not delete {sub}: {exc}")
                return False

        ok = _delete(key_path)
        print(f"[UNINSTALL] {'Removed' if ok else 'FAILED to remove'} HKCU\\{key_path}")
        return ok

    def _purge_associations_via_winreg(self) -> bool:
        """Python fallback for unregister_associations.ps1.

        Runs when PowerShell is unavailable or blocked by policy. It removes the
        same keys, minus the cosmetic reporting - because a dead association is
        worse than a noisy uninstall.
        """
        if sys.platform != "win32":
            return True
        try:
            import winreg
        except Exception:
            return False

        prog_ids = ["Lumen.EpubBook", "Lumen.PdfDocument", "Lumen.Book",
                    "LumenReader.EpubBook", "LumenReader.PdfDocument"]
        ok = True
        for prog_id in prog_ids:
            ok &= self._delete_registry_tree(rf"Software\Classes\{prog_id}")
        ok &= self._delete_registry_tree(rf"Software\Classes\Applications\{EXE_NAME}")
        ok &= self._delete_registry_tree(
            rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{EXE_NAME}")

        for ext in (".epub", ".pdf"):
            for base in (rf"Software\Classes\{ext}\OpenWithProgids",
                         rf"Software\Microsoft\Windows\CurrentVersion\Explorer"
                         rf"\FileExts\{ext}\OpenWithProgids"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base, 0,
                                        winreg.KEY_ALL_ACCESS) as key:
                        for prog_id in prog_ids:
                            try:
                                winreg.DeleteValue(key, prog_id)
                            except FileNotFoundError:
                                pass
                except FileNotFoundError:
                    pass
                except OSError:
                    ok = False
            # Release the default only when it is still ours.
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    rf"Software\Classes\{ext}", 0,
                                    winreg.KEY_ALL_ACCESS) as key:
                    current, _ = winreg.QueryValueEx(key, "")
                    if current in prog_ids:
                        winreg.DeleteValue(key, "")
            except (FileNotFoundError, OSError):
                pass

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\RegisteredApplications", 0,
                                winreg.KEY_ALL_ACCESS) as key:
                try:
                    winreg.DeleteValue(key, PRODUCT_NAME)
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, OSError):
            pass

        print("[UNINSTALL] Associations cleared via the winreg fallback.")
        return ok

    # ─── Total erasure ───────────────────────────────────────────────────
    def _protects_export(self, path: str) -> bool:
        """Is the user's freshly written export inside this tree?

        It can be: the destination folder is theirs to choose, and nothing stops
        them choosing ``%APPDATA%\\Lumen Reader``.  Deleting the export as part
        of erasing our traces would destroy the one file this whole wizard
        exists to hand them.
        """
        if not self._export_path:
            return False
        try:
            return os.path.commonpath(
                [os.path.abspath(path), os.path.abspath(self._export_path)]
            ) == os.path.abspath(path)
        except ValueError:      # different drives - no containment possible
            return False

    def _erase_all_traces(self) -> int:
        """Erase every remaining trace of Lumen: state, caches, registry.

        Returns the number of locations that actually held something, so the
        summary can report a real figure rather than a reassuring one.
        """
        erased = 0

        # ── State and cache directories ──────────────────────────────────
        roaming = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(roaming, APPDATA_DIR_NAME),
            os.path.join(local, APPDATA_DIR_NAME),
            os.path.join(local, FOLDER_NAME),
            os.path.join(local, "Temp", APPDATA_DIR_NAME),
        ]
        for directory in candidates:
            if not directory or not os.path.isdir(directory):
                continue
            if self._protects_export(directory):
                print(f"[UNINSTALL] Kept {directory} - it holds your export.")
                self._kept.append(f"{directory}  (holds your saved reading data)")
                continue
            if self._remove_tree(directory):
                erased += 1
                print(f"[UNINSTALL] Erased {directory}")

        if sys.platform != "win32":
            return erased

        # ── Registry ─────────────────────────────────────────────────────
        # The PowerShell unregisterer covers these too, but it is allowed to
        # fail (execution policy, missing pwsh) and this pass is not. Deleting
        # an absent key is a no-op, so running both is free.
        for key_path in (
            ARP_KEY,
            DISCOVERY_KEY,
            rf"Software\Classes\Applications\{EXE_NAME}",
            rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{EXE_NAME}",
        ):
            if self._delete_registry_tree(key_path):
                erased += 1

        self._purge_associations_via_winreg()
        erased += self._drop_vendor_key_if_empty()
        erased += self._purge_shell_history()
        return erased

    @staticmethod
    def _drop_vendor_key_if_empty() -> int:
        """Remove ``HKCU\\Software\\XAIHT`` - but only if Lumen was alone in it.

        The vendor key is shared ground.  Another XAIHT product living under it
        must survive Lumen's uninstall, so this deletes the parent only when
        removing Lumen left it with no subkeys and no values.
        """
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\XAIHT", 0,
                                winreg.KEY_ALL_ACCESS) as key:
                subkeys, values, _ = winreg.QueryInfoKey(key)
                if subkeys or values:
                    print("[UNINSTALL] HKCU\\Software\\XAIHT holds other products - kept.")
                    return 0
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\XAIHT")
            print("[UNINSTALL] Removed the now-empty HKCU\\Software\\XAIHT key.")
            return 1
        except FileNotFoundError:
            return 0
        except Exception:
            return 0

    @staticmethod
    def _purge_shell_history() -> int:
        """Drop the Explorer caches that name Lumen by path.

        Scoped deliberately to entries that *contain our executable name*.  The
        neighbouring ``RecentDocs\\.epub`` list is NOT touched: it records the
        user's own recently-opened books, exists with or without Lumen, and
        clearing it would delete a history that was never ours to delete.
        """
        try:
            import winreg
        except Exception:
            return 0

        removed = 0
        needle = EXE_NAME.lower()

        # MUICache stores the friendly name against the full exe path.
        mui = (r"Software\Classes\Local Settings\Software\Microsoft"
               r"\Windows\Shell\MuiCache")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, mui, 0,
                                winreg.KEY_ALL_ACCESS) as key:
                doomed = []
                index = 0
                while True:
                    try:
                        name, _value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    if needle in name.lower() or FOLDER_NAME.lower() in name.lower():
                        doomed.append(name)
                    index += 1
                for name in doomed:
                    try:
                        winreg.DeleteValue(key, name)
                        removed += 1
                    except OSError:
                        pass
        except FileNotFoundError:
            pass
        except OSError:
            pass

        # UserAssist counts every program the user launches, ROT13'd.
        base = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\UserAssist")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
                guides = []
                index = 0
                while True:
                    try:
                        guides.append(winreg.EnumKey(root, index))
                    except OSError:
                        break
                    index += 1
        except (FileNotFoundError, OSError):
            guides = []

        rot13 = str.maketrans(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm",
        )
        for guid in guides:
            path = rf"{base}\{guid}\Count"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_ALL_ACCESS) as key:
                    doomed = []
                    index = 0
                    while True:
                        try:
                            name, _value, _kind = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        if needle in name.translate(rot13).lower():
                            doomed.append(name)
                        index += 1
                    for name in doomed:
                        try:
                            winreg.DeleteValue(key, name)
                            removed += 1
                        except OSError:
                            pass
            except (FileNotFoundError, OSError):
                continue

        if removed:
            print(f"[UNINSTALL] Cleared {removed} Explorer cache entr(y/ies) naming Lumen.")
        return 1 if removed else 0

    # ─── Child processes ─────────────────────────────────────────────────
    @staticmethod
    def _clean_env() -> dict:
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            meipass = (getattr(sys, "_MEIPASS", "") or "").lower()
            exe_dir = os.path.dirname(sys.executable).lower()
            internal = os.path.join(exe_dir, "_internal").lower()
            blocked = {p for p in (meipass, exe_dir, internal) if p}
            paths = [
                p for p in env.get("PATH", "").split(os.pathsep)
                if p.lower() not in blocked
                and not any(p.lower().startswith(b + os.sep) for b in blocked)
            ]
            env["PATH"] = os.pathsep.join(paths)
        return env

    def _run_ps1(self, filename: str, target_dir: str) -> tuple[bool, str]:
        """Run a PowerShell script from the installation. Never raises."""
        script = os.path.join(target_dir, filename)
        if not os.path.isfile(script):
            # An older installation may predate this script. Not fatal; the
            # winreg fallback and the file sweep still finish the job.
            return False, f"{filename} not present in the installation"
        try:
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
                 "-NonInteractive", "-File", script],
                cwd=target_dir, env=self._clean_env(),
                capture_output=True, text=True, timeout=180, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return False, f"{filename} timed out after 180s"
        except OSError as exc:
            return False, f"{filename} could not be started: {exc}"
        if result.stdout:
            print(f"[{filename}]\n{result.stdout}")
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "").strip() or \
                          f"exit code {result.returncode}"
        return True, ""

    # ─── Shell refresh (same non-destructive approach as the installer) ──
    @staticmethod
    def _refresh_shell() -> None:
        try:
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                icon_db = os.path.join(local, "IconCache.db")
                if os.path.exists(icon_db):
                    os.remove(icon_db)
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        except Exception:
            pass
        try:
            result = ctypes.c_long(0)
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,
                ctypes.byref(result))
        except Exception:
            pass

    # ─── Completion ──────────────────────────────────────────────────────
    def _build_summary(self, target: str, notes: list[str]) -> list[str]:
        lines = [f"Removed from:  {target}"]
        if self._kept:
            lines.append(f"Kept in place: {', '.join(self._kept)}")
        if self.skip_save_var.get():
            lines.append("Reading data:  erased, as you asked.")
        else:
            counts = self._export_counts
            lines.append(f"Reading data:  saved to {self._export_path}")
            lines.append(
                f"               {counts.get('positions', 0)} book positions, "
                f"{counts.get('marks', 0)} marks/notes/quotes/tags"
            )
        lines.append("Configuration: erased — registry, caches and all settings.")

        # Never let this screen imply more than was done. Windows keeps records
        # of every program that has ever run (the Application event log, the
        # prefetcher, Amcache) in stores that belong to the operating system,
        # need administrator rights, and hold entries for thousands of unrelated
        # programs. An uninstaller that rewrote them would be tampering with
        # system forensics to flatter itself, so this one says so instead.
        lines.append("Not touched:   Windows' own event log and prefetch records, "
                     "which are the operating system's, not Lumen's.")
        lines.extend(f"Note: {n}" for n in notes)
        return lines

    def _show_success(self, target: str) -> None:
        self._uninstalling = False
        self.step_label.config(text="✓  Removal complete", fg=SUCCESS)
        messagebox.showinfo(
            "Lumen has been removed",
            f"{PRODUCT_NAME} was uninstalled.\n\n" + "\n".join(self._summary)
            + "\n\nYour books were not touched.",
        )
        # Scheduled last: as soon as this returns, the process exits and the
        # detached cmd can finally delete the exe we are running from.
        self._schedule_self_delete(target)
        self.root.destroy()

    def _show_error(self, detail: str) -> None:
        self._uninstalling = False
        for widget in (self.uninstall_btn, self.browse_btn, self.path_entry,
                       self.skip_check, self.save_entry):
            widget.config(state="normal")
        self._sync_save_row()
        self.step_label.config(text="✗  Removal failed", fg=ERROR)
        where = f"\n\nA full log was written to:\n{LOG_PATH}" if LOG_PATH else ""
        messagebox.showerror(
            "Uninstall error",
            f"Something went wrong while removing {PRODUCT_NAME}:\n\n{detail}"
            + where,
        )


# ─── Silent mode ─────────────────────────────────────────────────────────────
# install.py writes a QuietUninstallString of `"Uninstaller.exe" /S` into the
# Add/Remove Programs entry. Windows, winget and enterprise deployment tools
# take that value at its word and expect no UI. Advertising a capability we do
# not have would be a lie told by the registry, so /S is implemented for real:
# the same pipeline, no Tk, defaults chosen conservatively.

def run_silent(argv: list[str]) -> int:
    """Remove Lumen without any UI. Returns a process exit code.

    Conservative by construction: it removes only what it can positively
    identify as a Lumen installation, and it always EXPORTS the user's reading
    data before erasing anything, because an unattended run must not destroy
    something a person would have been asked about. ``/SAVETO=<dir>`` chooses
    where; ``/NOSAVE`` opts out and is the only way to lose reading history.
    """
    print(f"{PRODUCT_NAME} silent uninstall  (argv={argv[1:]})")

    target, source = LumenUninstaller._detect_install_path()
    for arg in argv[1:]:
        if arg.upper().startswith("/D="):
            target, source = arg[3:], "the /D= argument"
    if not target or not os.path.isdir(target):
        print(f"ERROR: no installation found ({source}). Nothing to do.")
        return 2

    target = os.path.abspath(target)
    looks_right = (
        os.path.isfile(os.path.join(target, EXE_NAME))
        or os.path.isfile(os.path.join(target, MANIFEST_NAME))
        or os.path.isfile(os.path.join(target, "unregister_associations.ps1"))
    )
    if not looks_right:
        print(f"REFUSING: {target} does not look like a {PRODUCT_NAME} "
              f"installation. Nothing was deleted.")
        return 3

    skip_save = any(a.upper() in ("/NOSAVE", "--no-save") for a in argv[1:])
    save_to = LumenUninstaller._default_save_dir()
    for arg in argv[1:]:
        if arg.upper().startswith("/SAVETO="):
            save_to = arg[8:]
    print(f"Target       : {target}  (found via {source})")
    print(f"Reading data : {'ERASE (/NOSAVE)' if skip_save else f'save to {save_to}'}")
    print("Everything else (configuration, index, caches, registry): ERASE")

    # A tiny stand-in for the wizard: the pipeline is shared, only the progress
    # reporting differs. Building a real Tk root in a silent run would flash a
    # window, which is precisely what /S promises not to do.
    class _Headless(LumenUninstaller):
        def __init__(self) -> None:                      # noqa: D107
            self.version = resolve_version()
            self.skip_save_var = _ConstantFlag(skip_save)
            self.save_path = _ConstantFlag(save_to)
            self.install_path = _ConstantFlag(target)
            self._uninstalling = True
            self._summary = []
            self._kept = []
            self._export_path = ""
            self._export_counts = {}
            self._progress_value = 0.0
            self.STEPS = LumenUninstaller.STEPS

        # Progress reporting collapses to log lines.
        def _set_progress(self, value, status=None):
            if status:
                print(f"  [{int(value * 100):3d}%] {status}")

        def _mark_step(self, idx, success=True, note=""):
            mark = "OK  " if success else "WARN"
            print(f"  [{mark}] {self.STEPS[idx][0]}{('  ' + note) if note else ''}")

        def _activate_step(self, idx):
            pass

    app = _Headless()
    failure: list[str] = []

    # _run_uninstall marshals its two outcomes through root.after(); in headless
    # mode we intercept them directly.
    def _ok(_target):
        print("\n".join(app._summary))
        print("RESULT: uninstalled.")

    def _fail(detail):
        failure.append(detail)
        print(f"RESULT: FAILED - {detail}")

    class _DirectDispatch:
        @staticmethod
        def after(_delay, func, *args):
            func(*args)

    app.root = _DirectDispatch()          # type: ignore[assignment]
    app._show_success = _ok               # type: ignore[method-assign]
    app._show_error = _fail               # type: ignore[method-assign]
    app._run_uninstall(target)

    if failure:
        return 1
    LumenUninstaller._schedule_self_delete(app, target)
    return 0


class _ConstantFlag:
    """A tk variable stand-in for the headless run.

    Holds the value verbatim - it now backs a StringVar (the save folder, the
    install path) as well as the original BooleanVar, and coercing a path
    through ``bool()`` would turn it into ``True``.
    """

    def __init__(self, value: Any):
        self._value = value

    def get(self) -> Any:
        return self._value


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> int:
    global LOG_PATH
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
    LOG_PATH = redirect_output_to_log()

    if any(a.upper() in ("/S", "/SILENT", "--silent") for a in sys.argv[1:]):
        return run_silent(sys.argv)

    root = tk.Tk()
    root.withdraw()
    app = LumenUninstaller(root)
    root.update_idletasks()
    root.deiconify()
    try:
        icon = os.path.join(LumenUninstaller._base_dir(), ICON_NAME)
        if os.path.isfile(icon):
            root.iconbitmap(icon)
    except Exception:
        pass
    root.protocol("WM_DELETE_WINDOW", app._on_cancel)
    root.mainloop()
    # Give the detached self-delete a moment to see this process go.
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
