"""Optional components that arrive as their own download.

The Controller Pak feature is the reason this exists. Reading Delta's pak files
off the phone needs ``pymobiledevice3``, Apple's usbmux service, a cable and a
trust pairing -- in a program whose whole dependency list is the standard
library. Making everyone carry that so a few people can have Controller Paks is
the wrong trade, and so is not building it.

So it ships as a **second executable**, downloaded separately, and this module
is how the main program notices it is there.

**A process, not an import.** The tempting version is a folder added to
``sys.path``. It does not work: this program is frozen around one embedded
CPython, and an add-on's compiled wheels (``pymobiledevice3`` pulls in
``cryptography``) load only against exactly that interpreter's version and ABI.
An importable add-on would move the dependency problem from build time to run
time, where it breaks the app it was meant to extend. A process boundary also
means a USB stall waiting on a trust dialog cannot hang the launcher's event
loop, and a crash in the add-on is a message rather than a dead program.

**The add-on moves files; this program writes saves.** Nothing an add-on
returns is written to disk by the add-on itself. It hands back bytes or drops
them in a folder of ours, and the ordinary save-writing path -- with its rolling
backups and its size guards -- puts them where they go. Backup discipline lives
in one place, and that place is not the component with the third-party
dependency in it.

The wire format is JSON Lines on stdout: one object per line, the last one
``{"type": "result", ...}``. Anything else on the line is ignored rather than
fatal, so an add-on that prints a warning does not become an add-on that failed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Bumped when the shape of a request or reply changes in a way an older
#: add-on could not satisfy. An add-on declares the protocol it speaks and one
#: that does not match ours is listed but never run -- the alternative is
#: running it and interpreting its answer wrongly.
PROTOCOL = 1

MANIFEST_NAME = "addon.json"

#: Where add-ons are looked for, under each searched root.
ADDON_DIRNAME = "addons"

#: Long enough for a cable, a trust prompt and a slow read; short enough that a
#: wedged add-on does not hold the window forever.
DEFAULT_TIMEOUT = 120.0


class AddonError(Exception):
    """An add-on could not be used, with the reason."""


@dataclass(frozen=True)
class Addon:
    """One installed add-on, as its manifest describes it."""

    identifier: str
    name: str
    version: str
    protocol: int
    executable: Path
    provides: tuple[str, ...]
    manifest_path: Path

    @property
    def folder(self) -> Path:
        return self.manifest_path.parent

    @property
    def speaks_our_protocol(self) -> bool:
        return self.protocol == PROTOCOL

    @property
    def usable(self) -> bool:
        return self.speaks_our_protocol and self.executable.is_file()

    def describe(self) -> str:
        if not self.executable.is_file():
            return f"{self.name} {self.version} — its program is missing"
        if not self.speaks_our_protocol:
            return (
                f"{self.name} {self.version} — built for a different version of "
                f"this program (speaks {self.protocol}, this needs {PROTOCOL})"
            )
        return f"{self.name} {self.version}"


@dataclass(frozen=True)
class AddonReply:
    """What came back from one run."""

    ok: bool
    #: Every JSON object the add-on printed, in order. Progress lines included.
    events: tuple[dict, ...] = ()
    #: The final ``{"type": "result"}`` object, or empty when there was none.
    result: dict = field(default_factory=dict)
    error: str | None = None

    def progress(self) -> tuple[str, ...]:
        return tuple(
            str(event.get("message", ""))
            for event in self.events
            if event.get("type") == "progress" and event.get("message")
        )


def _safe_executable(folder: Path, named: object) -> Path:
    """Resolve a manifest's executable name, refusing anything clever.

    A manifest is a file on disk that says which program to run, so it is worth
    being strict about: a bare filename only, resolved inside the add-on's own
    folder. Someone who can write into that folder can already replace the
    program itself, so this is not the last line of defence -- but a manifest
    that can name ``../../anything.exe`` turns a dropped text file into code
    execution, and there is no reason to allow it.
    """
    if not isinstance(named, str) or not named.strip():
        raise AddonError(f"{MANIFEST_NAME} does not name an executable")

    name = named.strip()
    if name != Path(name).name or name in (".", ".."):
        raise AddonError(
            f"{MANIFEST_NAME} names {name!r}, which is a path rather than a "
            f"filename. An add-on may only run a program in its own folder."
        )

    candidate = folder / name
    try:
        inside = candidate.resolve().parent == folder.resolve()
    except OSError as error:  # pragma: no cover -- unreadable folder
        raise AddonError(f"cannot resolve {candidate}: {error}") from error
    if not inside:
        raise AddonError(f"{name!r} resolves outside {folder}")
    return candidate


def read_manifest(path: Path) -> Addon:
    """Read one ``addon.json``.

    Raises rather than returning ``None``: a manifest that is present and
    malformed is worth a visible complaint, unlike a folder that simply has no
    add-on in it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AddonError(f"cannot read {path}: {error}") from error

    if not isinstance(raw, dict):
        raise AddonError(f"{path} is not a JSON object")

    identifier = str(raw.get("addon", "")).strip()
    if not identifier:
        raise AddonError(f"{path} does not say which add-on it is")

    protocol = raw.get("protocol")
    if not isinstance(protocol, int):
        raise AddonError(f"{path} does not declare a protocol version")

    provides = raw.get("provides", ())
    if not isinstance(provides, (list, tuple)):
        provides = ()

    return Addon(
        identifier=identifier,
        name=str(raw.get("name") or identifier),
        version=str(raw.get("version") or "unknown"),
        protocol=protocol,
        executable=_safe_executable(path.parent, raw.get("executable")),
        provides=tuple(str(item) for item in provides),
        manifest_path=path,
    )


