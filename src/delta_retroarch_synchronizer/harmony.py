"""Reader for Delta's Harmony sync folder as mirrored by the Dropbox client.

Delta syncs through Harmony (github.com/rileytestut/Harmony), which writes a
single flat folder -- no per-game subdirectories. Two kinds of entry live in it:

    Game-<sha1>                   JSON record describing a game
    Game-<sha1>-game              the ROM itself, no extension
    Game-<sha1>-artwork           box art
    GameSave-<sha1>               JSON record describing a battery save
    GameSave-<sha1>-gameSave      the battery save itself
    GameSave-<sha1>-gameTimeSave  GBC only: the .rtc clock file
    Cheat-<uuid>                  JSON record; the code is *inside* the JSON

``<sha1>`` is the SHA-1 of the original ROM file -- Delta computes it on import
(``RSTHasher.sha1HashOfFile``) and uses it as ``Game.identifier``. ``GameSave``
records reuse that same identifier, which is what lets us match a Delta save to
a local ROM without any filename guessing.

Record JSON shape, as observed on disk:

    {
      "type": "Game",
      "identifier": "<sha1>",
      "sha1Hash": "<hash of the record itself, not the ROM>",
      "record": {
        "name": "Pokemon: Fire Red Version",
        "filename": "<sha1>.gba",
        "type": "<base64 NSKeyedArchiver plist>",
        "isFavorite": false
      },
      "files": [
        {"identifier": "game", "sha1Hash": "<sha1>", "size": 16777216,
         "remoteIdentifier": "/delta emulator/game-<sha1>-game", ...}
      ],
      "relationships": {"gameCollection": {"type": ..., "identifier": ...}}
    }

Two shapes here differ from what Harmony's encoder suggests in isolation, and
both were corrected against real data rather than assumed:

- ``files`` is a *list* of file objects, not an ``{identifier: sha1}`` map.
  The encoder has both branches; records with uploaded files use the list.
- Core Data attributes that are not JSON-native -- ``type`` (a GameType) and
  ``artworkURL`` (an NSURL) -- are NSKeyedArchiver plists in base64, not the
  plain strings they appear to be. See ``_unwrap``.

``remoteIdentifier`` is Dropbox's lowercased path, so it must not be used to
build a local filename; the on-disk name preserves the record's original case.

Note that Harmony also attaches ``gameID``/``gameName`` as Dropbox *property
groups*. Those are cloud-side metadata and are NOT mirrored to disk by the
desktop client, so we deliberately never depend on them.

This module is strictly read-only. Delta's docs warn that editing files in this
folder can cause data loss, and Harmony reconciles against Dropbox file
revisions, so writing here would desync Delta itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

#: Reserved Game identifiers Delta uses for melonDS BIOS/firmware bundles.
#: These are not real games and must be skipped.
BIOS_IDENTIFIERS = frozenset(
    {
        "com.rileytestut.MelonDSDeltaCore.BIOS",
        "com.rileytestut.MelonDSDeltaCore.DSiBIOS",
    }
)


@dataclass
class HarmonyRecord:
    """One parsed ``<Type>-<identifier>`` JSON record."""

    path: Path
    type: str
    identifier: str
    fields: dict[str, Any]
    files: dict[str, Any]
    relationships: dict[str, Any]

    @property
    def name(self) -> str | None:
        value = self.fields.get("name")
        return value if isinstance(value, str) else None

    def related_identifier(self, key: str) -> str | None:
        rel = self.relationships.get(key)
        if isinstance(rel, dict):
            value = rel.get("identifier")
            return value if isinstance(value, str) else None
        return None


def _resolve_archived(plist: dict[str, Any]) -> str | None:
    """Pull the meaningful string out of a decoded NSKeyedArchiver plist.

    The archive is a flat ``$objects`` table addressed by UID, with ``$top.root``
    naming the entry point. A GameType archives to a bare string; an artworkURL
    archives to an NSURL whose ``NS.relative`` points at the string.
    """
    objects = plist.get("$objects")
    if not isinstance(objects, list):
        return None

    def at(index: Any) -> Any:
        # plistlib represents UIDs as plistlib.UID with a .data attribute.
        idx = getattr(index, "data", index)
        return objects[idx] if isinstance(idx, int) and 0 <= idx < len(objects) else None

    top = plist.get("$top")
    root = at(top.get("root")) if isinstance(top, dict) else None

    if isinstance(root, str):
        return root
    if isinstance(root, dict):
        for key in ("NS.relative", "NS.string"):
            resolved = at(root.get(key))
            if isinstance(resolved, str):
                return resolved

    # Fall back to the first real string in the table; "$null" is always [0].
    for entry in objects:
        if isinstance(entry, str) and entry != "$null":
            return entry
    return None


def _unwrap(value: Any) -> Any:
    """Normalise one attribute value out of a record's ``record`` dict.

    Core Data attributes that are not JSON-native -- a GameType, an NSURL --
    are archived with NSKeyedArchiver and stored as base64. Delta's `type`
    field is one of these, so it arrives as a base64 blob rather than the
    "com.rileytestut.delta.game.gba" string it looks like it should be.
    """
    if isinstance(value, str) and len(value) > 24:
        try:
            blob = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            blob = b""
        if blob.startswith(b"bplist"):
            try:
                decoded = _resolve_archived(plistlib.loads(blob))
            except (plistlib.InvalidFileException, ValueError, TypeError, IndexError):
                decoded = None
            if decoded is not None:
                return decoded

    if isinstance(value, dict) and len(value) == 1:
        (inner,) = value.values()
        if isinstance(inner, (str, int, float, bool)):
            return inner
    return value


def _normalise_files(value: Any) -> dict[str, Any]:
    """Normalise a record's ``files`` into ``{identifier: metadata}``.

    Harmony's encoder has two branches: a metadata-only record writes a plain
    ``{identifier: sha1}`` map, while a record with uploaded files writes a list
    of file objects. Real records on disk use the list form, so accept both and
    key everything by file identifier.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        files: dict[str, Any] = {}
        for entry in value:
            if isinstance(entry, dict) and isinstance(entry.get("identifier"), str):
                files[entry["identifier"]] = entry
        return files
    return {}


