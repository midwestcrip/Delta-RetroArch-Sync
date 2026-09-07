"""Finding and waiting on a process this program did not start.

**Sync and Play** used to call ``Popen`` unconditionally, so pressing it with
RetroArch already open started a second copy. The third naive-user test caught
that, and then asked the better question behind it: what happens to saves when
RetroArch is opened some other way? Nothing did -- the sync only ran around the
process this program launched itself.

Both are the same missing piece. Once a running RetroArch can be found and
waited on, pressing the button attaches to it: sync now, wait for that window to
close, sync again. The save still lands after the process exits, which is the
part that matters -- mGBA only flushes to disk on a clean quit.

ctypes rather than ``tasklist``: this has to answer "which retroarch.exe" (a
machine can hold more than one install) and then wait on it, and the Win32 calls
do both directly. Shelling out would give names without paths, and no handle to
wait on.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

#: Rights needed to read a process's image path and to wait on it. Deliberately
#: the narrowest pair that works: this never needs to read memory or terminate
#: anything, and asking for more would fail against processes at a higher
#: integrity level for no gain.
_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000

_STILL_RUNNING = 0x00000103  # STILL_ACTIVE
_WAIT_OBJECT_0 = 0x00000000

#: Enough for any desktop. EnumProcesses truncates silently rather than failing,
#: so the returned byte count is checked against this below.
_MAX_PROCESSES = 4096


def supported() -> bool:
    return sys.platform == "win32"


def _psapi():
    """Kernel32 exports these on modern Windows; psapi.dll is the older home."""
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover -- not reachable on Windows
        return None


def process_ids() -> list[int]:
    """Every process id on the machine, or an empty list if that cannot be read."""
    if not supported():
        return []
    kernel32 = _psapi()
    if kernel32 is None:  # pragma: no cover
        return []
    try:
        enum_processes = ctypes.WinDLL("psapi", use_last_error=True).EnumProcesses
    except OSError:  # pragma: no cover -- psapi is present on every Windows
        return []

    array = (wintypes.DWORD * _MAX_PROCESSES)()
    written = wintypes.DWORD()
    if not enum_processes(
        ctypes.byref(array), ctypes.sizeof(array), ctypes.byref(written)
    ):
        return []
    count = written.value // ctypes.sizeof(wintypes.DWORD)
    return [pid for pid in array[:count] if pid]


def image_path(pid: int) -> Path | None:
    """Where a process was launched from, or None if it cannot be opened.

    Being unable to open a process is ordinary, not an error: anything running
    as another user or at a higher integrity level refuses, and none of those
    are RetroArch.
    """
    if not supported():
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def find_by_name(name: str) -> list[tuple[int, Path]]:
    """Every running process with this executable name, wherever it lives.

    By name and not by path, which was the first version's mistake. It matched
    the *configured* executable's full path, on the reasoning that a portable
    RetroArch beside an installed one is two different programs sharing a name
    and attaching to the wrong one would wait on a window nobody is playing in.

    True, and beside the point. This machine has retroarch.exe at both
    ``C:/RetroArch-Win64`` and ``C:/Media/Games/Emulators/RetroArch``; config
    named one, the user had started the other, the match failed, and a second
    RetroArch opened -- the exact fault the check exists to prevent. Worse, the
    program then waited on the copy *it* had started, so closing the one being
    played never brought the window back.

    Two copies of an emulator writing saves for the same games is a bad state
    whatever their paths, so any of them is reason enough not to start another.
    """
    if not supported():
        return []

    wanted = name.lower()
    found: list[tuple[int, Path]] = []
    for pid in process_ids():
        path = image_path(pid)
        if path is not None and path.name.lower() == wanted:
            found.append((pid, path))
    return found


def find_running(executable: Path) -> int | None:
    """The process id of a running copy of this executable, by name.

    The path is used only to take the name from; see :func:`find_by_name` for
    why matching the whole path was wrong.
    """
    running = find_by_name(executable.name)
    return running[0][0] if running else None


def wait_for_exit(pid: int, *, timeout_ms: int = 0xFFFFFFFF) -> bool:
    """Block until that process ends. False if it could not be waited on.

    A process that has already gone counts as ended: OpenProcess failing is the
    normal way to discover that, and reporting it as a failure would make the
    caller skip the sync that should follow.
    """
    if not supported():
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, timeout_ms) == _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)
