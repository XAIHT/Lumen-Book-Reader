# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""install.py - the Lumen Book Reader installation wizard.

Frozen by ``build_installer.py`` into ``Installer.exe`` and shipped beside
``pkg.zip`` and ``Uninstaller.exe``.  It is deliberately SELF-CONTAINED: it
imports nothing from ``lumen_reader`` (the frozen installer has no access to
the package) and nothing outside the standard library, so the wizard can never
fail because the thing it is installing is not installed yet.

What the wizard gives the user control over
-------------------------------------------
  * Where Lumen goes.
  * Where their library lives - which becomes every shortcut's working
    directory, because Lumen builds its shelf from the current directory and
    writes reading marks there.
  * WHICH FILE TYPES OPEN IN LUMEN - a checkbox per format (.epub, .pdf), plus
    a separate, deliberately-unticked "make Lumen the default" switch.  Adding
    Lumen to the Open-with menu and seizing .pdf from Acrobat are two very
    different acts, and the dialog treats them that way.
  * Which shortcuts get created.

Everything is written under HKCU, so the install needs no administrator rights
and touches no other user of the machine.

Every registry key, shortcut and file this wizard creates is removed by
``uninstall.py``.  That mirror is the whole design: nothing is written that
does not have a matching removal.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import zipfile
from ctypes import wintypes
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

# ─── Product identity ────────────────────────────────────────────────────────
PRODUCT_NAME = "Lumen Book Reader"
FOLDER_NAME = "Lumen Book Reader"        # created inside the chosen parent
EXE_NAME = "Lumen.exe"
ICON_NAME = "lumen.ico"
PUBLISHER = "XAIHT"
AUTHOR = "Angela López Mendoza"
ABOUT_URL = "https://github.com/XAIHT/Lumen-Book-Reader"
ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LumenBookReader"
DISCOVERY_KEY = r"Software\XAIHT\Lumen Book Reader"
MANIFEST_NAME = "LumenInstall.json"

# The file types Lumen can claim. Mirrored EXACTLY by the ProgID table in
# register_associations.ps1 / unregister_associations.ps1 - adding a format
# means editing all three.
ASSOCIATIONS = (
    (".epub", "EPUB book", "Lumen.EpubBook", True),
    (".pdf", "PDF document", "Lumen.PdfDocument", True),
)


# ─── Version resolution ──────────────────────────────────────────────────────
# Frozen: read the running .exe's Win32 ProductVersion, so the badge in this
# header and Explorer's Properties ▸ Details sheet are the same string by
# construction. Source: fall back to git tags. Empty is a valid answer; the UI
# degrades gracefully.

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
        # 040904B0 (en-US, Unicode) is what our VERSIONINFO writes; the others
        # are safety nets for a differently-localised build.
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
    """Most recent reachable ``v*`` tag, stripped of the leading ``v``."""
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
    """Frozen -> the exe's own VERSIONINFO; source -> git."""
    if getattr(sys, "frozen", False):
        version = _read_exe_product_version(sys.executable)
        if version:
            return version
    return _derive_version_from_git()


# ─── DLL-locking prevention (PyInstaller --onedir) ───────────────────────────
# The frozen installer keeps vcruntime140*.dll inside _internal/. Any child
# process that inherits the DLL search path can pin those files open long after
# the installer exits, which then blocks the NEXT install from replacing them.

