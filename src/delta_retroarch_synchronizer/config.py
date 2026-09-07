"""Optional local configuration.

Everything here is an override for automatic discovery, so the tool works with
no config at all on a normal install. config.toml is gitignored: it holds
machine-specific absolute paths, which do not belong in a public repo. See
config.example.toml for the accepted keys.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import paths as paths_module

CONFIG_FILENAME = "config.toml"


@dataclass
class Config:
    #: Override for Delta's synced folder inside Dropbox.
    delta_folder: Path | None = None
    #: Override for the path to retroarch.cfg.
    retroarch_config: Path | None = None
    #: Override for RetroArch's save directory, if it is not what the cfg says.
    retroarch_save_dir: Path | None = None
    #: Where RetroArch should find ROMs copied over from Delta.
    retroarch_rom_dir: Path | None = None
    #: Path to retroarch.exe, for the launcher.
    retroarch_exe: Path | None = None

    #: Write desktop saves back into Delta's Dropbox folder. Off by default:
    #: Delta assumes it is the only writer to that folder, so pushing is the
    #: only part of this tool that can leave Delta needing manual repair.
    push_enabled: bool = False
    #: Copy ROMs out of Delta so RetroArch has content to load.
    sync_roms: bool = True
    #: Write Delta's cheats out as RetroArch .cht files.
    sync_cheats: bool = True
    #: Pull from Delta as soon as the launcher opens, so the desktop is
    #: current before you press Play.
    sync_on_open: bool = True
    #: Overrides the bundled Dropbox app key. Only needed by someone who
    #: would rather use their own app registration.
    dropbox_app_key: str = ""
    #: Whether the one-time "add this to your Start menu?" offer has been made.
    #: Recorded rather than inferred from the shortcut existing, so that
    #: declining it once, or deleting the shortcut later, is not treated as an
    #: invitation to ask again.
    start_menu_offered: bool = False


def _path(raw: object) -> Path | None:
    return Path(str(raw)).expanduser() if isinstance(raw, str) and raw.strip() else None


def find_config_file(start: Path | None = None) -> Path | None:
    """Look for config.toml next to the project root."""
    base = start or paths_module.state_dir()
    candidate = base / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load(path: Path | None = None) -> Config:
    """Load config.toml if present. A missing file is not an error."""
    path = path or find_config_file()
    if path is None:
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return Config()

    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    options = data.get("options", {}) if isinstance(data.get("options"), dict) else {}

    def flag(key: str, default: bool) -> bool:
        value = options.get(key, default)
        return bool(value) if isinstance(value, bool) else default

    return Config(
        delta_folder=_path(paths.get("delta_folder")),
        retroarch_config=_path(paths.get("retroarch_config")),
        retroarch_save_dir=_path(paths.get("retroarch_save_dir")),
        retroarch_rom_dir=_path(paths.get("retroarch_rom_dir")),
        retroarch_exe=_path(paths.get("retroarch_exe")),
        push_enabled=flag("push_enabled", False),
        sync_roms=flag("sync_roms", True),
        sync_cheats=flag("sync_cheats", True),
        sync_on_open=flag("sync_on_open", True),
        dropbox_app_key=str(options.get("dropbox_app_key", "") or ""),
        start_menu_offered=flag("start_menu_offered", False),
    )


def save(config: Config, path: Path | None = None) -> Path:
    """Write config.toml. Only paths the user actually set are recorded.

    Hand-rolled rather than using a TOML writer: the standard library can read
    TOML but not write it, and one dependency for six lines of output is a poor
    trade for a tool that otherwise needs none.
    """
    path = path or (paths_module.state_dir() / CONFIG_FILENAME)

    def line(key: str, value: Path | None) -> str:
        if value is None:
            return f"# {key} = \"\""
        return f'{key} = "{str(value).replace(chr(92), "/")}"'

    body = [
        "# Written by the launcher. Safe to edit by hand.",
        "# Gitignored: these paths are specific to this machine.",
        "",
        "[paths]",
        line("delta_folder", config.delta_folder),
        line("retroarch_config", config.retroarch_config),
        line("retroarch_save_dir", config.retroarch_save_dir),
        line("retroarch_rom_dir", config.retroarch_rom_dir),
        line("retroarch_exe", config.retroarch_exe),
        "",
        "[options]",
        f"push_enabled = {str(config.push_enabled).lower()}",
        f"sync_roms = {str(config.sync_roms).lower()}",
        f"sync_cheats = {str(config.sync_cheats).lower()}",
        f"sync_on_open = {str(config.sync_on_open).lower()}",
        f'dropbox_app_key = "{config.dropbox_app_key}"',
        f"start_menu_offered = {str(config.start_menu_offered).lower()}",
        "",
    ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path
