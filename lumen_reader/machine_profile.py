# ═══════════════════════════════════════════════════════════════════
#   ✦  L U M E N   B O O K   R E A D E R  ✦
#
#   Created by  Angela López Mendoza   ·   @angelahack1
#   Developer · Architect · Creator of Lumen
# ═══════════════════════════════════════════════════════════════════
"""What machine is this, really — and what may Lumen ask of it.

Lumen's sweep was tuned on a workstation: a fleet of HIGH-priority extractor
processes, one per logical processor, and walker threads at twice the core
count.  On that machine those numbers are right.  On a four-core laptop with a
7200 rpm disk they are actively hostile, and in three separate ways:

* **The CPU.**  Four HIGH-priority processes on four cores is the whole machine
  above the reader's own Qt thread, which runs at Normal.  The window stops
  repainting.  The user does not conclude "the sweep is busy", they conclude
  Lumen has hung.
* **The disk.**  A 7200 rpm spindle serves roughly 100 random IOPS and has one
  head.  Four extractors plus eight ``scandir`` threads seeking against it do
  not go four times faster - they go *slower* than one worker would, because
  every concurrent stream drags the head somewhere else.  Sequential 150 MB/s
  collapses to well under 2 MB/s under that access pattern.
* **Memory.**  Queue depths and batch sizes here are all multiples of the core
  count, and every in-flight book carries up to ``text_budget`` characters.
  Sized for 64 GB, they are a swap storm on 8 GB.

None of that is a GPU problem, and none of it is fixed by owning better
hardware - it is fixed by asking the machine what it is before deciding what to
run on it.  That is all this module does.  It answers four questions - how many
processors, how much memory, what kind of disk, is it removable - and every one
of them degrades to a safe "I could not tell" rather than raising, because a
hardware probe must never be able to stop a book from opening.

The tuning that consumes these answers lives in :class:`~lumen_reader.turbo_scan.ScanConfig`,
where every knob keeps its explicit override: this module decides only what
``auto`` means on *this* machine.  A user who asks for eight HIGH-priority
processes on a laptop still gets exactly that.

Detection notes
---------------
Rotational media is read with ``IOCTL_STORAGE_QUERY_PROPERTY`` against a volume
handle opened with **zero** access rights, which is the one form of that call
an ordinary user may make - ``GENERIC_READ`` on ``\\\\.\\C:`` needs
Administrator, and a probe that needs elevation is a probe that always returns
"unknown" for the people this module exists to serve.  The same handle also
yields the bus type, so NVMe, SATA SSD and spinning disk are told apart in one
call with no subprocess and no WMI.

This module must never import Qt: it is read by the worker processes.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":          # ctypes.wintypes does not exist elsewhere
    import ctypes
    from ctypes import wintypes
else:                                 # pragma: no cover - exercised off Windows
    import ctypes


# ────────────────────────────── what we can answer ──────────────────────────

#: How a volume behaves, which is the only thing the tuner actually cares about.
#: ``hdd`` is the one that changes decisions; the rest differ only in how much
#: concurrency they enjoy.
STORAGE_NVME = "nvme"
STORAGE_SSD = "ssd"
STORAGE_HDD = "hdd"
STORAGE_REMOVABLE = "removable"
STORAGE_NETWORK = "network"
STORAGE_UNKNOWN = "unknown"

STORAGE_LABELS: dict[str, str] = {
    STORAGE_NVME: "NVMe solid-state",
    STORAGE_SSD: "SATA solid-state",
    STORAGE_HDD: "Mechanical disk (seek penalty)",
    STORAGE_REMOVABLE: "Removable media",
    STORAGE_NETWORK: "Network share",
    STORAGE_UNKNOWN: "Unknown storage",
}

#: Volumes that punish concurrent seeks.  A mechanical disk has one head; a card
#: reader has one slow controller.  Both want a queue depth near one.
SEEK_BOUND = frozenset({STORAGE_HDD, STORAGE_REMOVABLE})

#: What the machine gets called in the settings window.  Purely descriptive -
#: nothing branches on the tier name, every decision branches on the measured
#: number that produced it, so a machine that sits on a boundary is never tuned
#: by a label.
TIER_MODEST = "modest"
TIER_STANDARD = "standard"
TIER_WORKSTATION = "workstation"

#: Below this, per-book text buffers and queue depths are what runs the machine
#: out of memory, not the index.  8 GB is the line because a 4 GB machine has
#: perhaps 1.5 GB free with a browser open.
LOW_MEMORY_BYTES = 8 * 1024 ** 3
TIGHT_MEMORY_BYTES = 4 * 1024 ** 3

#: At or below this many logical processors the reader's own Qt thread loses if
#: the fleet takes every core, so the fleet gives one back.  Above it, one
#: process per core is what Angela specified and what the workstation wants.
SMALL_CPU_CEILING = 8


@dataclass(frozen=True, slots=True)
class MachineProfile:
    """What this machine is.  Every field has a safe answer when unknown."""

    logical_cpus: int = 4
    ram_bytes: int = 0                  # 0 = could not tell
    storage: str = STORAGE_UNKNOWN
    storage_detail: str = ""
    probed_path: str = ""

    # ── the three questions the tuner asks ─────────────────────────────────

    @property
    def seek_bound(self) -> bool:
        """True when concurrent reads cost more than they buy.

        Unknown storage is deliberately **not** seek-bound: assuming a spindle
        would throttle every machine whose disk we failed to identify, and the
        cost of being wrong the other way is a slower sweep, not a frozen one -
        the priority and core-count guards below carry that case.
        """
        return self.storage in SEEK_BOUND

    @property
    def low_memory(self) -> bool:
        return 0 < self.ram_bytes < LOW_MEMORY_BYTES

    @property
    def tight_memory(self) -> bool:
        return 0 < self.ram_bytes < TIGHT_MEMORY_BYTES

    @property
    def tier(self) -> str:
        if self.seek_bound or self.logical_cpus <= 4 or self.low_memory:
            return TIER_MODEST
        if (self.logical_cpus >= 16 and self.storage == STORAGE_NVME
                and self.ram_bytes >= 32 * 1024 ** 3):
            return TIER_WORKSTATION
        return TIER_STANDARD

    # ── how it reads in the settings window ────────────────────────────────

    def summary(self) -> str:
        memory = (f"{self.ram_bytes / 1024 ** 3:,.0f} GB RAM"
                  if self.ram_bytes else "memory unknown")
        return (f"{self.logical_cpus} logical processors  ·  {memory}  ·  "
                f"{STORAGE_LABELS.get(self.storage, self.storage)}")


# ─────────────────────────────── memory ─────────────────────────────────────


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def total_ram_bytes() -> int:
    """Installed physical memory, or 0 if the platform will not say.

    Deliberately ctypes and ``sysconf`` rather than ``psutil``: Lumen must size
    itself correctly on a machine where no optional dependency installed, and a
    probe that needs a package to answer "how much RAM" is a probe that fails on
    exactly the frozen build where sizing matters most.
    """
    if sys.platform == "win32":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            return 0
        return 0
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, ValueError, OSError):
        return 0


# ─────────────────────────────── storage ────────────────────────────────────

_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_StorageDeviceProperty = 0
_StorageDeviceSeekPenaltyProperty = 7
_PropertyStandardQuery = 0

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = -1

#: ``STORAGE_BUS_TYPE`` values we act on.  The rest are lumped into "unknown"
#: rather than guessed at.
_BUS_USB = 0x07
_BUS_SD = 0x0C
_BUS_MMC = 0x0D
_BUS_NVME = 0x11

_DRIVE_REMOTE = 4
_DRIVE_REMOVABLE = 2


class _StoragePropertyQuery(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_ulong),
        ("QueryType", ctypes.c_ulong),
        ("AdditionalParameters", ctypes.c_byte * 1),
    ]


class _DeviceSeekPenaltyDescriptor(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("IncursSeekPenalty", ctypes.c_byte),
    ]


class _StorageDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Size", ctypes.c_ulong),
        ("DeviceType", ctypes.c_byte),
        ("DeviceTypeModifier", ctypes.c_byte),
        ("RemovableMedia", ctypes.c_byte),
        ("CommandQueueing", ctypes.c_byte),
        ("VendorIdOffset", ctypes.c_ulong),
        ("ProductIdOffset", ctypes.c_ulong),
        ("ProductRevisionOffset", ctypes.c_ulong),
        ("SerialNumberOffset", ctypes.c_ulong),
        ("BusType", ctypes.c_ulong),
        ("RawPropertiesLength", ctypes.c_ulong),
    ]


def _volume_root(path: str | os.PathLike[str]) -> str:
    """The device path to interrogate for *path*, or "" if there is not one."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        resolved = Path(str(path))
    drive = os.path.splitdrive(str(resolved))[0]
    # A UNC path splits to \\\\server\\share, which has no volume handle to open.
    if not drive or drive.startswith("\\\\"):
        return ""
    return drive.rstrip(os.sep).rstrip("/")