def parse_record(path: Path) -> HarmonyRecord | None:
    """Parse one record file, or return None if it is not a Harmony record.

    Attached data files (the ROM, the save) sit in the same folder and are not
    JSON, so a decode failure here is expected and not an error.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or "type" not in raw or "identifier" not in raw:
        return None

    fields = raw.get("record")
    fields = {k: _unwrap(v) for k, v in fields.items()} if isinstance(fields, dict) else {}

    return HarmonyRecord(
        path=path,
        type=str(raw["type"]),
        identifier=str(raw["identifier"]),
        fields=fields,
        files=_normalise_files(raw.get("files")),
        relationships=(
            raw.get("relationships")
            if isinstance(raw.get("relationships"), dict)
            else {}
        ),
    )


def iter_records(folder: Path) -> Iterator[HarmonyRecord]:
    """Yield every parseable Harmony record in the folder, ignoring data files."""
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        # Attached files always carry a trailing "-<fileIdentifier>" that a bare
        # record name never has, but names are ambiguous enough that we just try
        # to parse everything and let non-JSON fall out.
        record = parse_record(path)
        if record is not None:
            yield record


def attached_file(folder: Path, record: HarmonyRecord, file_id: str) -> Path | None:
    """Return the on-disk path of one of a record's attached files, if present.

    The capitalisation here is nominal. Delta re-uploads a record to
    ``remoteRecord.identifier``, which Harmony took from Dropbox's ``pathLower``
    -- so a record Delta has re-uploaded is named ``gamesave-<sha1>`` on disk,
    not ``GameSave-<sha1>``. This resolves anyway because the tool is
    Windows-only and NTFS is case-insensitive; on a case-sensitive filesystem it
    would need a case-folded index of the folder.
    """
    candidate = folder / f"{record.type}-{record.identifier}-{file_id}"
    return candidate if candidate.is_file() else None


def resolve_existing(folder: Path, name: str) -> Path:
    """Return the on-disk path for ``name``, preserving its real capitalisation.

    Delta re-uploads a record to ``remoteRecord.identifier``, which Harmony took
    from Dropbox's ``pathLower`` -- so a record Delta has touched is named
    ``gamesave-<sha1>`` on disk, not ``GameSave-<sha1>``.

    Reading is unaffected (NTFS is case-insensitive), but *writing* is not:
    ``os.replace`` renames the target to whatever spelling it is given, so
    writing to the constructed name silently renames Delta's file. That is a
    real change to Dropbox, and it is not the kind of change we want to be
    making to another app's storage. Always write through this.
    """
    lowered = name.lower()
    try:
        for entry in folder.iterdir():
            if entry.name.lower() == lowered:
                return entry
    except OSError:
        pass
    return folder / name
