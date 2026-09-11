"""Locate Delta's Dropbox mirror and RetroArch's directories on this machine.

Nothing here is hardcoded to one user's layout: paths are discovered, and every
discovery reports whether it actually succeeded so the inspector can tell the
user precisely what is missing rather than failing on a wrong assumption.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Harmony's folder name for Delta, set in Delta's SyncManager:
#: ``DropboxService.shared.preferredDirectoryName = "Delta Emulator"``.
DELTA_FOLDER_NAME = "Delta Emulator"


@dataclass
class Discovery:
    """One located path, or an explanation of why it could not be found."""

    label: str
    path: Path | None
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None


def dropbox_roots() -> list[Path]:
    """Every Dropbox root the desktop client reports, newest config first.

    The client writes info.json listing each linked account's local path. That
    is authoritative; guessing ``~/Dropbox`` is not, because the user can move
    the folder anywhere.
    """
    roots: list[Path] = []
    for base in (
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
    ):
        if not base:
            continue
        info = Path(base) / "Dropbox" / "info.json"
        if not info.is_file():
            continue
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for account in data.values():
            raw = account.get("path") if isinstance(account, dict) else None
            if raw:
                roots.append(Path(raw))

    # Fall back to the conventional location only if info.json told us nothing.
    if not roots:
        fallback = Path.home() / "Dropbox"
        if fallback.is_dir():
            roots.append(fallback)
    return roots


#: Where the Dropbox desktop client puts itself. It is a per-user install by
#: default but the 32-bit Program Files path is what a real machine here
#: actually had, so all three are checked rather than assumed.
_DROPBOX_CLIENTS = (
    ("ProgramFiles(x86)", "Dropbox/Client/Dropbox.exe"),
    ("ProgramFiles", "Dropbox/Client/Dropbox.exe"),
    ("LOCALAPPDATA", "Dropbox/Client/Dropbox.exe"),
)


def dropbox_client() -> Path | None:
    """The installed Dropbox client, whether or not anyone has signed in.

    Separate from :func:`dropbox_roots` on purpose. ``info.json`` only appears
    once an account is linked, so an installed-but-signed-out Dropbox looks
    exactly like no Dropbox at all -- and the third naive-user test hit that
    case and was told to install software it already had.
    """
    for variable, relative in _DROPBOX_CLIENTS:
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = Path(base).joinpath(*relative.split("/"))
        if candidate.is_file():
            return candidate

    # The client has run here even if its executable has since moved: this is
    # where it keeps its databases, and it is created on first launch.
    base = os.environ.get("LOCALAPPDATA")
    if base and (Path(base) / "Dropbox").is_dir():
        return Path(base) / "Dropbox"
    return None


def find_delta_folder() -> Discovery:
    """Find the 'Delta Emulator' folder inside whichever Dropbox root has it.

    Delta requests full-Dropbox access, so the folder is expected at the root of
    Dropbox rather than under /Apps. We check both rather than assume.
    """
    roots = dropbox_roots()
    if not roots:
        # Two different problems with two different answers. Telling someone to
        # install what they have already installed sends them to a download
        # page instead of to the sign-in button three feet away.
        if dropbox_client() is not None:
            return Discovery(
                "Delta Emulator folder",
                None,
                "Dropbox is installed but not signed in. Open Dropbox and sign "
                "into the account Delta syncs to, then press Check status.",
            )
        return Discovery(
            "Delta Emulator folder",
            None,
            "Dropbox is not installed. Install the Dropbox desktop client from "
            "dropbox.com, then sign into the account Delta syncs to.",
        )

    for root in roots:
        for candidate in (
            root / DELTA_FOLDER_NAME,
            root / "Apps" / DELTA_FOLDER_NAME,
        ):
            if candidate.is_dir():
                return Discovery("Delta Emulator folder", candidate)

    listed = ", ".join(str(r) for r in roots)
    return Discovery(
        "Delta Emulator folder",
        None,
        f"Dropbox found at {listed}, but no '{DELTA_FOLDER_NAME}' folder in it. "
        "In Delta: Settings -> Delta Sync -> connect Dropbox and let one full "
        "sync finish.",
    )


def dirs_from_uninstall(match: str) -> list[Path]:
    """Ask Windows where a program whose DisplayName contains ``match`` lives.

    An installer lets you put a program anywhere, so a candidate list of "usual"
    paths misses real installs. The uninstall registry entry is authoritative.
    InstallLocation is often blank -- it is for RetroArch's installer -- but
    DisplayIcon points at the executable, so fall back to its parent directory.

    ``match`` is compared case-insensitively against DisplayName. It is a
    substring rather than an equality test because publishers append versions
    ("mGBA 0.10.3"), and it is matched against the *display* name rather than
    the key name because the key is often a GUID.
    """
    try:
        import winreg
    except ImportError:  # Not on Windows.
        return []

    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    wanted = match.lower()
    found: list[Path] = []
    for hive, subkey in hives:
        try:
            root = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, index)
                    with winreg.OpenKey(root, name) as entry:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                        if wanted not in display.lower():
                            continue
                        for value, is_exe in (
                            ("InstallLocation", False),
                            ("DisplayIcon", True),
                        ):
                            try:
                                raw = str(winreg.QueryValueEx(entry, value)[0]).strip()
                            except OSError:
                                continue
                            if not raw:
                                continue
                            # DisplayIcon may carry an icon index: "path.exe,0".
                            path = Path(raw.split(",")[0].strip('"'))
                            directory = path.parent if is_exe else path
                            if directory.is_dir():
                                found.append(directory)
                except OSError:
                    continue
    return found


def _registry_retroarch_dirs() -> list[Path]:
    return dirs_from_uninstall("retroarch")


#: Windows records the full path of every executable the user has actually run,
#: keyed as "<path>.FriendlyAppName" and "<path>.ApplicationCompany".
_MUICACHE_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
)
_MUICACHE_SUFFIXES = (".FriendlyAppName", ".ApplicationCompany")


def _executable_name(path: str) -> str:
    """The filename part of a Windows path, lowercased.

    Not ``Path(path).name``: MuiCache stores Windows paths, and this module is
    imported on non-Windows when the tests run there, where ``Path`` would treat
    the whole backslash string as one filename.
    """
    return path.replace("/", "\\").rpartition("\\")[2].lower()


def dirs_from_muicache(names: Iterable[str], executables: Sequence[str]) -> list[Path]:
    """Pull an executable's directory out of MuiCache value names.

    Separated from the registry read so it can be tested without a registry.

    The match is on the whole filename rather than a suffix, which is what keeps
    ``RetroArch-Win64-setup.exe`` from being read as an install: running the
    installer leaves a MuiCache entry of its own, and its directory is Downloads.
    """
    wanted = {name.lower() for name in executables}
    directories: list[Path] = []
    for name in names:
        trimmed = name
        for suffix in _MUICACHE_SUFFIXES:
            if trimmed.endswith(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        else:
            continue
        if _executable_name(trimmed) not in wanted:
            continue
        directory = Path(trimmed).parent
        if directory not in directories:
            directories.append(directory)
    return directories


def retroarch_dirs_from_muicache(names: Iterable[str]) -> list[Path]:
    """RetroArch's own case of :func:`dirs_from_muicache`."""
    return dirs_from_muicache(names, ("retroarch.exe",))