def _query_volume(drive: str) -> tuple[int | None, int | None]:
    """``(bus type, incurs seek penalty)`` for a drive like ``C:``.

    Either element is ``None`` when the driver declines the query, which is
    normal - USB bridges and virtual disks frequently answer one and not the
    other, and the caller must cope with a partial answer rather than treat it
    as failure.
    """
    if sys.platform != "win32":
        return None, None
    handle = None
    try:
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        device_io = ctypes.windll.kernel32.DeviceIoControl
        device_io.restype = wintypes.BOOL
        device_io.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]

        # Zero desired access: enough to issue an informational IOCTL, and the
        # only form that does not require Administrator.
        handle = create_file(f"\\\\.\\{drive}", 0,
                             _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                             _OPEN_EXISTING, 0, None)
        if not handle or handle == wintypes.HANDLE(_INVALID_HANDLE_VALUE).value:
            return None, None

        returned = wintypes.DWORD(0)

        def ask(property_id: int, buffer: ctypes.Structure) -> bool:
            query = _StoragePropertyQuery()
            query.PropertyId = property_id
            query.QueryType = _PropertyStandardQuery
            return bool(device_io(
                handle, _IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query), ctypes.sizeof(query),
                ctypes.byref(buffer), ctypes.sizeof(buffer),
                ctypes.byref(returned), None,
            ))

        bus: int | None = None
        descriptor = _StorageDeviceDescriptor()
        if ask(_StorageDeviceProperty, descriptor):
            bus = int(descriptor.BusType)

        penalty: int | None = None
        seek = _DeviceSeekPenaltyDescriptor()
        if ask(_StorageDeviceSeekPenaltyProperty, seek):
            penalty = int(seek.IncursSeekPenalty)

        return bus, penalty
    except Exception:
        return None, None
    finally:
        if handle:
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass


