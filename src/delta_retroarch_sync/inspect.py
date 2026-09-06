"""Read-only inspection pass.

This is phase one of the project and it never writes a byte to either side. It
answers the questions the design depends on, against real data rather than
assumption: where the folders actually are, what Delta has synced, which games
map to which system, and whether RetroArch already holds a save for each one.

Run it after installing Dropbox and RetroArch, before enabling any sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config as config_module
from . import discovery, harmony, naming, systems


@dataclass
class GameEntry:
    """A game Delta has synced, joined to its save and RetroArch counterpart."""

    identifier: str
    name: str
    delta_type: str
    system: systems.System | None
    rom_path: Path | None
    save_path: Path | None
    extra_paths: dict[str, Path]
    retroarch_save: Path | None = None

    @property
    def system_label(self) -> str:
        return self.system.name if self.system else f"unknown ({self.delta_type})"

    @property
    def supported(self) -> bool:
        return self.system is not None and self.system.key in systems.ENABLED_SYSTEMS


def collect_games(delta_folder: Path) -> list[GameEntry]:
    """Join Delta's Game records to their GameSave records by shared SHA-1."""
    games: dict[str, harmony.HarmonyRecord] = {}
    saves: dict[str, harmony.HarmonyRecord] = {}

    for record in harmony.iter_records(delta_folder):
        if record.type == "Game":
            if record.identifier not in harmony.BIOS_IDENTIFIERS:
                games[record.identifier] = record
        elif record.type == "GameSave":
            saves[record.identifier] = record

    entries: list[GameEntry] = []
    for identifier, game in games.items():
        delta_type = str(game.fields.get("type", ""))
        system = systems.for_delta_type(delta_type)

        save_record = saves.get(identifier)
        save_path = None
        extra_paths: dict[str, Path] = {}
        if save_record is not None:
            save_path = harmony.attached_file(delta_folder, save_record, "gameSave")
            for file_id in system.extra_files if system else ():
                found = harmony.attached_file(delta_folder, save_record, file_id)
                if found:
                    extra_paths[file_id] = found

        entries.append(
            GameEntry(
                identifier=identifier,
                name=game.name or "(unnamed)",
                delta_type=delta_type,
                system=system,
                rom_path=harmony.attached_file(delta_folder, game, "game"),
                save_path=save_path,
                extra_paths=extra_paths,
            )
        )

    entries.sort(key=lambda e: (e.system_label, e.name.lower()))
    return entries


def collect_cheats(delta_folder: Path) -> dict[str, list[dict[str, str]]]:
    """Group Delta's cheat records by the SHA-1 of the game they belong to.

    Cheats are not files on Delta's side; the code lives inside the record JSON,
    reachable via the record's game relationship.
    """
    by_game: dict[str, list[dict[str, str]]] = {}
    for record in harmony.iter_records(delta_folder):
        if record.type != "Cheat":
            continue
        game_id = record.related_identifier("game")
        if not game_id:
            continue
        by_game.setdefault(game_id, []).append(
            {
                "name": str(record.fields.get("name", "")),
                "code": str(record.fields.get("code", "")),
                "type": str(record.fields.get("type", "")),
            }
        )
    return by_game


def retroarch_save_dir(config_path: Path) -> tuple[Path, dict[str, str]]:
    """Resolve RetroArch's save directory from its config."""
    settings = discovery.parse_retroarch_config(config_path)
    save_dir = discovery.resolve_retroarch_dir(
        settings, "savefile_directory", config_path, "saves"
    )
    return save_dir, settings


def find_retroarch_save(save_dir: Path, entry: GameEntry) -> Path | None:
    """Look for an existing RetroArch save matching this game by name.

    Matching on the RetroArch side has to be by content name, because RetroArch
    names saves after the ROM file. This is best-effort and only used for
    reporting; the authoritative link on the Delta side is always the SHA-1.
    """
    if not save_dir.is_dir() or entry.system is None:
        return None
    # Match the sanitised name, since that is what the ROM on disk is called --
    # Delta's raw display name may contain characters Windows forbids.
    wanted = naming.save_filename(
        entry.name, entry.system.retroarch_save_ext
    ).lower()
    for path in save_dir.rglob(f"*.{entry.system.retroarch_save_ext}"):
        if path.name.lower() == wanted:
            return path
    return None


