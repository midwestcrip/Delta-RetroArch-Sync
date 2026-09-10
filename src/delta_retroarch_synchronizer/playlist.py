"""Put copied ROMs into a RetroArch playlist, so they appear on its menu.

The release notes promise that "a game you added on your phone just appears on
the desktop ready to play". Until now it appeared in a *folder*: RetroArch shows
content from playlists, and copying a file into the ROM directory does not
create one. On a fresh install there are no playlists at all, so the main menu
stays empty and the game has to be found through Load Content -- which is
exactly what happened on 2026-09-06 with Super Mario World sitting correctly on
disk and nowhere to be seen in RetroArch.

A playlist is a ``.lpl`` file: JSON, one per system, named after the system's
libretro-database name, which ``systems.py`` already records as
``retroarch_db_name``.

Two rules govern everything here:

- **Never destroy a playlist.** These are the user's collections, likely built
  by scanning and possibly hand-curated. Entries are merged into whatever is
  already there, other entries are untouched, and unknown top-level keys are
  preserved so a newer RetroArch's settings survive a round trip.
- **Be idempotent.** Sync runs on every launch. Registering a game already in
  the playlist must change nothing at all, or every sync rewrites the file and
  churns the user's collection.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

#: The format version RetroArch writes. It reads older files and rewrites them
#: in its own format, so this only has to be something it accepts.
PLAYLIST_VERSION = "1.5"

#: RetroArch resolves the core itself when an entry says DETECT, prompting only
#: if several installed cores claim the system. Naming a specific core library
#: here would be a small convenience that goes stale the moment a core is
#: updated or removed, leaving an entry that cannot launch.
DETECT = "DETECT"


def playlist_path(playlist_dir: Path, db_name: str) -> Path:
    return playlist_dir / f"{db_name}.lpl"


def _empty_playlist() -> dict[str, Any]:
    return {
        "version": PLAYLIST_VERSION,
        "default_core_path": "",
        "default_core_name": "",
        "label_display_mode": 0,
        "right_thumbnail_mode": 0,
        "left_thumbnail_mode": 0,
        "thumbnail_match_mode": 0,
        "sort_mode": 0,
        "items": [],
    }


def load(path: Path) -> dict[str, Any]:
    """Read a playlist, or return an empty one.

    A file that will not parse is treated as absent for reading, but the caller
    still must not overwrite it -- see ``register``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty_playlist()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty_playlist()
    return data


def crc32_of(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """RetroArch's ``<uppercase hex>|crc`` form, read in chunks for large ROMs."""
    checksum = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            checksum = zlib.crc32(chunk, checksum)
    return f"{checksum & 0xFFFFFFFF:08X}|crc"


def make_entry(rom_path: Path, label: str, db_name: str) -> dict[str, Any]:
    return {
        "path": str(rom_path),
        "label": label,
        "core_path": DETECT,
        "core_name": DETECT,
        "crc32": crc32_of(rom_path),
        "db_name": f"{db_name}.lpl",
    }


def _same_path(left: str, right: str) -> bool:
    """Windows paths differ in case and separator without differing in meaning."""
    return left.replace("/", "\\").casefold() == right.replace("/", "\\").casefold()


def contains(data: dict[str, Any], rom_path: Path) -> bool:
    wanted = str(rom_path)
    return any(
        isinstance(item, dict) and _same_path(str(item.get("path", "")), wanted)
        for item in data.get("items", [])
    )


def register(
    playlist_dir: Path, db_name: str, rom_path: Path, label: str
) -> Path | None:
    """Add one ROM to its system's playlist. Returns the file if it changed.

    None means nothing needed doing -- either the entry was already there, or
    the playlist file exists but could not be parsed, in which case it is left
    strictly alone rather than replaced with a fresh one. Silently discarding
    someone's collection to fix a menu entry is not a trade worth making.
    """
    if not rom_path.is_file():
        return None

    path = playlist_path(playlist_dir, db_name)

    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            return None
        data = raw
    else:
        data = _empty_playlist()

    if contains(data, rom_path):
        return None

    data["items"].append(make_entry(rom_path, label, db_name))

    playlist_dir.mkdir(parents=True, exist_ok=True)

    # Staged and renamed, like every other write here. This function's whole
    # promise is that it never destroys a playlist, and it kept that promise
    # against a *parse* failure while breaking it against a crash: a direct
    # write truncates the file first, so losing power partway through replaces
    # someone's entire collection with half a line of JSON. The rename is
    # atomic on the same filesystem, so the playlist is either the old one or
    # the new one and never a fragment.
    staged = path.with_name(path.name + ".partial")
    try:
        staged.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        staged.replace(path)
    finally:
        if staged.exists():
            try:
                staged.unlink()
            except OSError:
                pass
    return path