def search_roots(state_dir: Path) -> list[Path]:
    """Where to look, nearest first.

    Beside the program is what someone unzipping an add-on into the app folder
    expects. ``addons/`` under the state directory is where it goes when the app
    itself lives somewhere unwritable, which is the same fallback
    ``paths.state_dir`` already makes for everything else.
    """
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(state_dir)

    found: list[Path] = []
    for root in roots:
        for candidate in (root / ADDON_DIRNAME, root):
            if candidate not in found:
                found.append(candidate)
    return found


def find_addons(roots: list[Path]) -> list[Addon]:
    """Every add-on under these roots, one folder deep.

    One deep on purpose. An add-on is a folder with a manifest at its top, and
    walking further would mean reading every file in the RetroArch install
    someone unzipped next door.
    """
    found: dict[str, Addon] = {}
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root / MANIFEST_NAME]
        try:
            candidates += [
                child / MANIFEST_NAME for child in sorted(root.iterdir())
                if child.is_dir()
            ]
        except OSError:
            continue
        for manifest in candidates:
            if not manifest.is_file():
                continue
            try:
                addon = read_manifest(manifest)
            except AddonError:
                # A broken manifest is not a reason to hide the working add-on
                # sitting beside it. It surfaces when something asks for it by
                # name, where there is somewhere to say so.
                continue
            # Nearest root wins, so an add-on beside the program beats a stale
            # copy in the state folder.
            found.setdefault(addon.identifier, addon)
    return list(found.values())


def find(identifier: str, roots: list[Path]) -> Addon | None:
    return next(
        (a for a in find_addons(roots) if a.identifier == identifier), None
    )


def _no_console() -> int:
    """Keep a console window from flashing up on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _terminate_tree(process: "subprocess.Popen") -> None:
    """Kill the add-on *and anything it started*.

    ``kill`` on Windows kills only the process named. A child that spawned a
    helper leaves the grandchild holding the pipes open, so the read after a
    timeout blocks for as long as the grandchild lives -- the timeout does not
    time out. Measured: a one-second timeout against an add-on whose helper
    slept for thirty took the full thirty seconds to return. In the launcher
    that is a frozen window, and the timeout exists precisely so there is not
    one.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            creationflags=_no_console(),
        )
    else:  # pragma: no cover -- this program ships on Windows
        process.kill()


def run(
    addon: Addon,
    arguments: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> AddonReply:
    """Run one add-on command and read its JSON Lines back.

    Never raises for anything the add-on does -- a crash, a timeout, garbage on
    stdout and a clean refusal all come back as an ``AddonReply`` that is not
    ``ok``. The caller is a button, and a button needs a sentence to show.
    """
    if not addon.speaks_our_protocol:
        return AddonReply(
            ok=False,
            error=(
                f"{addon.name} speaks protocol {addon.protocol} and this "
                f"program speaks {PROTOCOL}. Update whichever is older."
            ),
        )
    if not addon.executable.is_file():
        return AddonReply(ok=False, error=f"{addon.executable} is not there")

    command = [str(addon.executable), "--json", *arguments]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(addon.folder),
            creationflags=_no_console(),
            # Not optional, and not tidiness. Redirecting any of the three
            # standard handles makes Windows require all three, and a windowed
            # build has no console -- so the inherited stdin is not a valid
            # handle and the call dies with "[WinError 6] The handle is
            # invalid" before the add-on is even started. Found because pytest
            # replaces stdin the same way; it would have shipped as "add-ons
            # work when run from source and never in the packaged app".
            #
            # It is also the right contract: an add-on that waits on stdin gets
            # end-of-file instead of hanging until the timeout.
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        return AddonReply(ok=False, error=f"could not start {addon.name}: {error}")

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover -- unkillable child
            process.kill()
        return AddonReply(
            ok=False,
            error=(
                f"{addon.name} did not answer within {timeout:.0f} seconds. If "
                f"your phone is asking you to trust this computer, answer it "
                f"and try again."
            ),
        )

    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # Not everything an add-on prints is ours. Ignoring it beats
            # failing a good run because a library wrote a warning to stdout.
            continue
        if isinstance(parsed, dict):
            events.append(parsed)

    result = next(
        (event for event in reversed(events) if event.get("type") == "result"), None
    )

    if result is None:
        detail = (stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {process.returncode}"
        return AddonReply(
            ok=False,
            events=tuple(events),
            error=f"{addon.name} did not report a result ({tail})",
        )

    ok = bool(result.get("ok")) and process.returncode == 0
    return AddonReply(
        ok=ok,
        events=tuple(events),
        result=result,
        error=None if ok else str(result.get("error") or "the add-on reported a failure"),
    )
