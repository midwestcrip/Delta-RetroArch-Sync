"""Putting the launcher in the Start menu and on the desktop.

The third naive-user test raised this three separate times -- at extraction,
after discovery, and again at the end -- always the same sentence: the app does
not appear in the Start menu or in search, even after being opened and getting
past SmartScreen. That is correct behaviour for a portable zip, which has no
installer to make a shortcut, and it is also the single most repeated complaint
in the report. A program someone cannot find again is a program they stop using.

``tools/install_start_menu.ps1`` already did this, but only from a source
checkout: it targets ``launch_gui.pyw`` and needs Python on PATH, so the one
person who most needs it -- whoever downloaded the zip -- could not run it.

**Why shell out to PowerShell.** A ``.lnk`` is a binary shell-link structure,
not a text file, and the standard library cannot write one. The alternatives
were hand-assembling those bytes (fragile, and wrong is indistinguishable from
right until Windows refuses to open it) or a third-party dependency, which this
project does not have and is not taking on for one file. ``WScript.Shell`` is
the documented way to create one and ships with every Windows install.

Paths reach PowerShell through the environment rather than interpolated into
the script text, so a folder name containing a quote or a ``$`` cannot break --
or reshape -- the command.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from . import paths

#: Both the file name in the Start menu and what the user searches for.
LINK_NAME = f"{paths.APP_NAME}.lnk"

#: FOLDERID_Programs and FOLDERID_Desktop, for SHGetKnownFolderPath.
#:
#: Asked of Windows rather than built from %APPDATA% and the home directory,
#: because either can be redirected and the home-directory guess is the one that
#: fails silently. On the machine this was written for, OneDrive owns the
#: desktop: the real one is ``C:/Users/colso/OneDrive/Desktop`` and
#: ``~/Desktop`` **does not exist**, so writing a shortcut there would have
#: created a folder nobody ever looks at and reported success.
_FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

#: SHChangeNotify events, and the flag saying we are passing paths as strings.
_SHCNE_CREATE = 0x00000002
_SHCNE_DELETE = 0x00000004
_SHCNF_PATHW = 0x0005

DESCRIPTION = "Sync Delta and RetroArch saves, then play"

#: Assigns each value from the environment, so nothing the user's folder names
#: contain is ever parsed as PowerShell.
_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:DRS_LINK); "
    "$s.TargetPath = $env:DRS_TARGET; "
    "$s.Arguments = $env:DRS_ARGS; "
    "$s.WorkingDirectory = $env:DRS_WORKDIR; "
    "$s.Description = $env:DRS_DESC; "
    "if ($env:DRS_ICON) { $s.IconLocation = $env:DRS_ICON }; "
    "$s.Save()"
)


class ShortcutError(Exception):
    """Creating or removing the shortcut failed, with a reason worth showing."""


def _tell_the_shell(path: Path, event: int) -> None:
    """Tell Explorer a shell item appeared or went away.

    Writing the file is not enough. The desktop caches what it is showing, and
    a change made by anything other than Explorer itself can go unnoticed --
    which leaves a **ghost icon**: an icon for a file that is not there. It
    cannot be opened and it cannot be deleted, because there is nothing behind
    it to delete, and it survives until something refreshes the folder.

    The user hit exactly that, trying and failing to send a removed shortcut to
    the Recycle Bin. Their fault report was "it's not going", which is precisely
    what a ghost looks like from the outside.
    """
    if not supported():
        return
    try:
        ctypes.windll.shell32.SHChangeNotify(
            event, _SHCNF_PATHW, ctypes.c_wchar_p(str(path)), None
        )
    except Exception:  # pragma: no cover -- cosmetic; never worth raising for
        pass


def supported() -> bool:
    """Windows only. The Start menu is not a concept anywhere else."""
    return sys.platform == "win32"


def _known_folder(folder_id: str) -> Path | None:
    """Where Windows currently keeps one of its named folders.

    ``SHGetKnownFolderPath`` is the only correct way to ask. It follows
    redirection -- OneDrive, a roaming profile, a policy -- which the obvious
    constructions from %APPDATA% and the home directory do not.
    """
    if not supported():
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_byte * 8),
        ]

    guid = GUID()
    ole32 = ctypes.windll.ole32
    if ole32.CLSIDFromString(folder_id, ctypes.byref(guid)) != 0:
        return None  # pragma: no cover -- the ids above are constants

    buffer = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(buffer)
    )
    if result != 0 or not buffer.value:
        return None
    try:
        return Path(buffer.value)
    finally:
        ole32.CoTaskMemFree(buffer)


def start_menu_dir() -> Path | None:
    """The per-user Programs folder -- writable without administrator rights.

    Per-user deliberately: the test account was a Standard user, and the one
    thing that already stopped it dead was RetroArch's installer demanding an
    administrator PIN. A shortcut is not worth a second elevation prompt.
    """
    folder = _known_folder(_FOLDERID_PROGRAMS)
    if folder is not None:
        return folder
    # Only if the shell would not answer. Correct on an ordinary profile and
    # wrong on a redirected one, which is why it is the fallback and not the
    # first choice.
    appdata = os.environ.get("APPDATA") if supported() else None
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path | None:
    """The desktop, wherever the user's actually is."""
    return _known_folder(_FOLDERID_DESKTOP)


