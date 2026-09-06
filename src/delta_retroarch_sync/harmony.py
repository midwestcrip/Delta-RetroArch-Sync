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

Record JSON shape (Harmony's ``LocalRecord.encode``):

    {
      "type": "Game",
      "identifier": "<sha1>",
      "record": {"name": ..., "filename": ..., "type": ...},
      "files": {"game": "<sha1 of file>", "artwork": "..."},
      "relationships": {"gameCollection": {"type": ..., "identifier": ...}}
    }

Note that Harmony also attaches ``gameID``/``gameName`` as Dropbox *property
groups*. Those are cloud-side metadata and are NOT mirrored to disk by the
desktop client, so we deliberately never depend on them.

This module is strictly read-only. Delta's docs warn that editing files in this
folder can cause data loss, and Harmony reconciles against Dropbox file
revisions, so writing here would desync Delta itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

#: Record files are "<Type>-<identifier>"; attached files add "-<fileID>".
_RECORD_RE = re.compile(r"^(?P<type>[A-Za-z]+)-(?P<identifier>[^-]+(?:-[^-]+)*)$")

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


def _unwrap(value: Any) -> Any:
    """Harmony encodes attributes through AnyCodable; some land as 1-key dicts."""
    if isinstance(value, dict) and len(value) == 1:
        (inner,) = value.values()
        if isinstance(inner, (str, int, float, bool)):
            return inner
    return value


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
        files=raw.get("files") if isinstance(raw.get("files"), dict) else {},
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
    """Return the on-disk path of one of a record's attached files, if present."""
    candidate = folder / f"{record.type}-{record.identifier}-{file_id}"
    return candidate if candidate.is_file() else None
