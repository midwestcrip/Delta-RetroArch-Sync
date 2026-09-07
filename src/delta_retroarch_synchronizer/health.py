"""Checks that a pushed save is in a state Delta can actually act on.

Pushing failed on 2026-09-05 in a way that produced no error anywhere: Delta
reported "sync complete", and simply did not apply the change. Every individual
piece looked right, and finding the problem meant listing the Dropbox folder by
hand and comparing revisions. That is the gap this closes.

Each check corresponds to a way the push has actually broken, not a hypothetical:

- The record's stored hash no longer matching its contents means Delta treats it
  as corrupt.
- The record's ``sha1`` disagreeing with the save file means Delta either
  ignores the push or downloads something that fails validation.
- The record naming a revision other than the one Dropbox holds is what made the
  first push attempt fail outright -- Delta asks for that exact revision.
- Local content that has not reached Dropbox yet means the desktop client is
  still syncing and nothing downstream can be trusted.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import dropbox_api, harmony
from .manifest import apple_timestamp_to_unix


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class DeltaActivity:
    """When Delta itself last wrote to the synced folder.

    Delta signed into the wrong Dropbox account is undetectable from inside the
    right one: account B leaves no trace in account A's folder, so there is
    nothing to look for. It happened on 2026-09-06 and cost an hour, with every
    desktop-side check passing because everything on the desktop genuinely was
    fine.

    What *is* observable is that Delta stops writing. That is weak on its own --
    not having played is equally consistent with it -- but it becomes sharp at
    one specific moment: a sync that finds nothing to do. "Already up to date"
    and "your phone has been writing somewhere else for a week" look identical
    to the user, and this is what tells them apart.

    Read from Delta's own ``modifiedDate`` rather than file timestamps, which
    are rewritten wholesale whenever Dropbox re-downloads the folder.
    """

    #: Unix timestamp of the newest Delta write, or None if nothing is dated.
    last_write: float | None
    #: Records seen, and how many carried a usable date. Game and
    #: GameCollection records have no modifiedDate; saves and cheats do.
    records: int
    dated: int

    def age_seconds(self, now: float) -> float | None:
        return None if self.last_write is None else max(0.0, now - self.last_write)


#: Past this, "Delta last wrote N ago" is worth questioning out loud. Half a day
#: covers an ordinary night's gap without prompting; it is a noise threshold for
#: the standalone report, not a judgement about whether anything is wrong. The
#: launcher ignores it, because there the message only appears when a sync
#: already found nothing to do, and then the age matters at any size.
SUSPICIOUS_SILENCE_SECONDS = 12 * 3600


def describe_age(seconds: float) -> str:
    """A duration a person would say out loud, not a precise one."""
    minutes = seconds / 60
    if minutes < 90:
        count, unit = round(minutes), "minute"
    elif minutes < 60 * 36:
        count, unit = round(minutes / 60), "hour"
    else:
        count, unit = round(minutes / 1440), "day"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def delta_activity(delta_folder: Path) -> DeltaActivity:
    """Find when Delta last wrote anything to its folder."""
    newest: float | None = None
    records = 0
    dated = 0

    for record in harmony.iter_records(delta_folder):
        records += 1
        raw = record.fields.get("modifiedDate")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        dated += 1
        when = apple_timestamp_to_unix(float(raw))
        if newest is None or when > newest:
            newest = when

    return DeltaActivity(last_write=newest, records=records, dated=dated)


def idle_sync_note(activity: DeltaActivity, now: float) -> str:
    """What to say when a sync found nothing to do.

    Phrased as a fact plus a question the user can answer and we cannot: only
    they know whether they have played since then.
    """
    age = activity.age_seconds(now)
    if age is None:
        return (
            "Delta has not dated anything in this folder, so there is no way to "
            "tell when it last synced."
        )
    return (
        f"Delta last wrote {describe_age(age)} ago. If you have played since "
        "then, check Settings -> Delta Sync on your phone: Delta may be signed "
        "into a different Dropbox account, or its sync may be off."
    )


@dataclass
class Health:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def problems(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]


def clock_check(
    delta_clock: Path | None,
    retroarch_clock: Path | None,
    *,
    now: float,
    core_note: str = "",
) -> "Check | None":
    """Whether both sides count the cartridge clock from the same instant.

    This is the one thing about the clock that can actually be wrong and stay
    invisible. The clock is never stored as a value -- Gambatte derives it as
    ``now - baseTime`` -- so two machines agree if and only if they hold the
    same base. If they drift apart, the in-game date differs between phone and
    desktop and nothing else would say so.

    Returns None for a game with no clock at all, which is every system but
    Game Boy Color.
    """
    from . import clock as clock_module

    if delta_clock is None or not delta_clock.is_file():
        return None
    if core_note:
        # A core whose format has not been verified gets no clock written, so
        # the two sides are *expected* to differ. Not a fault.
        return Check("clock", True, core_note)

    try:
        delta_base = clock_module.delta_timestamp(delta_clock.read_bytes())
    except (OSError, ValueError) as error:
        return Check("clock", False, f"Delta's clock is unreadable: {error}")

    reading = clock_module.describe_cartridge_clock(delta_base, now)
    since = clock_module.describe(delta_base)

    if retroarch_clock is None or not retroarch_clock.is_file():
        return Check(
            "clock",
            True,
            f"reads {reading}, counting from {since}; RetroArch has no clock "
            "yet (it is written with the save)",
        )

    try:
        retro_base = clock_module.retroarch_timestamp(retroarch_clock.read_bytes())
    except (OSError, ValueError) as error:
        return Check("clock", False, f"RetroArch's clock is unreadable: {error}")

    if delta_base == retro_base:
        return Check("clock", True, f"agrees on both sides: reads {reading}")

    drift = abs(delta_base - retro_base)
    return Check(
        "clock",
        False,
        f"the two sides count from different instants — Delta from {since}, "
        f"RetroArch from {clock_module.describe(retro_base)} "
        f"({describe_age(drift)} apart). The in-game date will differ between "
        "them; syncing the save will bring the clock with it.",
    )


def check_game(
    delta_folder: Path,
    identifier: str,
    dropbox: "dropbox_api.DropboxClient | None" = None,
    *,
    retroarch_clock: Path | None = None,
    clock_core_note: str = "",
    now: float | None = None,
) -> Health:
    """Inspect one game's record and save for the failures seen in practice."""
    from .delta_writer import verify_record_hash

    checks: list[Check] = []

    record_path = harmony.resolve_existing(delta_folder, f"GameSave-{identifier}")
    save_path = harmony.resolve_existing(
        delta_folder, f"GameSave-{identifier}-gameSave"
    )

    if not record_path.is_file():
        checks.append(
            Check(
                "record present",
                False,
                f"{record_path.name} is missing. Delta will recreate it on its "
                "next sync; if it does not, the game may have been removed.",
            )
        )
        return Health(checks)
    checks.append(Check("record present", True, record_path.name))

    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        checks.append(Check("record parses", False, str(error)))
        return Health(checks)

    # Not a pass/fail condition, and reporting it as one was wrong.
    #
    # The record's top-level hash is stored twice: in the JSON, and in the
    # Dropbox property group that only Delta's own app can write. A push
    # preserves the JSON copy precisely so the two stay equal, which means the
    # value deliberately stops describing the record's contents. Failing on that
    # would flag every successfully pushed save as corrupt.
    #
    # What it does still tell us is who wrote the record last, which is useful
    # when reading the rest of this report.
    written_by_delta = verify_record_hash(raw)
    checks.append(
        Check(
            "record hash",
            True,
            "as Delta last wrote it" if written_by_delta
            else "preserved from Delta's last write, as a push leaves it "
                 "(expected -- see README, Pushing back to Delta)",
        )
    )

    if not save_path.is_file():
        checks.append(Check("save present", False, f"{save_path.name} is missing"))
        return Health(checks)

    from .delta_writer import recently_written

    file_hash = hashlib.sha1(save_path.read_bytes()).hexdigest()
    claimed = str(raw.get("record", {}).get("sha1", ""))
    agrees = claimed == file_hash
    # A record and its save are downloaded as two independent files and do not
    # land together, so a disagreement between two freshly-written files is
    # Dropbox mid-flight rather than damage. Reported as passing, because it is
    # not a state anyone needs to act on -- it resolves itself.
    settling = not agrees and recently_written([record_path, save_path])
    if settling:
        detail = (
            "still arriving from Dropbox -- the record and the save were written "
            "moments apart and do not agree yet. Resolves itself."
        )
    elif agrees:
        detail = "consistent"
    else:
        detail = f"record says {claimed[:12]}, file is {file_hash[:12]}"
    checks.append(
        Check("record points at the save on disk", agrees or settling, detail)
    )

    if dropbox is None:
        checks.append(
            Check(
                "Dropbox revision",
                True,
                "not checked (authorise Dropbox to include this)",
            )
        )
        return Health(checks)

    folder = delta_folder.name
    for label, path in (("record", record_path), ("save", save_path)):
        try:
            meta = dropbox.get_metadata(f"/{folder}/{path.name}")
        except dropbox_api.DropboxError as error:
            checks.append(Check(f"{label} on Dropbox", False, str(error)[:120]))
            continue

        uploaded = dropbox_api.content_hash(path) == meta.get("content_hash")
        checks.append(
            Check(
                f"{label} uploaded to Dropbox",
                uploaded,
                "in sync" if uploaded
                else "the desktop client has not finished uploading this yet",
            )
        )

        if label == "save":
            files = raw.get("files")
            named = ""
            if isinstance(files, list):
                for entry in files:
                    if isinstance(entry, dict) and entry.get("identifier") == "gameSave":
                        named = str(entry.get("versionIdentifier", ""))
            actual = str(meta.get("rev", ""))
            matches = named == actual
            checks.append(
                Check(
                    "record names the current revision",
                    matches,
                    "matches" if matches
                    else f"record says {named}, Dropbox holds {actual} -- "
                    "Delta would download the wrong version",
                )
            )

    delta_clock = harmony.resolve_existing(
        delta_folder, f"GameSave-{identifier}-gameTimeSave"
    )
    clock_result = clock_check(
        delta_clock if delta_clock.is_file() else None,
        retroarch_clock,
        now=time.time() if now is None else now,
        core_note=clock_core_note,
    )
    if clock_result is not None:
        checks.append(clock_result)

    return Health(checks)