def _size(path: Path | None) -> str:
    if path is None:
        return "-"
    try:
        return f"{path.stat().st_size:,} B"
    except OSError:
        return "?"


def _override(path: Path | None, label: str) -> discovery.Discovery | None:
    """Turn a configured path into a Discovery, so config wins over guessing.

    A configured path that does not exist is reported as missing rather than
    silently falling back, since a wrong override is worth surfacing.
    """
    if path is None:
        return None
    if path.exists():
        return discovery.Discovery(label, path, "from config.toml")
    return discovery.Discovery(
        label, None, f"config.toml points at {path}, which does not exist"
    )


def run() -> int:
    """Print the inspection report. Returns a process exit code."""
    print("Delta / RetroArch sync :: inspection (read-only)\n")

    config = config_module.load()
    delta = _override(config.delta_folder, "Delta Emulator folder") or (
        discovery.find_delta_folder()
    )
    retro = _override(config.retroarch_config, "retroarch.cfg") or (
        discovery.find_retroarch_config()
    )

    for found in (delta, retro):
        status = "OK  " if found.found else "MISS"
        print(f"  [{status}] {found.label}: {found.path or found.detail}")
    print()

    if not delta.found or delta.path is None:
        print("Cannot continue without Delta's Dropbox folder.")
        return 1

    entries = collect_games(delta.path)
    cheats = collect_cheats(delta.path)

    save_dir: Path | None = None
    if retro.found and retro.path is not None:
        save_dir, settings = retroarch_save_dir(retro.path)
        sorted_by_core = discovery.truthy(settings, "sort_savefiles_enable")
        sorted_by_content = discovery.truthy(
            settings, "sort_savefiles_by_content_enable"
        )
        print(f"  RetroArch save directory: {save_dir}")
        print(f"    sort into folders by core:    {sorted_by_core}")
        print(f"    sort into folders by content: {sorted_by_content}")
        if sorted_by_core or sorted_by_content:
            print(
                "    NOTE: sorting is on, so saves live in subfolders. "
                "The sync must mirror that structure."
            )
        print()
        for entry in entries:
            entry.retroarch_save = find_retroarch_save(save_dir, entry)

    if not entries:
        print("No games found in Delta's folder. Has Delta finished a full sync?")
        return 1

    print(f"  {len(entries)} game(s) synced by Delta:\n")
    for entry in entries:
        flag = " " if entry.supported else "!"
        print(f"  {flag} {entry.name}  [{entry.system_label}]")
        print(f"      sha1:            {entry.identifier}")
        print(f"      ROM in Dropbox:  {_size(entry.rom_path)}")
        print(f"      save in Dropbox: {_size(entry.save_path)}")
        for file_id, path in entry.extra_paths.items():
            print(f"      {file_id}: {_size(path)}")
        if save_dir is not None:
            found_save = entry.retroarch_save
            label = found_save.name if found_save else "none found"
            print(f"      save in RetroArch: {label}")
        game_cheats = cheats.get(entry.identifier, [])
        if game_cheats:
            print(f"      cheats: {len(game_cheats)}")
            for cheat in game_cheats:
                print(f"        - {cheat['name']} ({cheat['type']})")
        if entry.system and not entry.supported:
            note = entry.system.conversion_note or "not yet enabled"
            print(f"      SKIPPED: {note}")
        print()

    unsupported = [e for e in entries if not e.supported]
    if unsupported:
        print(
            f"  {len(unsupported)} game(s) marked '!' are not cleared for sync yet. "
            "They are reported but will never be written."
        )
    return 0