def _reset_dll_search_path() -> None:
    """Drop ``_internal`` from the loader's search order for our children."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass


def _free_vc_runtime_handles() -> None:
    """Release our own references to the bundled VC runtime DLLs."""
    if sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        get_handle = k32.GetModuleHandleW
        get_handle.restype = ctypes.c_void_p
        for dll_name in ("vcruntime140.dll", "vcruntime140_1.dll"):
            handle = get_handle(dll_name)
            if handle:
                k32.FreeLibrary(handle)
    except Exception:
        pass


# ─── Diagnostics ─────────────────────────────────────────────────────────────
# A frozen --windowed wizard has no stdout: CPython's print() silently returns
# when sys.stdout is None, so every diagnostic in this file would evaporate at
# exactly the moment someone needs it - a failed install on a machine we cannot
# see. Point stdout at a log file instead, and name that file in the failure
# dialog so the user has something concrete to send.

LOG_NAME = "install.log"


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
    handle.write(f"\n===== {stamp}  {PRODUCT_NAME} install =====\n")
    sys.stdout = handle
    sys.stderr = handle
    return path


LOG_PATH = ""


# ─── Colour palette ──────────────────────────────────────────────────────────
# Lumen's own warm-parchment-on-ink identity, not Tlamatini's cyan.
BG_DARK       = "#0d1117"
BG_PANEL      = "#141b24"
BG_CARD       = "#111820"
BG_INPUT      = "#1b2530"
FG_PRIMARY    = "#e8e3d8"
FG_SECONDARY  = "#8d9aa8"
FG_DIM        = "#5a6673"
ACCENT        = "#63d1ad"     # Lumen green
ACCENT_HOVER  = "#7ee9c4"
ACCENT_DEEP   = "#3fa588"
GOLD          = "#f2bd4d"
SUCCESS       = "#63d1ad"
WARNING       = "#f2bd4d"
ERROR         = "#ff7c52"
BTN_BG        = "#1e3a33"
BTN_HOVER     = "#2b5548"
BTN_CANCEL_BG = "#241c22"
BTN_CANCEL_HV = "#37282f"
PROGRESS_BG   = "#1b2530"
BORDER_COLOR  = "#26303c"
FONT_FAMILY   = "Segoe UI"


class LumenInstaller:
    """The dark-themed installation wizard for Lumen Book Reader."""

    # ── Weighted installation steps. The weights sum to 1.0 so the progress
    #    bar tracks real work rather than step count: extraction is ~55% of a
    #    Lumen install and the bar should say so.
    STEPS = [
        ("Preparing the installation directory…", 0.04),
        ("Extracting Lumen…",                     0.55),
        ("Writing the install manifest…",         0.03),
        ("Installing the uninstaller…",           0.05),
        ("Registering with Windows…",             0.08),
        ("Creating shortcuts…",                   0.08),
        ("Registering file associations…",        0.11),
        ("Refreshing the Windows shell…",         0.06),
    ]

    # If preserved_user_state.json cannot be read, this is what we protect.
    # FAIL-SAFE, never fail-open: keeping a file by mistake is recoverable,
    # deleting the user's settings because a JSON file did not parse is not.
    _PRESERVE_FALLBACK = (
        MANIFEST_NAME, "Uninstaller.exe", "logs", "library",
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        self.version = resolve_version()
        title = f"{PRODUCT_NAME} Installer"
        if self.version:
            title += f"  v{self.version}"
        self.root.title(title)
        self.root.configure(bg=BG_DARK)
        self.root.resizable(False, False)

        w, h = 720, 790
        sx = (self.root.winfo_screenwidth() - w) // 2
        sy = max((self.root.winfo_screenheight() - h) // 2 - 20, 0)
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        self.zip_path = self._find_beside("pkg.zip")
        if not self.zip_path:
            messagebox.showerror(
                "Package not found",
                "pkg.zip was not found next to the installer.\n\n"
                "Keep Installer.exe, pkg.zip and Uninstaller.exe together in the "
                "same folder, then run the installer again.",
            )
            self.root.destroy()
            return

        self.install_path = tk.StringVar(value=self._default_install_parent())
        self.library_path = tk.StringVar(value=self._default_library())
        self.assoc_vars: dict[str, tk.BooleanVar] = {
            ext: tk.BooleanVar(value=default)
            for ext, _label, _progid, default in ASSOCIATIONS
        }
        self.set_default_var = tk.BooleanVar(value=False)
        self.desktop_var = tk.BooleanVar(value=True)
        self.startmenu_var = tk.BooleanVar(value=True)

        self._progress_value = 0.0
        self._installing = False
        self._summary: list[str] = []

        self._build_ui()

    # ─── Resource helpers ────────────────────────────────────────────────
    @staticmethod
    def _base_dir() -> str:
        """The folder holding Installer.exe (frozen) or install.py (source)."""
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.abspath(os.path.dirname(__file__))

    @classmethod
    def _find_beside(cls, filename: str) -> str | None:
        path = os.path.join(cls._base_dir(), filename)
        return path if os.path.isfile(path) else None

    @staticmethod
    def _default_install_parent() -> str:
        """``%LOCALAPPDATA%\\Programs`` - the per-user home Windows expects."""
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidate = os.path.join(local, "Programs")
            if os.path.isdir(candidate) or os.path.isdir(local):
                return candidate
        return os.path.expanduser("~")

    @staticmethod
    def _known_folder(folder_guid: str) -> str:
        """Resolve a Windows Known Folder by GUID, honouring redirection.

        ``%USERPROFILE%\\Documents`` is a guess, and on a machine with OneDrive
        Known Folder Move - or any roaming/redirected profile - it is the WRONG
        guess: the real Documents folder is somewhere else entirely, and the
        one on disk is a stale decoy. ``SHGetKnownFolderPath`` asks Windows
        where the folder actually is.
        """
        if sys.platform != "win32":
            return ""
        try:
            from ctypes import windll, wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD),
                            ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            parsed = UUID(folder_guid)
            fields = parsed.fields
            guid = GUID(fields[0], fields[1], fields[2],
                        (ctypes.c_ubyte * 8)(fields[3], fields[4],
                                             *parsed.bytes[10:]))
            out = ctypes.c_wchar_p()
            # 0 = KF_FLAG_DEFAULT, NULL token = the current user.
            if windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
                return ""
            path = out.value or ""
            windll.ole32.CoTaskMemFree(out)
            return path
        except Exception:
            return ""

    @classmethod
    def _default_library(cls) -> str:
        """The real Documents folder, wherever Windows says it lives."""
        documents = cls._known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7")
        if documents and os.path.isdir(documents):
            return documents
        home = os.path.expanduser("~")
        for name in ("Documents", "Documentos"):
            candidate = os.path.join(home, name)
            if os.path.isdir(candidate):
                return candidate
        return home

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
        self._build_association_section(inner)
        self._rule(inner)
        self._build_shortcut_section(inner)
        self._build_progress_section(inner)
        self._build_buttons(inner)

    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg=BG_CARD, height=96)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ACCENT, height=3).pack(fill="x")

        row = tk.Frame(hdr, bg=BG_CARD)
        row.pack(fill="both", expand=True)

        tk.Label(row, text="◆", font=(FONT_FAMILY, 26), bg=BG_CARD, fg=ACCENT
                 ).pack(side="left", padx=(22, 12), pady=(10, 0))

        titles = tk.Frame(row, bg=BG_CARD)
        titles.pack(side="left", pady=(16, 0))
        tk.Label(titles, text=PRODUCT_NAME, font=(FONT_FAMILY, 19, "bold"),
                 bg=BG_CARD, fg=FG_PRIMARY).pack(anchor="w")
        tk.Label(titles, text="Installation wizard", font=(FONT_FAMILY, 10),
                 bg=BG_CARD, fg=FG_SECONDARY).pack(anchor="w")

        self._build_version_badge(row)

    def _build_version_badge(self, parent: tk.Frame) -> None:
        """The pill in the header. Rendered only when a version resolved."""
        if not self.version:
            return
        outer = tk.Frame(parent, bg=ACCENT, highlightthickness=0, bd=0)
        outer.pack(side="right", padx=(0, 24), pady=(22, 0))
        badge = tk.Frame(outer, bg=BG_INPUT)
        badge.pack(padx=1, pady=1)          # the 1-px reveal IS the border
        tk.Label(badge, text="VERSION", font=(FONT_FAMILY, 7, "bold"),
                 bg=BG_INPUT, fg=FG_SECONDARY).pack(padx=15, pady=(5, 0))
        tk.Label(badge, text=f"v{self.version}", font=(FONT_FAMILY, 12, "bold"),
                 bg=BG_INPUT, fg=ACCENT).pack(padx=15, pady=(0, 5))

    def _rule(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", pady=9)

    def _section_label(self, parent: tk.Frame, text: str, hint: str = "") -> None:
        tk.Label(parent, text=text, font=(FONT_FAMILY, 9, "bold"),
                 bg=BG_PANEL, fg=FG_SECONDARY).pack(anchor="w")
        if hint:
            tk.Label(parent, text=hint, font=(FONT_FAMILY, 8),
                     bg=BG_PANEL, fg=FG_DIM, justify="left").pack(anchor="w", pady=(0, 6))

    def _path_row(self, parent: tk.Frame, variable: tk.StringVar,
                  browse_title: str) -> tuple[tk.Entry, tk.Button]:
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", pady=(0, 4))
        entry = tk.Entry(
            row, textvariable=variable, font=(FONT_FAMILY, 10),
            bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=ACCENT,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=BORDER_COLOR, highlightcolor=ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        button = self._make_button(
            row, "Browse",
            lambda: self._browse_into(variable, browse_title),
            width=10, small=True,
        )
        button.pack(side="right")
        return entry, button

    def _build_path_section(self, inner: tk.Frame) -> None:
        self._section_label(
            inner, "INSTALLATION FOLDER",
            f'A "{FOLDER_NAME}" folder is created inside the folder you choose.',
        )
        self.path_entry, self.browse_btn = self._path_row(
            inner, self.install_path, f"Choose a parent folder for {FOLDER_NAME}"
        )
        self.target_label = tk.Label(inner, text="", font=(FONT_FAMILY, 8),
                                     bg=BG_PANEL, fg=ACCENT, anchor="w")
        self.target_label.pack(fill="x", pady=(0, 8))
        self.install_path.trace_add("write", self._on_path_change)

        self._section_label(
            inner, "YOUR LIBRARY FOLDER  ·  THE WHOLE DATALAKE",
            "Every EPUB and PDF in this folder AND its sub-folders becomes your\n"
            "shelf. Lumen opens here and keeps your reading marks beside your books.",
        )
        self.library_entry, self.library_btn = self._path_row(
            inner, self.library_path, "Choose the folder that holds your books"
        )
        self.library_count_label = tk.Label(
            inner, text="", font=(FONT_FAMILY, 8, "bold"),
            bg=BG_PANEL, fg=ACCENT, anchor="w",
        )
        self.library_count_label.pack(fill="x", pady=(0, 8))
        self.library_path.trace_add("write", self._on_library_change)
        self._library_count_token = 0
        self._on_library_change()
        self._on_path_change()

    def _build_association_section(self, inner: tk.Frame) -> None:
        self._section_label(
            inner, "OPEN THESE FILE TYPES WITH LUMEN",
            "Ticked types gain a “Read in Lumen” command and Lumen's icon.\n"
            "Your current default app is not changed unless you ask below.",
        )
        grid = tk.Frame(inner, bg=BG_PANEL)
        grid.pack(fill="x", pady=(0, 4))
        for column, (ext, label, _progid, _default) in enumerate(ASSOCIATIONS):
            cell = tk.Frame(grid, bg=BG_PANEL)
            cell.grid(row=0, column=column, sticky="w", padx=(0, 34))
            self._make_check(
                cell, f"{ext}   {label}", self.assoc_vars[ext], bold=True
            ).pack(anchor="w")

        self._make_check(
            inner,
            "Also make Lumen the default app for the types ticked above",
            self.set_default_var,
            accent=GOLD,
        ).pack(anchor="w", pady=(6, 0))
        # wraplength is not optional here. Without it Tk sizes the label to the
        # full width of the text and the card clips both ends, so the sentence
        # reads "...ndows may still ask you to confirm... Default ap". The value
        # is the card's inner width (720 window - 2x26 body - 2x22 card padding)
        # minus this label's own indent, so it wraps instead of overflowing.
        tk.Label(
            inner,
            text="Windows may still ask you to confirm the change the first time. "
                 "You can undo it any time in Settings ▸ Apps ▸ Default apps.",
            font=(FONT_FAMILY, 8), bg=BG_PANEL, fg=FG_DIM, justify="left",
            wraplength=560, anchor="w",
        ).pack(anchor="w", padx=(26, 0), fill="x")

    def _build_shortcut_section(self, inner: tk.Frame) -> None:
        self._section_label(inner, "SHORTCUTS")
        row = tk.Frame(inner, bg=BG_PANEL)
        row.pack(fill="x")
        self._make_check(row, "Desktop", self.desktop_var).pack(side="left", padx=(0, 34))
        self._make_check(row, "Start menu", self.startmenu_var).pack(side="left")

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
            "Lumen.Horizontal.TProgressbar",
            troughcolor=PROGRESS_BG, background=ACCENT,
            darkcolor=ACCENT_DEEP, lightcolor=ACCENT_HOVER,
            bordercolor=BG_PANEL, thickness=16,
        )
        self.progress_bar = ttk.Progressbar(
            self.progress_frame, style="Lumen.Horizontal.TProgressbar",
            orient="horizontal", mode="determinate", maximum=100,
        )
        self.progress_bar.pack(fill="x", pady=(0, 2))

        self.pct_label = tk.Label(self.progress_frame, text="0 %",
                                  font=(FONT_FAMILY, 9, "bold"), bg=BG_PANEL,
                                  fg=ACCENT, anchor="e")
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
        self.cancel_btn = self._make_button(row, "Cancel", self._on_cancel, cancel=True)
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.install_btn = self._make_button(row, "◆  Install", self._start_install)
        self.install_btn.pack(side="right")

        # Enter anywhere in the window = Install, so the keyboard path and the
        # mouse path run the exact same validation.
        self.path_entry.bind("<Return>", self._on_enter_key)
        self.library_entry.bind("<Return>", self._on_enter_key)
        self.root.bind("<Return>", self._on_enter_key)
        self.root.bind("<Escape>", lambda _e: self._on_cancel())

    # ─── Widget factories ────────────────────────────────────────────────
    def _make_button(self, parent, text, command, width=14, small=False,
                     cancel=False) -> tk.Button:
        bg = BTN_CANCEL_BG if cancel else BTN_BG
        hover = BTN_CANCEL_HV if cancel else BTN_HOVER
        fg = FG_SECONDARY if cancel else FG_PRIMARY
        font = (FONT_FAMILY, 9) if small else (FONT_FAMILY, 10, "bold")
        btn = tk.Button(
            parent, text=text, command=command, font=font, bg=bg, fg=fg,
            activebackground=hover, activeforeground=FG_PRIMARY,
            relief="flat", bd=0, cursor="hand2", padx=14, pady=6, width=width,
        )
        btn.bind("<Enter>", lambda _e, b=btn, c=hover: b.config(bg=c))
        btn.bind("<Leave>", lambda _e, b=btn, c=bg: b.config(bg=c))
        return btn

    def _make_check(self, parent, text, variable, bold=False,
                    accent=ACCENT) -> tk.Checkbutton:
        return tk.Checkbutton(
            parent, text=text, variable=variable,
            font=(FONT_FAMILY, 9, "bold") if bold else (FONT_FAMILY, 9),
            bg=BG_PANEL, fg=FG_PRIMARY, activebackground=BG_PANEL,
            activeforeground=accent, selectcolor=BG_INPUT,
            highlightthickness=0, bd=0, cursor="hand2", anchor="w",
        )

    # ─── Path helpers ────────────────────────────────────────────────────
    def _on_path_change(self, *_args) -> None:
        raw = self.install_path.get().strip()
        self.target_label.config(
            text=f"➜  {os.path.join(raw, FOLDER_NAME)}" if raw else ""
        )

    def _on_library_change(self, *_args) -> None:
        """Count the datalake in the background so the wizard never stalls.

        A real library can hold tens of thousands of files across many folders,
        so the walk runs on a worker thread and reports through a token: a stale
        walk that finishes after the reader has already picked a different
        folder is discarded instead of overwriting the newer count.
        """
        raw = self.library_path.get().strip()
        self._library_count_token += 1
        token = self._library_count_token
        if not raw or not os.path.isdir(raw):
            self.library_count_label.config(
                text="" if not raw else "This folder does not exist yet.")
            return
        self.library_count_label.config(text="Counting your books…")
        threading.Thread(
            target=self._count_library, args=(raw, token), daemon=True
        ).start()

    def _count_library(self, root: str, token: int) -> None:
        skip = {".git", "__pycache__", "node_modules", ".venv", "venv",
                "$RECYCLE.BIN", "System Volume Information"}
        epub = pdf = 0
        total_bytes = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in skip and not entry.name.startswith("$"):
                            stack.append(entry.path)
                        continue
                    suffix = os.path.splitext(entry.name)[1].casefold()
                    if suffix == ".epub":
                        epub += 1
                    elif suffix == ".pdf":
                        pdf += 1
                    else:
                        continue
                    total_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        if token != self._library_count_token:
            return  # the reader moved on; this walk is stale
        size = float(total_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                readable = f"{size:,.1f} {unit}"
                break
            size /= 1024
        total = epub + pdf
        text = (f"✓  {total:,} books found   ·   {epub:,} EPUB   ·   {pdf:,} PDF"
                f"   ·   {readable}") if total else (
                "No EPUB or PDF books in this folder yet — that is fine, "
                "Lumen will pick them up later.")
        self.root.after(0, lambda: self.library_count_label.config(text=text))

    def _browse_into(self, variable: tk.StringVar, title: str) -> None:
        current = variable.get().strip()
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=current if os.path.isdir(current) else os.path.expanduser("~"),
        )
        if chosen:
            variable.set(os.path.normpath(chosen))

    def _validate(self) -> str | None:
        """Return the full install directory, or ``None`` when unusable."""
        raw = self.install_path.get().strip()
        if not raw:
            messagebox.showwarning("No folder selected",
                                   "Please choose an installation folder.")
            return None

        # A parent that does not exist yet is fine as long as we can make it -
        # ``%LOCALAPPDATA%\Programs`` is often absent on a fresh profile.
        if not os.path.isdir(raw):
            try:
                os.makedirs(raw, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Folder cannot be created",
                    f"This folder does not exist and could not be created:\n{raw}\n\n{exc}",
                )
                return None

        if not os.access(raw, os.W_OK):
            messagebox.showerror(
                "Folder is not writable",
                f"Windows will not let this installer write to:\n{raw}\n\n"
                "Choose a folder inside your user profile, such as\n"
                f"{self._default_install_parent()}",
            )
            return None

        library = self.library_path.get().strip()
        if library and not os.path.isdir(library):
            proceed = messagebox.askyesno(
                "Library folder not found",
                f"This library folder does not exist:\n{library}\n\n"
                "Install anyway? Shortcuts will start in the installation "
                "folder instead, and you can point Lumen at your books later.",
            )
            if not proceed:
                return None

        target = os.path.join(raw, FOLDER_NAME)
        if os.path.isdir(target) and os.listdir(target):
            choose_other = messagebox.askyesno(
                "Folder is not empty",
                f"{target}\n\nalready contains files.\n\n"
                "Yes  - choose a different folder\n"
                "No   - install over it (your settings and library are kept)",
            )
            if choose_other:
                self._browse_into(self.install_path,
                                  f"Choose a parent folder for {FOLDER_NAME}")
                return None
        return target

    def _on_enter_key(self, _event=None):
        # Returns "break" so the keypress does not also reach the window-level
        # binding and fire twice. _start_install is re-entry-guarded regardless.
        self._start_install()
        return "break"

    def _on_cancel(self) -> None:
        if self._installing:
            leave = messagebox.askyesno(
                "Installation in progress",
                "Lumen is still being installed.\n\n"
                "Quitting now leaves a partial installation behind. Quit anyway?",
            )
            if not leave:
                return
        self.root.destroy()

    # ─── Kick off ────────────────────────────────────────────────────────
    def _start_install(self) -> None:
        if self._installing:
            return
        target = self._validate()
        if target is None:
            return

        self._installing = True
        for widget in (self.install_btn, self.browse_btn, self.library_btn,
                       self.path_entry, self.library_entry):
            widget.config(state="disabled")
        self.progress_frame.pack(
            fill="x", before=self.progress_frame.master.winfo_children()[-1]
        )
        threading.Thread(target=self._run_install, args=(target,), daemon=True).start()

    # ─── Progress plumbing (always marshalled back to the Tk thread) ─────
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
            text=f"   ▸  {desc}", fg=ACCENT))

    # ─── Preserved user state ────────────────────────────────────────────
    def _preserved_names(self) -> set[str]:
        """Top-level names inside the install directory that must survive.

        Read from ``preserved_user_state.json``, looked for in the package we
        are about to extract (authoritative: it is THIS version's list), then
        beside the installer, then inside an existing installation. Only if all
        three fail do we use the built-in copy - and we still preserve.
        """
        def _parse(raw: bytes | str) -> set[str] | None:
            try:
                doc = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
            except Exception:
                return None
            names = doc.get("preserve_on_reinstall") or doc.get("preserve")
            if isinstance(names, list) and names:
                return {str(n).strip().lower() for n in names if str(n).strip()}
            return None

        try:
            with zipfile.ZipFile(self.zip_path) as zf:
                parsed = _parse(zf.read("preserved_user_state.json"))
                if parsed:
                    return parsed
        except Exception:
            pass

        base = self._base_dir()
        for candidate in (
            os.path.join(base, "preserved_user_state.json"),
            os.path.join(base, "_internal", "preserved_user_state.json"),
        ):
            try:
                with open(candidate, "r", encoding="utf-8-sig") as fh:
                    parsed = _parse(fh.read())
                if parsed:
                    return parsed
            except Exception:
                continue

        return {n.lower() for n in self._PRESERVE_FALLBACK}

    @staticmethod
    def _is_preserved_member(member: str, target: str, preserved: set[str]) -> bool:
        """True when this zip member belongs to EXISTING user state.

        Matching is on the TOP-LEVEL name, so ``logs/lumen.log`` is protected by
        the entry ``logs``. Nothing is skipped unless it is already on disk - a
        fresh install must still receive every seed file.
        """
        normalized = member.replace("\\", "/").strip("/")
        if not normalized:
            return False
        top = normalized.split("/")[0]
        if top.lower() not in preserved:
            return False
        return os.path.exists(os.path.join(target, top))

    # ─── The install pipeline (background thread) ────────────────────────
    def _run_install(self, target: str) -> None:
        try:
            cumulative = 0.0
            self._summary = []

            # ── 0. Directory ────────────────────────────────────────────
            idx = 0
            self._activate_step(idx)
            self._set_progress(0.0, "Creating the installation directory…")
            os.makedirs(target, exist_ok=True)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx)

            # ── 1. Extract, preserving existing user state ──────────────
            idx = 1
            self._activate_step(idx)
            weight = self.STEPS[idx][1]
            preserved = self._preserved_names()
            kept: list[str] = []
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                members = zf.namelist()
                total = max(len(members), 1)
                for i, member in enumerate(members, 1):
                    if self._is_preserved_member(member, target, preserved):
                        top = member.replace("\\", "/").split("/")[0]
                        if top not in kept:
                            kept.append(top)
                    else:
                        zf.extract(member, target)
                    if i % 25 == 0 or i == total:
                        self._set_progress(cumulative + weight * (i / total),
                                           f"Extracting Lumen…  ({i}/{total})")
            if kept:
                print(f"[INSTALL] Preserved existing user state: {', '.join(sorted(kept))}")
            cumulative += weight
            self._set_progress(cumulative)
            self._mark_step(idx, note=f"({len(kept)} kept)" if kept else "")

            # ── 2. The manifest every other component reads ─────────────
            idx = 2
            self._activate_step(idx)
            self._set_progress(cumulative, "Writing the install manifest…")
            selected = self._selected_extensions()
            self._write_manifest(target, selected)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx)

            # ── 3. Uninstaller ──────────────────────────────────────────
            idx = 3
            self._activate_step(idx)
            self._set_progress(cumulative, "Installing the uninstaller…")
            has_uninstaller = self._copy_uninstaller(target)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, success=has_uninstaller,
                            note="" if has_uninstaller else "(not bundled)")

            # ── 4. Windows registration (HKCU) ──────────────────────────
            idx = 4
            self._activate_step(idx)
            self._set_progress(cumulative, "Registering with Windows…")
            arp_ok = self._register_programs_entry(target, has_uninstaller)
            # Discovery is registered INDEPENDENTLY of the Add/Remove entry: a
            # working installation must be findable even when Uninstaller.exe
            # is missing or the ARP write raised.
            self._register_discovery(target, selected)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, success=arp_ok,
                            note="" if arp_ok else "(Add/Remove entry skipped)")

            # ── 5. Shortcuts ────────────────────────────────────────────
            idx = 5
            self._activate_step(idx)
            self._set_progress(cumulative, "Creating shortcuts…")
            shortcuts_ok, shortcut_detail = self._run_ps1("CreateShortcut.ps1", target)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, success=shortcuts_ok,
                            note="" if shortcuts_ok else "(see log)")
            if not shortcuts_ok:
                print(f"[INSTALL] CreateShortcut.ps1: {shortcut_detail}")

            # ── 6. File associations ────────────────────────────────────
            idx = 6
            self._activate_step(idx)
            self._set_progress(cumulative, "Registering file associations…")
            assoc_ok, assoc_detail = self._run_ps1("register_associations.ps1", target)
            cumulative += self.STEPS[idx][1]
            self._set_progress(cumulative)
            self._mark_step(idx, success=assoc_ok,
                            note="" if assoc_ok else "(see log)")
            if not assoc_ok:
                print(f"[INSTALL] register_associations.ps1: {assoc_detail}")

            # ── 7. Shell refresh ────────────────────────────────────────
            idx = 7
            self._activate_step(idx)
            self._set_progress(cumulative, "Refreshing the Windows shell…")
            self._refresh_shell()
            self._set_progress(1.0, "Installation complete")
            self._mark_step(idx)

            # Leave the record of this install WITH the installation. When
            # somebody asks in six months why .pdf opens in Lumen, the answer is
            # in the folder rather than in a support conversation.
            self._archive_log(target)

            self._summary = self._build_summary(target, selected, shortcuts_ok, assoc_ok)
            self.root.after(0, self._show_success, target)

        except Exception as exc:            # noqa: BLE001 - surfaced to the user
            self.root.after(0, self._show_error, str(exc))

    # ─── Selections ──────────────────────────────────────────────────────
    def _selected_extensions(self) -> list[str]:
        return [ext for ext, _l, _p, _d in ASSOCIATIONS if self.assoc_vars[ext].get()]

    def _write_manifest(self, target: str, selected: list[str]) -> None:
        """Write ``LumenInstall.json`` - the contract between all components.

        The PowerShell registrars, the shortcut creator, ``Uninstaller.exe`` and
        any future updater ALL read this one file. Nothing about an installation
        is inferred from a second place, which is precisely why an upgrade
        cannot half-apply.
        """
        library = self.library_path.get().strip()
        manifest = {
            "ProductName": PRODUCT_NAME,
            "Version": self.version,
            "Publisher": PUBLISHER,
            "Author": AUTHOR,
            "InstallDir": os.path.abspath(target).replace("/", "\\"),
            "LibraryDir": os.path.abspath(library).replace("/", "\\") if library else "",
            "Executable": EXE_NAME,
            "IconFile": ICON_NAME,
            "Associations": selected,
            "SetAsDefault": bool(self.set_default_var.get()),
            "Shortcuts": {
                "Desktop": bool(self.desktop_var.get()),
                "StartMenu": bool(self.startmenu_var.get()),
                "InstallDir": True,
            },
            "InstalledAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        path = os.path.join(target, MANIFEST_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        print(f"[INSTALL] Wrote {path}")

    # ─── Child-process helpers ───────────────────────────────────────────
    @staticmethod
    def _clean_env() -> dict:
        """A PATH with no PyInstaller directories, so children cannot pin our DLLs."""
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
        """Run a PowerShell script that was just extracted into *target_dir*.

        Returns ``(ok, detail)`` instead of raising. A shortcut that could not
        be created, or an association Group Policy refused, must NOT abandon an
        otherwise-good installation half-written - it is reported in the
        checklist and in the summary, and the install carries on.
        """
        script = os.path.join(target_dir, filename)
        if not os.path.isfile(script):
            return False, f"{filename} not found at {script}"
        try:
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
                 "-NonInteractive", "-File", script],
                cwd=target_dir, env=self._clean_env(),
                capture_output=True, text=True, timeout=180,
                close_fds=True,
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

    @staticmethod
    def _archive_log(target_dir: str) -> None:
        """Copy this run's log into the installation's ``logs`` folder."""
        if not LOG_PATH or not os.path.isfile(LOG_PATH):
            return
        try:
            logs_dir = os.path.join(target_dir, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            sys.stdout.flush()
            shutil.copy2(LOG_PATH, os.path.join(logs_dir, LOG_NAME))
        except OSError as exc:
            print(f"WARNING: could not archive the install log: {exc}")

    def _copy_uninstaller(self, target_dir: str) -> bool:
        """Copy ``Uninstaller.exe`` from beside the installer into the install dir."""
        src = self._find_beside("Uninstaller.exe")
        if not src:
            print("WARNING: Uninstaller.exe not found beside the installer - skipping.")
            return False
        try:
            shutil.copy2(src, os.path.join(target_dir, "Uninstaller.exe"))
            return True
        except OSError as exc:
            print(f"WARNING: could not copy Uninstaller.exe: {exc}")
            return False

    # ─── Registry: Add/Remove Programs ───────────────────────────────────
    def _register_programs_entry(self, target_dir: str, has_uninstaller: bool) -> bool:
        """Advertise Lumen in Settings ▸ Apps ▸ Installed apps.

        Per-user (HKCU), matching the non-elevated install. Best effort: a
        registry hiccup reports itself in the checklist but never fails the
        installation - the reader still works without an Add/Remove entry.
        """
        if sys.platform != "win32":
            return False
        try:
            import winreg

            install_dir = os.path.abspath(target_dir)
            uninstaller = os.path.join(install_dir, "Uninstaller.exe")
            if not has_uninstaller or not os.path.isfile(uninstaller):
                print("WARNING: no Uninstaller.exe - skipping the Installed-apps entry.")
                return False

            exe = os.path.join(install_dir, EXE_NAME)
            icon = os.path.join(install_dir, ICON_NAME)
            display_icon = icon if os.path.isfile(icon) else (
                f"{exe},0" if os.path.isfile(exe) else uninstaller
            )
            quoted = f'"{uninstaller}"'

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ARP_KEY) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, PRODUCT_NAME)
                if self.version:
                    winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, self.version)
                    parts = self.version.split("+")[0].split("-")[0].split(".")
                    try:
                        winreg.SetValueEx(key, "VersionMajor", 0, winreg.REG_DWORD, int(parts[0]))
                        winreg.SetValueEx(key, "VersionMinor", 0, winreg.REG_DWORD, int(parts[1]))
                    except (IndexError, ValueError):
                        pass
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
                winreg.SetValueEx(key, "Comments", 0, winreg.REG_SZ,
                                  f"A focused desktop reading room for EPUB and PDF. "
                                  f"Created by {AUTHOR}.")
                winreg.SetValueEx(key, "Contact", 0, winreg.REG_SZ, AUTHOR)
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, display_icon)
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
                winreg.SetValueEx(key, "InstallDate", 0, winreg.REG_SZ,
                                  datetime.now().strftime("%Y%m%d"))
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, quoted)
                winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                                  f"{quoted} /S")
                winreg.SetValueEx(key, "URLInfoAbout", 0, winreg.REG_SZ, ABOUT_URL)
                winreg.SetValueEx(key, "HelpLink", 0, winreg.REG_SZ, ABOUT_URL)
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
                size_kb = self._estimated_size_kb(install_dir)
                if size_kb > 0:
                    winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
            print(f"Registered {PRODUCT_NAME} in Installed apps (HKCU\\{ARP_KEY})")
            return True
        except Exception as exc:            # noqa: BLE001 - never fatal
            print(f"WARNING: could not register the Installed-apps entry: {exc}")
            return False

    @staticmethod
    def _estimated_size_kb(install_dir: str) -> int:
        """Size in KB for the Add/Remove "Size" column. Capped, best-effort."""
        total = 0
        seen = 0
        try:
            for root, _dirs, files in os.walk(install_dir):
                for name in files:
                    seen += 1
                    if seen > 80000:
                        return total // 1024
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        except Exception:
            return 0
        return total // 1024

    def _register_discovery(self, target_dir: str, selected: list[str]) -> None:
        """Write ``HKCU\\Software\\XAIHT\\Lumen Book Reader``.

        A stable, machine-readable answer to "where is Lumen, which version is
        it, and what did it claim?" - for a future updater, for companion tools,
        and for the uninstaller when ``LumenInstall.json`` has been deleted.

        Every value is written on every install, empty string when unknown, so
        a stale value from a previous version can never survive.
        """
        if sys.platform != "win32":
            return
        try:
            import winreg

            install_dir = os.path.abspath(target_dir)
            exe = os.path.join(install_dir, EXE_NAME)
            icon = os.path.join(install_dir, ICON_NAME)
            library = self.library_path.get().strip()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, DISCOVERY_KEY) as key:
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
                winreg.SetValueEx(key, "Executable", 0, winreg.REG_SZ,
                                  exe if os.path.isfile(exe) else "")
                winreg.SetValueEx(key, "IconPath", 0, winreg.REG_SZ,
                                  icon if os.path.isfile(icon) else "")
                winreg.SetValueEx(key, "Version", 0, winreg.REG_SZ, self.version or "")
                winreg.SetValueEx(key, "LibraryDir", 0, winreg.REG_SZ,
                                  os.path.abspath(library) if library else "")
                winreg.SetValueEx(key, "RegisteredExtensions", 0, winreg.REG_SZ,
                                  ";".join(selected))
                winreg.SetValueEx(key, "IsDefaultHandler", 0, winreg.REG_DWORD,
                                  1 if self.set_default_var.get() else 0)
                winreg.SetValueEx(key, "InstalledAt", 0, winreg.REG_SZ,
                                  datetime.now().astimezone().isoformat(timespec="seconds"))
            print(f"Registered discovery key (HKCU\\{DISCOVERY_KEY})")
        except Exception as exc:            # noqa: BLE001 - never fatal
            print(f"WARNING: could not register the discovery key: {exc}")

    # ─── Shell refresh ───────────────────────────────────────────────────
    @staticmethod
    def _refresh_shell() -> None:
        """Make Explorer notice the new icons and associations - WITHOUT
        restarting it.

        Killing explorer.exe and hoping it comes back is how an installer
        leaves someone staring at a blank desktop. ``SHChangeNotify`` is the
        supported way to say "associations changed", and the broadcast makes
        every top-level window re-read its settings.
        """
        try:
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                icon_db = os.path.join(local, "IconCache.db")
                if os.path.exists(icon_db):
                    os.remove(icon_db)
                cache_dir = os.path.join(local, "Microsoft", "Windows", "Explorer")
                if os.path.isdir(cache_dir):
                    for name in os.listdir(cache_dir):
                        if name.startswith("iconcache"):
                            try:
                                os.remove(os.path.join(cache_dir, name))
                            except OSError:
                                pass
        except Exception:
            pass

        try:
            SHCNE_ASSOCCHANGED = 0x08000000
            SHCNF_IDLIST = 0x0000
            ctypes.windll.shell32.SHChangeNotify(
                SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        except Exception:
            pass

        try:
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.c_long(0)
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))
        except Exception:
            pass

    # ─── Completion ──────────────────────────────────────────────────────
    def _build_summary(self, target: str, selected: list[str],
                       shortcuts_ok: bool, assoc_ok: bool) -> list[str]:
        lines = [f"Location:  {target}"]
        library = self.library_path.get().strip()
        if library:
            lines.append(f"Library:   {library}")
        if selected and assoc_ok:
            verb = "open in Lumen by default" if self.set_default_var.get() \
                else "can now be opened with Lumen"
            lines.append(f"File types: {', '.join(selected)} {verb}.")
        elif selected and not assoc_ok:
            lines.append(f"File types: {', '.join(selected)} could NOT be registered "
                         f"(see the installer log).")
        else:
            lines.append("File types: none registered - use right-click ▸ Open with.")
        wanted = [n for n, v in (("Desktop", self.desktop_var),
                                 ("Start menu", self.startmenu_var)) if v.get()]
        if wanted:
            lines.append(("Shortcuts: " + ", ".join(wanted)) +
                         ("" if shortcuts_ok else "  (could not be created)"))
        return lines

    def _show_success(self, target: str) -> None:
        self._installing = False
        self.step_label.config(text="✓  Installation complete", fg=SUCCESS)
        body = "\n".join(self._summary)
        messagebox.showinfo(
            "Installation complete",
            f"{PRODUCT_NAME} was installed successfully.\n\n{body}\n\n"
            "You can remove it any time from Settings ▸ Apps ▸ Installed apps, "
            "or by running Uninstaller.exe in the installation folder.",
        )
        self.root.destroy()

    def _show_error(self, detail: str) -> None:
        self._installing = False
        for widget in (self.install_btn, self.browse_btn, self.library_btn,
                       self.path_entry, self.library_entry):
            widget.config(state="normal")
        self.step_label.config(text="✗  Installation failed", fg=ERROR)
        where = f"\n\nA full log was written to:\n{LOG_PATH}" if LOG_PATH else ""
        messagebox.showerror(
            "Installation error",
            f"Something went wrong while installing {PRODUCT_NAME}:\n\n{detail}"
            + where,
        )


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> int:
    global LOG_PATH
    _reset_dll_search_path()
    LOG_PATH = redirect_output_to_log()
    try:
        import pyi_splash            # type: ignore[import-not-found]
        pyi_splash.update_text("Loading the Lumen installer…")
        pyi_splash.close()
    except Exception:
        pass

    root = tk.Tk()
    root.withdraw()                  # keep the window hidden while we build it
    app = LumenInstaller(root)
    if not getattr(app, "zip_path", None):
        return 1
    root.update_idletasks()
    root.deiconify()
    try:
        icon = os.path.join(LumenInstaller._base_dir(), ICON_NAME)
        if os.path.isfile(icon):
            root.iconbitmap(icon)
    except Exception:
        pass
    root.protocol("WM_DELETE_WINDOW", app._on_cancel)
    root.mainloop()
    _free_vc_runtime_handles()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