def link_path() -> Path | None:
    folder = start_menu_dir()
    return folder / LINK_NAME if folder else None


def desktop_link_path() -> Path | None:
    folder = desktop_dir()
    return folder / LINK_NAME if folder else None


def links() -> dict[str, Path]:
    """Every place a shortcut goes, by the name the user would call it."""
    found: dict[str, Path] = {}
    start_menu = link_path()
    if start_menu is not None:
        found["Start menu"] = start_menu
    desktop = desktop_link_path()
    if desktop is not None:
        found["desktop"] = desktop
    return found


def installed() -> bool:
    """True if a shortcut exists anywhere. Either one counts, so removing one
    by hand does not leave the button offering to add what is already there."""
    return any(path.is_file() for path in links().values())


def target() -> tuple[Path, str, Path]:
    """What the shortcut should point at: (program, arguments, working dir).

    Frozen, that is the executable itself and nothing else. From a source
    checkout it is ``pythonw.exe`` -- the console-free interpreter -- running
    ``launch_gui.pyw``, because pointing at ``python.exe`` leaves a black
    console window sitting behind the launcher for as long as it is open.
    """
    if paths.is_frozen():
        exe = Path(sys.executable).resolve()
        return exe, "", exe.parent

    root = paths.resource_dir()
    entry = root / "launch_gui.pyw"
    if not entry.is_file():
        raise ShortcutError(f"cannot find {entry}")

    interpreter = Path(sys.executable).resolve()
    windowed = interpreter.with_name("pythonw.exe")
    if windowed.is_file():
        interpreter = windowed
    # Quoted because a checkout path with a space would otherwise arrive as two
    # arguments.
    return interpreter, f'"{entry}"', root


def icon_location() -> str:
    """Where Windows should read the shortcut's icon from.

    Empty when frozen, which is not an omission: the executable already carries
    the icon, while the copy unpacked beside it lives in a temporary directory
    PyInstaller deletes on exit -- so pointing the shortcut there would give a
    blank icon the moment the program closed.
    """
    if paths.is_frozen():
        return ""
    candidate = paths.resource_dir() / "assets" / "synchronizer.ico"
    return str(candidate) if candidate.is_file() else ""


def _run_powershell(script: str, variables: dict[str, str]) -> None:
    environment = dict(os.environ)
    environment.update(variables)
    # Without this the GUI flashes a console window every time.
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=environment,
            # Explicit, not inherited. A windowed build has no console, so its
            # standard handles are invalid, and handing one of those to
            # CreateProcess fails the whole call with "the handle is invalid"
            # before PowerShell starts. Caught by the test below, which runs
            # under pytest's capture -- the same shape of problem.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ShortcutError(str(error)) from error

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ShortcutError(detail.splitlines()[0] if detail else "PowerShell failed")


def write_link(link: Path) -> Path:
    """Write one shortcut at exactly this path."""
    program, arguments, working_dir = target()
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ShortcutError(str(error)) from error

    _run_powershell(
        _SCRIPT,
        {
            "DRS_LINK": str(link),
            "DRS_TARGET": str(program),
            "DRS_ARGS": arguments,
            "DRS_WORKDIR": str(working_dir),
            "DRS_DESC": DESCRIPTION,
            "DRS_ICON": icon_location(),
        },
    )

    # PowerShell can report success while writing nothing if the COM call was
    # refused, so confirm against the filesystem rather than the exit code.
    if not link.is_file():
        raise ShortcutError(f"{link} was not created")
    _tell_the_shell(link, _SHCNE_CREATE)
    return link


def create() -> dict[str, Path]:
    """Put the launcher in the Start menu and on the desktop.

    Both, because they answer different habits: one is for people who search,
    the other for people who look. Returns what was written, keyed by place.
    """
    if not supported():
        raise ShortcutError("Shortcuts like these are a Windows feature.")

    places = links()
    if not places:
        raise ShortcutError("Could not locate the Start menu or the desktop.")

    written: dict[str, Path] = {}
    errors: list[str] = []
    for name, link in places.items():
        try:
            written[name] = write_link(link)
        except ShortcutError as error:
            errors.append(f"{name}: {error}")

    # One failing is not both failing. A desktop that cannot be written to is
    # no reason to withhold the Start menu entry.
    if not written:
        raise ShortcutError("; ".join(errors))
    return written


def remove() -> dict[str, Path]:
    """Take them back out. An empty result means there was nothing there."""
    removed: dict[str, Path] = {}
    for name, link in links().items():
        if not link.is_file():
            continue
        try:
            link.unlink()
        except OSError as error:
            raise ShortcutError(str(error)) from error
        _tell_the_shell(link, _SHCNE_DELETE)
        removed[name] = link
    return removed
