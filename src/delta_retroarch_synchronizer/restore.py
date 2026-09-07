"""Reading the rolling backups, and putting one back.

Every write this tool makes copies the previous version aside first -- saves on
both sides, clock files, Delta's records, and now cheat records. That has been
true since the beginning and has never been reachable: the folder is a flat list
of names like ``GameSave-dd5945db...-gameSave.20260906T210845793346.bak``, which
is not something anyone is going to read under pressure.

This turns that pile into a list of restore points and puts one back.

Two things shape the design:

- **Restoring is itself a write**, so it takes the same care every other write
  does: the current file is backed up before it is replaced, which makes a
  restore undoable by restoring the point it just created.
- **Restoring into Delta is not a file copy.** It goes through ``push_save``
  like any other push, because the record has to be updated to describe the
  restored bytes and to name their new Dropbox revision. Copying the backup into
  place and stopping there would leave Delta downloading the newer save straight
  back over it.

A ``GameSave-<sha1>`` record backup is listed but never restored on its own. The
record is regenerated from the save it accompanies, so restoring the save is the
operation; the record copy exists for manual recovery if the format model ever
turns out to be wrong.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import delta_writer

if TYPE_CHECKING:  # pragma: no cover -- imported for types only
    from . import dropbox_api
    from . import inspect as inspect_module
    from . import sync as sync_module

#: ``<original name>.<UTC stamp>.bak``. The original name may itself contain
#: dots ("Pokemon - Fire Red Version.srm"), so the stamp's shape is what makes
#: the split unambiguous rather than counting separators.
_BACKUP_NAME = re.compile(r"^(?P<original>.+)\.(?P<stamp>\d{8}T\d{12})\.bak$")

_STAMP_FORMAT = "%Y%m%dT%H%M%S%f"

DELTA = "Delta"
RETROARCH = "RetroArch"


@dataclass(frozen=True)
class Backup:
    """One backed-up file, decoded from its name."""

    path: Path
    original_name: str
    stamp: datetime
    size: int

    @property
    def side(self) -> str:
        """Which side the file came from.

        Decided by the name's shape, because that is what the two sides actually
        differ in: Delta's folder is flat and hash-named, RetroArch's files are
        named after the game.
        """
        if self.original_name.startswith(("GameSave-", "gamesave-", "Cheat-", "cheat-")):
            return DELTA
        return RETROARCH

    @property
    def kind(self) -> str:
        name = self.original_name
        if name.lower().startswith("cheat-"):
            return "cheat"
        if name.lower().startswith("gamesave-"):
            parts = name.split("-")
            if len(parts) == 2:
                return "record"
            return "clock" if parts[-1].lower() == "gametimesave" else "save"
        return "clock" if name.lower().endswith(".rtc") else "save"

    @property
    def identifier(self) -> str | None:
        """The game SHA-1 or cheat UUID, for a Delta-side backup."""
        if self.side != DELTA:
            return None
        parts = self.original_name.split("-")
        if self.original_name.lower().startswith("cheat-"):
            return "-".join(parts[1:])
        return parts[1] if len(parts) > 1 else None

    @property
    def restorable(self) -> bool:
        """A record on its own is context, not something to put back alone."""
        return self.kind != "record"

    @property
    def when(self) -> str:
        return self.stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_backup_name(name: str) -> tuple[str, datetime] | None:
    """Split a backup filename into what it was and when it was taken."""
    match = _BACKUP_NAME.match(name)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group("stamp"), _STAMP_FORMAT)
    except ValueError:
        return None
    return match.group("original"), stamp.replace(tzinfo=timezone.utc)


def scan(backup_dir: Path) -> list[Backup]:
    """Every backup in the folder, newest first.

    Anything that does not parse is skipped rather than reported. The folder is
    ours, but a stray file in it is not a reason to fail a listing.
    """
    found: list[Backup] = []
    if not backup_dir.is_dir():
        return found
    for path in backup_dir.iterdir():
        if not path.is_file():
            continue
        parsed = parse_backup_name(path.name)
        if parsed is None:
            continue
        original, stamp = parsed
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append(Backup(path, original, stamp, size))
    found.sort(key=lambda b: (b.stamp, b.original_name), reverse=True)
    return found


def label_for(backup: Backup, names: dict[str, str]) -> str:
    """A human name for what this backup belongs to.

    Delta's filenames are hashes, so the game name has to be joined in from the
    records. Falling back to the raw name matters: a backup whose game has since
    been deleted from Delta is exactly when someone needs to restore it.
    """
    if backup.side == RETROARCH:
        return Path(backup.original_name).stem
    identifier = backup.identifier
    if identifier and identifier in names:
        return names[identifier]
    return backup.original_name


@dataclass(frozen=True)
class RestorePoint:
    """One backup, presented as something a person can choose."""

    backup: Backup
    label: str

    def describe(self) -> str:
        return (
            f"{self.label}  ·  {self.backup.side} {self.backup.kind}  ·  "
            f"{self.backup.when}  ·  {self.backup.size:,} B"
        )


def restore_points(backups: list[Backup], names: dict[str, str]) -> list[RestorePoint]:
    """The restorable subset, newest first, ready to list."""
    return [
        RestorePoint(backup, label_for(backup, names))
        for backup in backups
        if backup.restorable
    ]


def find_retroarch_target(save_dir: Path, original_name: str) -> Path | None:
    """Where a RetroArch-side backup came from.

    Searched rather than reconstructed. Rebuilding the path means knowing which
    core RetroArch would pick and whether it sorts saves into folders, and both
    can have changed since the backup was taken -- while the file itself, if it
    is still there, is unambiguous.
    """
    if not save_dir.is_dir():
        return None
    lowered = original_name.lower()
    for path in save_dir.rglob("*"):
        if path.is_file() and path.name.lower() == lowered:
            return path
    return None


#: A restore deliberately leaves the manifest alone.
#:
#: The tempting alternative -- recording the restored file as the agreed state --
#: would stop the next sync propagating it, and the two sides would then hold
#: different saves while the manifest claimed they agreed. That divergence would
#: never be reconciled, because nothing would ever look changed again.
#:
#: Leaving it untouched means a restore behaves like any other edit: the side it
#: happened on now differs from the last agreement, so the next sync carries it
#: to the other side, or reports a conflict if that side moved too. Which is
#: exactly what someone restoring a save is asking for.
RESTORE_IS_A_CHANGE = (
    "the next sync will carry this to the other side, or report a conflict if "
    "that side also changed"
)


def restore_retroarch(
    point: RestorePoint, save_dir: Path, backup_dir: Path
) -> str:
    """Put a RetroArch-side file back, backing up what is there now."""
    from .sync import backup as take_backup
    from .sync import copy_atomically

    target = find_retroarch_target(save_dir, point.backup.original_name)
    if target is None:
        raise FileNotFoundError(
            f"nothing named {point.backup.original_name} under {save_dir}. "
            "RetroArch may not have this game any more, or it now sorts saves "
            "into a different folder."
        )

    take_backup(target, backup_dir)
    copy_atomically(point.backup.path, target)
    return f"restored {target}"


def restore_delta_cheat(
    point: RestorePoint, delta_folder: Path, backup_dir: Path
) -> str:
    """Put a cheat's old code back into Delta.

    Reads the code out of the backed-up record rather than writing the record
    file back wholesale. The rest of that record -- its hash, its archived type,
    its relationships -- belongs to whatever Delta has done since, and only the
    code is ours to change.
    """
    identifier = point.backup.identifier
    if not identifier:
        raise ValueError(f"cannot tell which cheat {point.backup.path.name} is")

    raw = json.loads(point.backup.path.read_text(encoding="utf-8"))
    code = raw.get("record", {}).get("code")
    if not isinstance(code, str):
        raise ValueError(f"{point.backup.path.name} holds no cheat code")

    return delta_writer.push_cheat(delta_folder, identifier, code, backup_dir)


def restore_delta_save(
    point: RestorePoint,
    sync_paths: "sync_module.Paths",
    entry: "inspect_module.GameEntry",
    dropbox: "dropbox_api.DropboxClient",
    *,
    file_identifier: str = delta_writer.PRIMARY_FILE,
) -> str:
    """Put a save (or clock) back into Delta, through the normal push path.

    Not a file copy. The record has to be updated to describe these bytes and to
    name the Dropbox revision they get when the desktop client uploads them --
    without that, Delta downloads the newer save straight back over the restored
    one and the restore silently undoes itself.
    """
    from .sync import push_with_revision

    return push_with_revision(
        sync_paths,
        entry,
        point.backup.path,
        dropbox,
        file_identifier=file_identifier,
    )