def muicache_dirs(executables: Sequence[str]) -> list[Path]:
    """Where a program has been run from, which finds portable copies.

    The uninstall registry only knows about installs made by an installer, and
    it keeps pointing at them after the folder is deleted. A RetroArch extracted
    from the portable zip is invisible to it. On this project's own machine the
    uninstall entry named a stale C:\\RetroArch-Win64 that no longer existed
    while the RetroArch actually in use sat under C:\\Media\\Games\\Emulators,
    so discovery reported nothing and the user had to set the path by hand.

    MuiCache is the difference: Windows writes an entry the first time you run
    an executable, wherever it lives. That matters more for the standalone
    emulators than it does for RetroArch -- most of them ship as a zip with no
    installer at all, so the uninstall registry knows nothing about them and
    this is the only registry source that does.
    """
    try:
        import winreg
    except ImportError:  # Not on Windows.
        return []

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MUICACHE_KEY)
    except OSError:
        return []

    names: list[str] = []
    with key:
        for index in range(winreg.QueryInfoKey(key)[1]):
            try:
                names.append(winreg.EnumValue(key, index)[0])
            except OSError:
                continue
    return dirs_from_muicache(names, executables)


def _muicache_retroarch_dirs() -> list[Path]:
    return muicache_dirs(("retroarch.exe",))


def dirs_from_app_paths(executables: Sequence[str]) -> list[Path]:
    """The App Paths registration, when an installer wrote one."""
    try:
        import winreg
    except ImportError:
        return []

    found: list[Path] = []
    for executable in executables:
        subkey = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" "\\" + executable
        )
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    raw = str(winreg.QueryValueEx(key, "")[0]).strip('"').strip()
            except OSError:
                continue
            if raw:
                directory = Path(raw).parent
                if directory not in found:
                    found.append(directory)
                break
    return found


def _app_paths_retroarch_dir() -> list[Path]:
    return dirs_from_app_paths(("retroarch.exe",))


def install_dirs(executables: Sequence[str], uninstall_match: str = "") -> list[Path]:
    """Every directory on this machine that might hold one of ``executables``.

    The three registry sources answer different questions and none of them is
    sufficient alone: App Paths and the uninstall list only know about installers,
    and both keep naming a folder after it is deleted, while MuiCache only knows
    what has actually been run. Most standalone emulators ship as a plain zip, so
    for them MuiCache is usually the only source that says anything at all.

    Nothing here checks that the directory still exists -- the caller filters by
    looking for the file it actually wants, which is what makes it safe to
    consult sources that go stale.
    """
    found: list[Path] = []
    sources = [dirs_from_app_paths(executables), muicache_dirs(executables)]
    if uninstall_match:
        sources.insert(0, dirs_from_uninstall(uninstall_match))
    for source in sources:
        for directory in source:
            if directory not in found:
                found.append(directory)
    return found