def _linux_storage(path: str | os.PathLike[str]) -> tuple[str, str]:
    """Read ``/sys/block/<dev>/queue/rotational`` for *path*'s device."""
    try:
        device = os.stat(path).st_dev
        major, minor = os.major(device), os.minor(device)
        link = Path(f"/sys/dev/block/{major}:{minor}").resolve()
        for candidate in (link, *link.parents):
            flag = candidate / "queue" / "rotational"
            if flag.exists():
                rotational = flag.read_text().strip() == "1"
                return ((STORAGE_HDD, f"{candidate.name} reports rotational=1")
                        if rotational else
                        (STORAGE_SSD, f"{candidate.name} reports rotational=0"))
    except Exception:
        pass
    return STORAGE_UNKNOWN, "No rotational flag under /sys for this device."


def detect_storage(path: str | os.PathLike[str]) -> tuple[str, str]:
    """``(class, why)`` for the volume holding *path*.

    The reason string is returned alongside the class on purpose: the settings
    window shows it, and a machine tuned down to two workers must be able to say
    *which* measurement caused that.  "Lumen decided you are slow" is not an
    acceptable thing for a program to tell someone about their computer.
    """
    drive = _volume_root(path)
    if sys.platform != "win32":
        return _linux_storage(path)
    if not drive:
        return STORAGE_NETWORK, "Path is a UNC share; no local volume to query."

    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
    except Exception:
        drive_type = 0
    if drive_type == _DRIVE_REMOTE:
        return STORAGE_NETWORK, f"{drive} is a mapped network drive."

    bus, penalty = _query_volume(drive)

    if bus == _BUS_NVME:
        return STORAGE_NVME, f"{drive} is on an NVMe bus."
    if bus in (_BUS_USB, _BUS_SD, _BUS_MMC):
        return STORAGE_REMOVABLE, (
            f"{drive} is on a USB or card-reader bus; treated as seek-bound "
            f"whatever the media claims."
        )
    if penalty == 1:
        return STORAGE_HDD, f"{drive} reports a seek penalty: mechanical disk."
    if penalty == 0:
        return STORAGE_SSD, f"{drive} reports no seek penalty: solid-state."
    if drive_type == _DRIVE_REMOVABLE:
        return STORAGE_REMOVABLE, f"{drive} is removable media."
    return STORAGE_UNKNOWN, (
        f"{drive} would not answer the seek-penalty query; "
        f"assuming solid-state behaviour but leaving the CPU guards on."
    )


# ──────────────────────── probing, exactly once, per volume ─────────────────
#
# Cached because ``resolved_processes`` is called from the settings window while
# the user drags a spin box.  The Windows path is two IOCTLs and costs
# microseconds, but "cheap" is not "free at 60 Hz".

_lock = threading.Lock()
_cache: dict[str, MachineProfile] = {}


def logical_cpus() -> int:
    """Logical processors this process may actually run on.

    Affinity, not the installed count: a machine that has pinned Lumen to four
    processors has four, whatever the die holds.
    """
    count = os.cpu_count() or 4
    try:
        affinity = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        if affinity:
            count = affinity
    except (AttributeError, OSError):
        pass
    return max(1, count)


def profile(path: str | os.PathLike[str] | None = None) -> MachineProfile:
    """This machine, as it relates to *path* (the library root, normally)."""
    target = str(path) if path is not None else os.getcwd()
    key = _volume_root(target) or target
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        storage, detail = detect_storage(target)
    except Exception:                                   # pragma: no cover
        storage, detail = STORAGE_UNKNOWN, "Storage probe failed."
    built = MachineProfile(
        logical_cpus=logical_cpus(),
        ram_bytes=total_ram_bytes(),
        storage=storage,
        storage_detail=detail,
        probed_path=target,
    )
    with _lock:
        _cache[key] = built
    return built


def refresh() -> None:
    """Forget every cached answer, so the next question re-probes the machine."""
    with _lock:
        _cache.clear()
