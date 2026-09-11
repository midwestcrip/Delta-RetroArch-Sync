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
    from . import config as config_module
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
    #: The standalone emulator this came from, as an ``Emulator.key``, or empty
    #: for Delta and RetroArch. Taken from the subfolder rather than the name,
    #: because the name cannot carry it: mGBA and VBA-M both write
    #: ``Pokemon.sav``, so two emulators' restore points are the same string.
    emulator: str = ""

    @property
    def side(self) -> str:
        """Which side the file came from.

        Decided by the name's shape for the two original sides, because that is
        what they actually differ in: Delta's folder is flat and hash-named,
        RetroArch's files are named after the game. A standalone emulator's
        files are named the same way as RetroArch's, which is exactly why the
        folder has to say and the name cannot.
        """
        if self.emulator:
            from . import emulators as emulators_module

            found = emulators_module.for_key(self.emulator)
            return found.name if found is not None else self.emulator
        if self.original_name.startswith(("GameSave-", "gamesave-", "Cheat-", "cheat-")):
            return DELTA
        return RETROARCH

    @property
    def is_delta(self) -> bool:
        """Whether this came out of Delta's folder.

        Used where ``side == DELTA`` used to be compared directly, which stopped
        being safe once ``side`` could also be an emulator's display name.
        """
        return not self.emulator and self.original_name.startswith(
            ("GameSave-", "gamesave-", "Cheat-", "cheat-")
        )

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
        if not self.is_delta:
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

    One level of subfolders is included, each named after a standalone
    emulator. Only one level: these are folders this tool creates, so there is
    nothing below them, and a full walk would turn a stray directory somebody
    dropped in here into an unbounded scan.
    """
    found: list[Backup] = []
    if not backup_dir.is_dir():
        return found

    def collect(directory: Path, emulator: str) -> None:
        for path in directory.iterdir():
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
            found.append(Backup(path, original, stamp, size, emulator))

    collect(backup_dir, "")
    for child in backup_dir.iterdir():
        if child.is_dir():
            collect(child, child.name)

    found.sort(key=lambda b: (b.stamp, b.original_name), reverse=True)
    return found


def label_for(backup: Backup, names: dict[str, str]) -> str:
    """A human name for what this backup belongs to.

    Delta's filenames are hashes, so the game name has to be joined in from the
    records. Falling back to the raw name matters: a backup whose game has since
    been deleted from Delta is exactly when someone needs to restore it.
    """
    if not backup.is_delta:
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


def emulator_search_roots(
    key: str,
    config: "config_module.Config",
    rom_dir: Path | None = None,
) -> list[Path]:
    """Where a standalone emulator's save could be, for a restore.

    Same principle as :func:`find_retroarch_target`: the file is searched for
    rather than reconstructed, because where an emulator writes can have changed
    since the backup was taken. The difference is that there is no single folder
    to search -- most of these write beside the ROM -- so several candidates are
    tried.

    **The first candidate is the answer the sync itself would give**, via
    ``resolve_save_dir``, rather than a list assembled here. Re-deriving the
    candidates by hand is what broke this: that list held the install folder,
    the emulator's usual folders and the *configured* ROM folder, and so missed
    both of the automatic answers -- the folder the emulator's own config file
    names, and the ROM folder derived when none is configured. Either one meant
    a backup that could not be put back.

    The rest are still tried afterwards, because a save written before the
    emulator's config changed is exactly the one someone needs back.
    """
    from . import emulators as emulators_module

    roots: list[Path] = []
    override = config.emulator_save_dirs.get(key)
    if override is not None:
        roots.append(override)

    from . import config as config_module

    for installed in emulators_module.find_installed(
        named=config_module.emulator_dirs(config)
    ):
        if installed.emulator.key != key:
            continue
        resolved = emulators_module.resolve_save_dir(
            installed, rom_dir, override=override
        )
        if resolved.directory is not None:
            roots.append(resolved.directory)
        roots.append(installed.install_dir)
        for template in installed.emulator.save_dirs:
            candidate = emulators_module._expand(template, installed.install_dir)
            if candidate is not None:
                roots.append(candidate)

    for candidate in (rom_dir, config.retroarch_rom_dir):
        if candidate is not None:
            roots.append(candidate)

    seen: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in seen:
            seen.append(root)
    return seen


def find_target(
    point: RestorePoint,
    save_dir: Path,
    config: "config_module.Config | None" = None,
    rom_dir: Path | None = None,
) -> Path | None:
    """Where a desktop-side backup came from, RetroArch's or an emulator's."""
    if not point.backup.emulator:
        return find_retroarch_target(save_dir, point.backup.original_name)
    if config is None:
        return None
    for root in emulator_search_roots(point.backup.emulator, config, rom_dir):
        found = find_retroarch_target(root, point.backup.original_name)
        if found is not None:
            return found
    return None


def restore_retroarch(
    point: RestorePoint,
    save_dir: Path,
    backup_dir: Path,
    config: "config_module.Config | None" = None,
    rom_dir: Path | None = None,
) -> str:
    """Put a desktop-side file back, backing up what is there now.

    Handles RetroArch and the standalone emulators both. They are the same
    operation -- find the file, back it up, copy over it -- and differ only in
    where the file is looked for.
    """
    from .sync import backup as take_backup
    from .sync import copy_atomically

    target = find_target(point, save_dir, config, rom_dir)
    if target is None:
        where = point.backup.side if point.backup.emulator else f"under {save_dir}"
        raise FileNotFoundError(
            f"nothing named {point.backup.original_name} in {where}. "
            f"{point.backup.side} may not have this game any more, or it now "
            "keeps its saves in a different folder."
        )

    # Into the same folder the backup came from, so a restore of an emulator's
    # save stays that emulator's history rather than landing in RetroArch's.
    if point.backup.emulator:
        backup_dir = backup_dir / point.backup.emulator
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
