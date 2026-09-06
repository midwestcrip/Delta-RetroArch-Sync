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


def _path(raw: object) -> Path | None:
    return Path(str(raw)).expanduser() if isinstance(raw, str) and raw.strip() else None


def find_config_file(start: Path | None = None) -> Path | None:
    """Look for config.toml next to the project root."""
    base = start or Path(__file__).resolve().parents[2]
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
    return Config(
        delta_folder=_path(paths.get("delta_folder")),
        retroarch_config=_path(paths.get("retroarch_config")),
        retroarch_save_dir=_path(paths.get("retroarch_save_dir")),
        retroarch_rom_dir=_path(paths.get("retroarch_rom_dir")),
    )
