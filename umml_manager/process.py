from __future__ import annotations

import csv
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ProcessInspectionError(RuntimeError):
    """Raised when UMML cannot determine whether the game is running safely."""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    command: str


def running_game_processes(game_dir: str | Path | None = None) -> tuple[ProcessInfo, ...]:
    game = str(Path(game_dir).expanduser()).casefold() if game_dir else ""
    found: list[ProcessInfo] = []
    for process in _iter_processes():
        text = f"{process.name} {process.command}".casefold()
        if "umamusume" in text or (game and game in text and ".exe" in text):
            found.append(process)
    return tuple(found)


def _iter_processes() -> Iterable[ProcessInfo]:
    if os.name == "nt":
        return tuple(_windows_processes())
    return tuple(_procfs_processes())


def _procfs_processes() -> Iterable[ProcessInfo]:
    proc = Path("/proc")
    if not proc.is_dir():
        raise ProcessInspectionError(
            "Process inspection is unavailable because /proc is not mounted"
        )
    try:
        entries = tuple(proc.iterdir())
    except OSError as exc:
        raise ProcessInspectionError(f"Could not inspect /proc: {exc}") from exc

    found: list[ProcessInfo] = []
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            name = (entry / "comm").read_text(errors="replace").strip()
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except OSError:
            # Processes routinely disappear or become unreadable while /proc is
            # traversed. Skipping one entry is safe; failing to inspect /proc at
            # all is not.
            continue
        found.append(ProcessInfo(int(entry.name), name, command))
    return found


def _windows_processes() -> Iterable[ProcessInfo]:
    # The Manager checks for the game every few seconds on the UI thread.
    # Spawning tasklist.exe for that took long enough to stall window
    # dragging; the Toolhelp snapshot takes a few milliseconds.
    try:
        return _toolhelp_processes()
    except Exception:
        return _tasklist_processes()


def _toolhelp_processes() -> list[ProcessInfo]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot_fn = kernel32.CreateToolhelp32Snapshot
    snapshot_fn.argtypes = (wintypes.DWORD, wintypes.DWORD)
    snapshot_fn.restype = wintypes.HANDLE
    first_fn = kernel32.Process32FirstW
    next_fn = kernel32.Process32NextW
    for fn in (first_fn, next_fn):
        fn.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        fn.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    TH32CS_SNAPPROCESS = 0x00000002
    invalid = wintypes.HANDLE(-1).value
    snapshot = snapshot_fn(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    found: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = first_fn(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            if pid and pid != os.getpid():
                name = entry.szExeFile
                found.append(ProcessInfo(pid, name, name))
            ok = next_fn(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not found:
        raise OSError("Toolhelp snapshot returned no processes")
    return found


def _tasklist_processes() -> list[ProcessInfo]:
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            # No console window flash when the Manager has no console.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessInspectionError(f"Could not run tasklist: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown tasklist error").strip()
        raise ProcessInspectionError(
            f"tasklist failed with exit code {result.returncode}: {detail}"
        )

    found: list[ProcessInfo] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1].replace(",", ""))
        except ValueError:
            continue
        found.append(ProcessInfo(pid, row[0], row[0]))
    return found
