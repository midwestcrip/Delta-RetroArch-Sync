"""Locate Delta's Dropbox mirror and RetroArch's directories on this machine.

Nothing here is hardcoded to one user's layout: paths are discovered, and every
discovery reports whether it actually succeeded so the inspector can tell the
user precisely what is missing rather than failing on a wrong assumption.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
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


def find_delta_folder() -> Discovery:
    """Find the 'Delta Emulator' folder inside whichever Dropbox root has it.

    Delta requests full-Dropbox access, so the folder is expected at the root of
    Dropbox rather than under /Apps. We check both rather than assume.
    """
    roots = dropbox_roots()
    if not roots:
        return Discovery(
            "Delta Emulator folder",
            None,
            "No Dropbox install found. Install the Dropbox desktop client and "
            "sign into the account Delta syncs to.",
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


def _registry_retroarch_dirs() -> list[Path]:
    """Ask Windows where RetroArch was installed.

    RetroArch's installer lets you put it anywhere, so a candidate list of
    "usual" paths misses real installs. The uninstall registry entry is
    authoritative. InstallLocation is often blank for this installer, but
    DisplayIcon points at retroarch.exe, so fall back to its parent directory.
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
                        if "retroarch" not in display.lower():
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


#: Windows records the full path of every executable the user has actually run,
#: keyed as "<path>.FriendlyAppName" and "<path>.ApplicationCompany".
_MUICACHE_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
)
_MUICACHE_SUFFIXES = (".FriendlyAppName", ".ApplicationCompany")


def retroarch_dirs_from_muicache(names: Iterable[str]) -> list[Path]:
    """Pull retroarch.exe's directory out of MuiCache value names.

    Separated from the registry read so it can be tested without a registry.
    """
    directories: list[Path] = []
    for name in names:
        trimmed = name
        for suffix in _MUICACHE_SUFFIXES:
            if trimmed.endswith(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        else:
            continue
        if not trimmed.lower().endswith("retroarch.exe"):
            continue
        directory = Path(trimmed).parent
        if directory not in directories:
            directories.append(directory)
    return directories


def _muicache_retroarch_dirs() -> list[Path]:
    """Where RetroArch has been run from, which finds portable copies.

    The uninstall registry only knows about installs made by the installer, and
    it keeps pointing at them after the folder is deleted. A RetroArch extracted
    from the portable zip is invisible to it. On this project's own machine the
    uninstall entry named a stale C:\\RetroArch-Win64 that no longer existed
    while the RetroArch actually in use sat under C:\\Media\\Games\\Emulators,
    so discovery reported nothing and the user had to set the path by hand.

    MuiCache is the difference: Windows writes an entry the first time you run
    an executable, wherever it lives.
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
    return retroarch_dirs_from_muicache(names)


def _app_paths_retroarch_dir() -> list[Path]:
    """The App Paths registration, when RetroArch's installer wrote one."""
    try:
        import winreg
    except ImportError:
        return []

    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\retroarch.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                raw = str(winreg.QueryValueEx(key, "")[0]).strip('"').strip()
        except OSError:
            continue
        if raw:
            return [Path(raw).parent]
    return []


def find_retroarch_config() -> Discovery:
    """Find retroarch.cfg across the usual Windows install layouts.

    Every source here is filtered by whether the file actually exists, which is
    what makes it safe to consult sources that go stale -- the uninstall entry
    and App Paths both keep naming a folder long after it is deleted.
    """
    candidates = [
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
    for candidate in candidates:
        if candidate.is_file():
            return Discovery("retroarch.cfg", candidate)
    return Discovery(
        "retroarch.cfg",
        None,
        "RetroArch config not found. Install RetroArch and launch it once so it "
        "writes its config, or set retroarch_config in config.toml.",
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
