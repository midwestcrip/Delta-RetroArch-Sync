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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.json"

#: Narrows a file's bytes to the part that is the save, for emulators that
#: append their own bookkeeping. Supplied by the caller -- what counts as the
#: save is the emulator's business, and this module deliberately knows nothing
#: about any particular one.
Body = Callable[[bytes], bytes]

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
    def of(cls, path: Path, *, body: "Body | None" = None) -> "FileState":
        if body is not None:
            return cls.of_bytes(body(path.read_bytes()))
        return cls(sha1=sha1_of(path), size=path.stat().st_size)

    @classmethod
    def of_bytes(cls, data: bytes) -> "FileState":
        return cls(sha1=hashlib.sha1(data).hexdigest(), size=len(data))

    def matches(self, path: Path, *, body: "Body | None" = None) -> bool:
        """True if the file on disk is unchanged from this recorded state.

        Size is checked first because it is free and rules out most changes; the
        hash is what actually decides. Timestamps are deliberately not used --
        Dropbox rewrites mtimes on sync, so an mtime comparison reports changes
        that never happened.

        ``body`` narrows all of that to the part of the file that is the save.
        Some emulators append their own bookkeeping -- mGBA writes a clock block
        after a Game Boy save and restamps it on every launch -- and hashing
        that would call opening a game "the player made progress". It has to be
        passed to ``of`` and ``matches`` alike, or a state recorded one way is
        compared the other and never matches.

        **A state recorded before there was a ``body`` still counts.** Every
        manifest already on disk fingerprints the whole file, so introducing a
        narrowed view would otherwise declare every one of those files changed
        on the first run after an upgrade -- and for an N64 game where Delta had
        genuinely moved, "Delta changed" plus that false "RetroArch changed" is
        a **conflict reported in place of a perfectly ordinary pull**. So the
        whole file is tried as a fallback.

        That widening cannot produce a wrong answer: identical bytes narrow to
        identical bytes, so "the whole file is unchanged" already implies "the
        save inside it is unchanged". It can only ever forgive, never miss a
        real change. The stale shape is then rewritten the next time this
        target is recorded.
        """
        try:
            if body is None and path.stat().st_size != self.size:
                return False
        except OSError:
            return False
        try:
            if FileState.of(path, body=body) == self:
                return True
            return body is not None and FileState.of(path) == self
        except OSError:
            return False


@dataclass
class CheatState:
    """What a cheat looked like when both sides last agreed.

    The name is remembered as well as the code because it is the only thing
    linking a `.cht` entry back to a Delta cheat -- a `.cht` carries no UUID. So
    a rename has to be detectable, or the cheat silently unlinks and reappears
    as an uncreatable RetroArch-only one.
    """

    code: str
    name: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "CheatState":
        # Manifests written before renaming was supported stored a bare code
        # string. Read as a code with no remembered name, which is exactly what
        # it is -- the first sync after upgrading then fills the name in.
        if isinstance(raw, str):
            return cls(code=raw)
        if isinstance(raw, dict):
            return cls(code=str(raw.get("code", "")), name=str(raw.get("name", "")))
        return cls(code="")


#: The manifest's name for RetroArch, which has its own field rather than a slot
#: in ``targets`` because it was here first and its shape is already on disk in
#: every existing manifest.
RETROARCH = "retroarch"


@dataclass
class Agreement:
    """What both files looked like when one desktop target last agreed with Delta.

    A pair, never two separate facts. "Delta has not changed" is only meaningful
    *relative to a particular target*: the question a sync asks is "has Delta
    moved since this emulator last saw it", and two emulators synced at
    different moments have different answers.
    """

    delta: FileState | None = None
    desktop: FileState | None = None

    @property
    def empty(self) -> bool:
        return self.delta is None and self.desktop is None