def _config_candidates() -> list[Path]:
    """Every place retroarch.cfg could be, best guess first.

    Split out from the search so a test can empty it. The registry sources are
    already stubbable one by one, but the fixed paths below are not, and one of
    them -- C:/RetroArch-Win64 -- exists on this project's own machine, which
    made "no RetroArch anywhere" impossible to test against.
    """
    return [
        directory / "retroarch.cfg"
        for directory in (
            _registry_retroarch_dirs()
            + _app_paths_retroarch_dir()
            + _muicache_retroarch_dirs()
        )
    ] + [
        Path(os.environ.get("APPDATA", "")) / "RetroArch" / "retroarch.cfg",
        Path("C:/RetroArch-Win64/retroarch.cfg"),
        Path("C:/RetroArch/retroarch.cfg"),
        Path(os.environ.get("ProgramFiles", "")) / "RetroArch" / "retroarch.cfg",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "RetroArch" / "retroarch.cfg",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Packages"
        / "RetroArch"
        / "retroarch.cfg",
    ]


def find_retroarch_config() -> Discovery:
    """Find retroarch.cfg across the usual Windows install layouts.

    Every source here is filtered by whether the file actually exists, which is
    what makes it safe to consult sources that go stale -- the uninstall entry
    and App Paths both keep naming a folder long after it is deleted.
    """
    for candidate in _config_candidates():
        if candidate.is_file():
            return Discovery("retroarch.cfg", candidate)

    # Same distinction as Dropbox: RetroArch writes its config on first launch,
    # so "installed but never opened" and "not installed" are separate problems
    # and only one of them is solved by downloading anything.
    installed = find_retroarch_exe()
    if installed is not None:
        return Discovery(
            "retroarch.cfg",
            None,
            f"RetroArch is installed at {installed.parent} but has never been "
            "launched, so it has not written its config yet. Open RetroArch "
            "once, close it, then press Check status.",
        )
    return Discovery(
        "retroarch.cfg",
        None,
        "RetroArch is not installed. Install it from retroarch.com and launch "
        "it once so it writes its config, or set retroarch_config in "
        "config.toml.",
    )


_CFG_LINE = re.compile(r'^\s*(?P<key>[A-Za-z0-9_]+)\s*=\s*"?(?P<value>.*?)"?\s*$')


def parse_retroarch_config(path: Path) -> dict[str, str]:
    """Parse retroarch.cfg into a flat dict. The format is ``key = "value"``."""
    settings: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return settings
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _CFG_LINE.match(line)
        if match:
            settings[match["key"]] = match["value"]
    return settings


def resolve_retroarch_dir(
    settings: dict[str, str], key: str, config_path: Path, default_subdir: str
) -> Path:
    """Resolve a directory setting from retroarch.cfg.

    RetroArch writes ``default`` (or an empty value) to mean "the folder next to
    the config", and supports ``:`` as a prefix meaning the install directory.
    """
    raw = settings.get(key, "").strip()
    base = config_path.parent
    if not raw or raw == "default":
        return base / default_subdir
    if raw.startswith(":"):
        return base / raw.lstrip(":/\\")
    return Path(raw)


def truthy(settings: dict[str, str], key: str) -> bool:
    """RetroArch writes booleans as the strings 'true' / 'false'."""
    return settings.get(key, "").strip().lower() == "true"


def installed_cores(config_path: Path, settings: dict[str, str]) -> dict[str, str]:
    """Map each installed core's display name to its library filename.

    RetroArch sorts saves by the core's ``corename``, not its filename, so the
    name in the .info file is what decides the save folder. Only cores actually
    present in the cores directory are returned -- info files ship for every
    core in the catalogue, installed or not.
    """
    cores_dir = resolve_retroarch_dir(settings, "libretro_directory", config_path, "cores")
    info_dir = resolve_retroarch_dir(settings, "libretro_info_path", config_path, "info")
    if not cores_dir.is_dir():
        return {}

    found: dict[str, str] = {}
    for library in cores_dir.glob("*_libretro.dll"):
        info = info_dir / f"{library.stem}.info"
        name = library.stem.replace("_libretro", "")
        if info.is_file():
            for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "corename":
                    name = value.strip().strip('"') or name
                    break
        found[name] = library.name
    return found


def find_retroarch_exe(config_path: Path | None = None) -> Path | None:
    """Locate retroarch.exe, preferring the folder its config lives in.

    The config is already discovered via the registry, and the executable sits
    beside it in every normal install, so that is a better first guess than
    re-deriving the install location.
    """
    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(config_path.parent / "retroarch.exe")
    candidates += [d / "retroarch.exe" for d in _registry_retroarch_dirs()]

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
