"""Putting the launcher in the Start menu.

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

import os
import subprocess
import sys
from pathlib import Path

from . import paths

#: Both the file name in the Start menu and what the user searches for.
LINK_NAME = f"{paths.APP_NAME}.lnk"

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


def supported() -> bool:
    """Windows only. The Start menu is not a concept anywhere else."""
    return sys.platform == "win32"


def start_menu_dir() -> Path | None:
    """The per-user Programs folder -- writable without administrator rights.

    Per-user deliberately: the test account was a Standard user, and the one
    thing that already stopped it dead was RetroArch's installer demanding an
    administrator PIN. A shortcut is not worth a second elevation prompt.
    """
    if not supported():
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def link_path() -> Path | None:
    folder = start_menu_dir()
    return folder / LINK_NAME if folder else None


def installed() -> bool:
    link = link_path()
    return bool(link and link.is_file())


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


def create() -> Path:
    """Add the launcher to this user's Start menu. Returns the shortcut path."""
    if not supported():
        raise ShortcutError("The Start menu is a Windows feature.")

    link = link_path()
    if link is None:
        raise ShortcutError("Could not locate the Start menu folder (no %APPDATA%).")

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
    return link


def remove() -> bool:
    """Take it back out. False means there was nothing there to remove."""
    link = link_path()
    if link is None or not link.is_file():
        return False
    try:
        link.unlink()
    except OSError as error:
        raise ShortcutError(str(error)) from error
    return True