@dataclass
class Entry:
    """The agreed state of one game's save on Delta and on each desktop target.

    There can be more than one desktop side. Someone syncing the same game to
    RetroArch and to standalone mGBA has two independent agreements to keep, and
    collapsing them into one field would make each sync look like a change the
    other side had made -- every run reporting a conflict that is really just the
    other emulator's copy.

    **Both halves are per target, not only the desktop one.** Sharing the Delta
    half looks harmless, because there is only one Delta -- and it silently
    starves every target but the first. RetroArch pulls a new save from the
    phone and records that Delta is now agreed; mGBA is reconciled a moment
    later, sees a Delta that "has not changed", and is left holding the old save
    with the sync reporting "unchanged on both sides". Whichever target ran
    first would quietly decide that none of the others needed the update.
    """

    delta: FileState | None = None
    retroarch: FileState | None = None
    #: Agreed pairs per standalone emulator, keyed by ``Emulator.key``.
    #: RetroArch is not in here: its pair is the two fields above, which is the
    #: shape already written in every existing manifest.
    targets: dict[str, Agreement] = field(default_factory=dict)

    def agreement(self, target: str = RETROARCH) -> Agreement:
        """Both halves of one target's agreement, as of its last sync."""
        if target == RETROARCH:
            return Agreement(delta=self.delta, desktop=self.retroarch)
        return self.targets.get(target, Agreement())

    def desktop(self, target: str = RETROARCH) -> FileState | None:
        """The agreed desktop state for one target."""
        return self.agreement(target).desktop

    def with_agreement(self, target: str, agreed: Agreement) -> "Entry":
        """A copy with one target's pair replaced and every other one untouched."""
        if target == RETROARCH:
            return Entry(
                delta=agreed.delta,
                retroarch=agreed.desktop,
                targets=dict(self.targets),
            )
        targets = dict(self.targets)
        if agreed.empty:
            targets.pop(target, None)
        else:
            targets[target] = agreed
        return Entry(delta=self.delta, retroarch=self.retroarch, targets=targets)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "delta": asdict(self.delta) if self.delta else None,
            "retroarch": asdict(self.retroarch) if self.retroarch else None,
        }
        # Omitted entirely when empty, so a manifest from a machine that only
        # uses RetroArch keeps the shape it has always had.
        if self.targets:
            payload["targets"] = {
                key: {
                    "delta": asdict(agreed.delta) if agreed.delta else None,
                    "desktop": asdict(agreed.desktop) if agreed.desktop else None,
                }
                for key, agreed in sorted(self.targets.items())
            }
        return payload

    @classmethod
    def from_json(cls, raw: Any) -> "Entry":
        if not isinstance(raw, dict):
            return cls()

        def state(value: Any) -> FileState | None:
            if isinstance(value, dict) and "sha1" in value and "size" in value:
                return FileState(sha1=str(value["sha1"]), size=int(value["size"]))
            return None

        def agreement(value: Any) -> Agreement:
            # A bare FileState is how per-target state was written before it was
            # clear the Delta half has to be per target too. Read as "we know
            # what the emulator had, not what Delta looked like at the time",
            # which is exactly true and makes the next sync re-evaluate rather
            # than trust a pairing that was never recorded.
            bare = state(value)
            if bare is not None:
                return Agreement(delta=None, desktop=bare)
            if isinstance(value, dict):
                return Agreement(
                    delta=state(value.get("delta")),
                    desktop=state(value.get("desktop")),
                )
            return Agreement()

        stored = raw.get("targets")
        targets: dict[str, Agreement] = {}
        if isinstance(stored, dict):
            for key, value in stored.items():
                parsed = agreement(value)
                if not parsed.empty:
                    targets[str(key)] = parsed

        return cls(
            delta=state(raw.get("delta")),
            retroarch=state(raw.get("retroarch")),
            targets=targets,
        )


class Manifest:
    """Keyed by game SHA-1, which is stable across both sides and all renames."""

    def __init__(
        self,
        path: Path,
        entries: dict[str, Entry] | None = None,
        cheats: dict[str, CheatState] | None = None,
        notices: set[str] | None = None,
    ) -> None:
        self.path = path
        self.entries: dict[str, Entry] = entries or {}
        #: One-time notices already shown, so they are not repeated every sync.
        self.notices: set[str] = notices or set()
        #: Last agreed code and name per cheat UUID. Cheats are not files on
        #: Delta's side -- the code lives inside the record JSON -- so they
        #: cannot use ``FileState`` and get their own section rather than a
        #: strained fit into the games one.
        self.cheats: dict[str, CheatState] = cheats or {}

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
            {str(k): CheatState.from_json(v) for k, v in stored_cheats.items()}
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
            "cheats": {key: asdict(state) for key, state in self.cheats.items()},
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
        self,
        identifier: str,
        delta: Path | None,
        desktop: Path | None,
        target: str = RETROARCH,
        desktop_body: "Body | None" = None,
    ) -> None:
        """Record Delta and one desktop target as agreed, after a copy.

        Merged into whatever is already stored rather than replacing it, so
        recording a sync to one emulator does not erase the agreement with
        another -- which would leave the next run with no history for it and
        report a conflict.

        Only this target's pair is touched, *including its Delta half*. Writing
        the Delta half globally is what made the second target go stale: it said
        "Delta is agreed" on behalf of emulators that had not seen the new save.

        ``desktop_body`` applies only to the desktop half, because only that
        side has an emulator appending to it. Delta's own copy is always the
        save and nothing else, which is the reason a trailer has to be dropped
        on the way home rather than recorded and forgiven.
        """
        self.entries[identifier] = self.entries.get(identifier, Entry()).with_agreement(
            target,
            Agreement(
                delta=FileState.of(delta) if delta and delta.is_file() else None,
                desktop=(
                    FileState.of(desktop, body=desktop_body)
                    if desktop and desktop.is_file()
                    else None
                ),
            ),
        )

    def record_states(
        self,
        identifier: str,
        delta: "FileState | None",
        desktop: "FileState | None",
        target: str = RETROARCH,
    ) -> None:
        """Record an agreement the caller has already measured.

        :meth:`record` reads the two files itself, which is right after a copy:
        what was just written is what should be agreed. It is wrong when the
        agreement is being written for files *nobody wrote* -- there, the bytes
        that were examined and the bytes on disk a moment later are not
        necessarily the same, and re-reading would agree to whichever arrived
        last rather than to the pair that was actually checked.
        """
        self.entries[identifier] = self.entries.get(identifier, Entry()).with_agreement(
            target, Agreement(delta=delta, desktop=desktop)
        )

    def cheat_code(self, identifier: str) -> str | None:
        """The last agreed code for one cheat, or None if there is no history."""
        state = self.cheats.get(identifier)
        return state.code if state is not None else None

    def cheat_name(self, identifier: str) -> str | None:
        """The last agreed name, or None if this cheat predates name tracking.

        None and "" mean different things here. None is "we never knew", which
        is the case for a manifest written before renaming was supported, and
        means a name difference cannot be attributed to either side.
        """
        state = self.cheats.get(identifier)
        if state is None or not state.name:
            return None
        return state.name

    def record_cheat(self, identifier: str, canonical: str, name: str = "") -> None:
        """Record a cheat as agreed on both sides."""
        self.cheats[identifier] = CheatState(code=canonical, name=name)

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
