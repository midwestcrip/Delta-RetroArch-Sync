"""Optional local configuration.

Everything here is an override for automatic discovery, so the tool works with
no config at all on a normal install. config.toml is gitignored: it holds
machine-specific absolute paths, which do not belong in a public repo. See
config.example.toml for the accepted keys.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
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

    #: Standalone emulators to sync to, by key -- "mgba", "mupen64plus" and so
    #: on. Empty by default, which is the whole of the opt-in: every installed
    #: emulator is *found* and reported, and none is written to until it is
    #: named here. Having mGBA installed is not a statement that these games
    #: should be synced into it.
    emulators_enabled: tuple[str, ...] = ()
    #: Where each emulator is installed, when discovery cannot find it. The
    #: value may be the executable or the folder holding it.
    emulator_paths: dict[str, Path] = field(default_factory=dict)
    #: Where each emulator keeps its saves, when its own config does not say and
    #: the default is wrong. This is the setting the tool tells you to write
    #: when it cannot work the folder out for itself.
    emulator_save_dirs: dict[str, Path] = field(default_factory=dict)

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
    #: Whether the one-time "add shortcuts?" offer has been made. Recorded
    #: rather than inferred from a shortcut existing, so that declining it
    #: once, or deleting a shortcut later, is not treated as an invitation to
    #: ask again.
    shortcuts_offered: bool = False


def rom_dir(config: Config, retroarch_config: Path | None) -> Path | None:
    """Where ROMs copied out of Delta go, set or derived.

    RetroArch has no canonical ROM location, so unset means "a folder beside the
    config". Defined once here because more than one caller needs the answer and
    they must agree: the `sync` command derived it inline while the restore path
    read only the configured value, so a save written beside the ROM by default
    was looked for somewhere that value was ``None`` -- and a standalone
    emulator's backup could not be put back at all, which is the commonest case
    rather than an edge one, since beside-the-ROM is most of these emulators'
    default.
    """
    if config.retroarch_rom_dir is not None:
        return config.retroarch_rom_dir
    if retroarch_config is not None:
        return retroarch_config.parent / "roms"
    return None


def emulator_dirs(config: Config) -> dict[str, Path]:
    """Each configured emulator path as a *folder*, still keyed by emulator.

    A ``path`` in config.toml may name either the executable or the folder
    holding it, because both are things someone reasonably types when asked
    where a program is. Resolving that is the only work here.

    Defined once, for the same reason as :func:`rom_dir`, and keyed for a
    sharper one: all three callers used to flatten this dict to a bare list of
    folders before handing it to discovery, which threw away *which emulator
    each path was for*. A folder set under ``[emulators.mgba]`` then answered
    for every other emulator too, and could pull one away from the install the
    registry knew about. Handing back the mapping is what keeps that from being
    expressible.
    """
    return {
        key: raw.parent if raw.is_file() else raw
        for key, raw in config.emulator_paths.items()
    }


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

    # [emulators] holds one list plus a subtable per emulator, so its own keys
    # and its subtables are read separately. A malformed section reads as no
    # section at all rather than raising: this file is hand-edited, and a typo
    # in an optional setting should not stop the tool from starting.
    raw_emulators = data.get("emulators")
    emulators = raw_emulators if isinstance(raw_emulators, dict) else {}
    enabled_raw = emulators.get("enabled")
    enabled = (
        tuple(str(key) for key in enabled_raw if isinstance(key, str))
        if isinstance(enabled_raw, list)
        else ()
    )
    emulator_paths: dict[str, Path] = {}
    emulator_save_dirs: dict[str, Path] = {}
    for key, section in emulators.items():
        if not isinstance(section, dict):
            continue
        install = _path(section.get("path"))
        if install is not None:
            emulator_paths[str(key)] = install
        save_dir = _path(section.get("save_dir"))
        if save_dir is not None:
            emulator_save_dirs[str(key)] = save_dir

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
        # start_menu_offered was this key's name while the offer covered only
        # the Start menu. Read as a fallback so nobody who already answered is
        # asked a second time.
        shortcuts_offered=flag(
            "shortcuts_offered", flag("start_menu_offered", False)
        ),
        emulators_enabled=enabled,
        emulator_paths=emulator_paths,
        emulator_save_dirs=emulator_save_dirs,
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
        f"shortcuts_offered = {str(config.shortcuts_offered).lower()}",
        "",
    ]

    # Written back rather than dropped. The launcher saves this whole file
    # whenever any setting changes, so anything omitted here is deleted by the
    # next press of Save settings -- which would quietly un-enable an emulator
    # the user had configured by hand.
    keys = sorted(
        set(config.emulators_enabled)
        | set(config.emulator_paths)
        | set(config.emulator_save_dirs)
    )
    if keys or config.emulators_enabled:
        listed = ", ".join(f'"{key}"' for key in config.emulators_enabled)
        body += ["[emulators]", f"enabled = [{listed}]", ""]
        for key in keys:
            section = [f"[emulators.{key}]"]
            if key in config.emulator_paths:
                section.append(line("path", config.emulator_paths[key]))
            if key in config.emulator_save_dirs:
                section.append(line("save_dir", config.emulator_save_dirs[key]))
            if len(section) > 1:
                body += section + [""]

    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path
