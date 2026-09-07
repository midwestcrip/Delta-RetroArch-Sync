"""The last known-good state both sides agreed on.

This is what makes conflict detection possible. Without it the only options are
"newest timestamp wins" (which loses a session's progress whenever clocks or
sync lag disagree) and "always push" (which overwrites whichever side you did
not play on last). Neither is acceptable when the thing being overwritten is a
save file.

With a manifest, each side is compared against the last state we *know* both
agreed on, and the four cases separate cleanly:

    neither changed  -> nothing to do
    only Delta       -> pull
    only RetroArch   -> push
    both changed     -> genuine conflict, refuse and report

The fourth case is the entire point. Silently resolving it is how progress
disappears, so it is never resolved automatically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.json"

#: Seconds between the Unix epoch and Apple's 2001-01-01 reference date, used
#: by Delta's Core Data `modifiedDate`.
APPLE_EPOCH_OFFSET = 978307200.0


def apple_timestamp_to_unix(value: float) -> float:
    """Convert a Core Data reference-date timestamp to a Unix timestamp."""
    return value + APPLE_EPOCH_OFFSET


def sha1_of(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """SHA-1 of a file's contents, read in chunks so a 16MB ROM is not slurped."""
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class FileState:
    """What a file looked like at the last successful sync."""

    sha1: str
    size: int

    @classmethod
    def of(cls, path: Path) -> "FileState":
        return cls(sha1=sha1_of(path), size=path.stat().st_size)

    def matches(self, path: Path) -> bool:
        """True if the file on disk is unchanged from this recorded state.

        Size is checked first because it is free and rules out most changes; the
        hash is what actually decides. Timestamps are deliberately not used --
        Dropbox rewrites mtimes on sync, so an mtime comparison reports changes
        that never happened.
        """
        try:
            if path.stat().st_size != self.size:
                return False
        except OSError:
            return False
        return sha1_of(path) == self.sha1


@dataclass
class Entry:
    """The agreed state of one game's save on both sides."""

    delta: FileState | None = None
    retroarch: FileState | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "delta": asdict(self.delta) if self.delta else None,
            "retroarch": asdict(self.retroarch) if self.retroarch else None,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "Entry":
        if not isinstance(raw, dict):
            return cls()

        def state(value: Any) -> FileState | None:
            if isinstance(value, dict) and "sha1" in value and "size" in value:
                return FileState(sha1=str(value["sha1"]), size=int(value["size"]))
            return None

        return cls(delta=state(raw.get("delta")), retroarch=state(raw.get("retroarch")))


class Manifest:
    """Keyed by game SHA-1, which is stable across both sides and all renames."""

    def __init__(
        self,
        path: Path,
        entries: dict[str, Entry] | None = None,
        cheats: dict[str, str] | None = None,
        notices: set[str] | None = None,
    ) -> None:
        self.path = path
        self.entries: dict[str, Entry] = entries or {}
        #: One-time notices already shown, so they are not repeated every sync.
        self.notices: set[str] = notices or set()
        #: Last agreed cheat code per cheat UUID, canonicalised to bare hex
        #: digits. Cheats are not files on Delta's side -- the code lives inside
        #: the record JSON -- so they cannot use ``FileState`` and get their own
        #: section rather than a strained fit into the games one.
        self.cheats: dict[str, str] = cheats or {}

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Load the manifest. A missing or corrupt file means 'no agreed state'.

        Treating corruption as empty is deliberate and safe: every file then
        looks changed on both sides, which surfaces as a conflict to resolve
        rather than as a silent overwrite in either direction.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return cls(path)
        games = raw.get("games") if isinstance(raw, dict) else None
        if not isinstance(games, dict):
            return cls(path)
        # A manifest written before cheats were tracked simply has no section,
        # which reads as "no agreed state" for every cheat -- and that is handled
        # safely, because two sides that already agree need no history to say so.
        stored_cheats = raw.get("cheats") if isinstance(raw, dict) else None
        cheats = (
            {str(k): str(v) for k, v in stored_cheats.items()}
            if isinstance(stored_cheats, dict)
            else {}
        )
        stored_notices = raw.get("notices") if isinstance(raw, dict) else None
        notices = (
            {str(v) for v in stored_notices} if isinstance(stored_notices, list) else set()
        )
        return cls(
            path,
            {str(k): Entry.from_json(v) for k, v in games.items()},
            cheats,
            notices,
        )

    def save(self) -> None:
        """Write atomically, so an interrupted write cannot corrupt the record."""
        payload = {
            "version": 1,
            "games": {key: entry.to_json() for key, entry in self.entries.items()},
            "cheats": dict(self.cheats),
            "notices": sorted(self.notices),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(self.path)

    def get(self, identifier: str) -> Entry:
        return self.entries.get(identifier, Entry())

    def record(
        self, identifier: str, delta: Path | None, retroarch: Path | None
    ) -> None:
        """Record both sides as agreed, after a successful copy."""
        self.entries[identifier] = Entry(
            delta=FileState.of(delta) if delta and delta.is_file() else None,
            retroarch=(
                FileState.of(retroarch) if retroarch and retroarch.is_file() else None
            ),
        )

    def cheat_code(self, identifier: str) -> str | None:
        """The last agreed code for one cheat, or None if there is no history."""
        return self.cheats.get(identifier)

    def record_cheat(self, identifier: str, canonical: str) -> None:
        """Record a cheat as agreed on both sides."""
        self.cheats[identifier] = canonical

    def already_said(self, key: str) -> bool:
        """Whether a one-time notice has been shown before.

        Some things are worth saying once and insufferable every time -- that a
        game's Controller Pak data does not sync, for instance, which is true
        permanently and changes nothing about the sync. A message repeated on
        every run is one people learn to scroll past, and the log has already
        lost a real confirmation that way once.
        """
        return key in self.notices

    def record_said(self, key: str) -> None:
        self.notices.add(key)
