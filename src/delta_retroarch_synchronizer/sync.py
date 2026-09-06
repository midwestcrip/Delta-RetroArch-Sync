"""The reconcile pass.

Compares each game's save on both sides against the manifest and acts on the
difference. Data loss is the failure mode being designed against, so:

- Delta's Dropbox folder is only written to when pushing is explicitly enabled,
  and never as a side effect of a pull. See ``PUSH_IS_EXPERIMENTAL`` below.
- Anything about to be overwritten is backed up first, on both sides.
- A save is written to a temporary file and moved into place, so a crash
  mid-write cannot leave a half-written save where a good one used to be.
- A real conflict is reported, never resolved by guessing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from . import cheats as cheats_module
from . import delta_writer, dropbox_api
from . import inspect as inspect_module
from . import manifest as manifest_module
from . import naming

#: The RetroArch -> Delta direction writes into Delta's Dropbox folder, which
#: Delta's own docs warn against. It is off by default and enabled per run.
#:
#: The record format is fully modelled (see delta_writer) and every push verifies
#: the existing record's hash before touching anything. The one part that cannot
#: be derived locally is the save's Dropbox revision, which is read back from the
#: API after the desktop client uploads -- see push_with_revision.
PUSH_IS_EXPERIMENTAL = (
    "push writes into Delta's Dropbox folder; verify the change reaches your "
    "phone before relying on it"
)

BACKUP_DIRNAME = "backups"
#: How many old versions of each save to keep.
BACKUP_KEEP = 10


class Action(Enum):
    NOTHING = "nothing to do"
    PULL = "pull: Delta -> RetroArch"
    PUSH = "push: RetroArch -> Delta"
    CONFLICT = "CONFLICT: both sides changed"
    MISSING = "nothing to sync"
    SKIPPED = "skipped"


@dataclass
class Outcome:
    game: str
    action: Action
    detail: str = ""
    applied: bool = False


@dataclass
class SyncReport:
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def conflicts(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.action is Action.CONFLICT]

    @property
    def changed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.applied]


def decide(
    entry: manifest_module.Entry,
    delta_save: Path | None,
    retroarch_save: Path | None,
) -> tuple[Action, str]:
    """Work out what to do for one game, from the two files and the manifest.

    Pure: it reads file contents but changes nothing, so the decision can be
    tested and shown to the user (``--dry-run``) before anything is written.
    """
    delta_exists = delta_save is not None and delta_save.is_file()
    retro_exists = retroarch_save is not None and retroarch_save.is_file()

    if not delta_exists and not retro_exists:
        return Action.MISSING, "neither side has a save"

    # First sync for this game: whichever side has the only save is the source.
    if entry.delta is None and entry.retroarch is None:
        if delta_exists and not retro_exists:
            return Action.PULL, "first sync, only Delta has a save"
        if retro_exists and not delta_exists:
            return Action.PUSH, "first sync, only RetroArch has a save"
        # Both exist with no agreed history. They may be identical, in which
        # case there is nothing to do and nothing to lose.
        assert delta_save is not None and retroarch_save is not None
        if manifest_module.sha1_of(delta_save) == manifest_module.sha1_of(
            retroarch_save
        ):
            return Action.NOTHING, "both sides already identical"
        return Action.CONFLICT, "both sides have saves but no agreed history"

    # A side with no file cannot be a source. If one side's save is gone while
    # the other still has one, that is a restore, not a change to propagate --
    # treating a vanished RetroArch save as "RetroArch changed" would mean
    # pushing a deletion to Delta and wiping the save on the phone too.
    if not retro_exists:
        return Action.PULL, "RetroArch's save is missing; restoring from Delta"
    if not delta_exists:
        return Action.PUSH, "Delta's save is missing; restoring from RetroArch"

    delta_changed = not (
        entry.delta is not None
        and delta_save is not None
        and entry.delta.matches(delta_save)
    )
    retro_changed = not (
        entry.retroarch is not None
        and retroarch_save is not None
        and entry.retroarch.matches(retroarch_save)
    )

    if delta_changed and retro_changed:
        return Action.CONFLICT, "both sides changed since the last sync"
    if delta_changed:
        return Action.PULL, "Delta changed since the last sync"
    if retro_changed:
        return Action.PUSH, "RetroArch changed since the last sync"
    return Action.NOTHING, "unchanged on both sides"


def backup(path: Path, backup_dir: Path, *, keep: int = BACKUP_KEEP) -> Path | None:
    """Copy a file aside before it is overwritten, keeping the last ``keep``.

    Named with a UTC timestamp so the ordering is stable regardless of local
    time or DST, and so two backups in the same second do not collide.
    """
    if not path.is_file():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, destination)

    existing = sorted(backup_dir.glob(f"{path.name}.*.bak"))
    for stale in existing[:-keep]:
        try:
            stale.unlink()
        except OSError:
            # A backup we cannot prune is not worth failing a sync over.
            pass
    return destination


def copy_atomically(source: Path, destination: Path) -> None:
    """Copy so that the destination is never observed half-written.

    Writing straight to the destination means a crash mid-copy replaces a good
    save with a truncated one. Writing beside it and moving into place makes the
    swap atomic on the same filesystem.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        shutil.copy2(source, temporary)
        # Verify before publishing: a short copy must never become the save.
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError(
                f"copy of {source.name} was {temporary.stat().st_size} bytes, "
                f"expected {source.stat().st_size}"
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


@dataclass
class Paths:
    delta_folder: Path
    retroarch_config: Path
    save_dir: Path
    state_dir: Path

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / BACKUP_DIRNAME

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / manifest_module.MANIFEST_FILENAME


def retroarch_save_path(
    save_dir: Path, entry: inspect_module.GameEntry, core_name: str, sorted_by_core: bool
) -> Path:
    """Where RetroArch keeps (or would keep) this game's save."""
    assert entry.system is not None
    folder = save_dir / core_name if sorted_by_core else save_dir
    return folder / naming.save_filename(entry.name, entry.system.retroarch_save_ext)


def sync_rom(
    entry: inspect_module.GameEntry, rom_dir: Path, *, dry_run: bool = False
) -> Outcome | None:
    """Copy a game's ROM out of Delta so RetroArch has something to load.

    The filename matters more than it looks: RetroArch names a battery save
    after the content file, so this stem and the save's stem must agree or
    RetroArch will not find the save this tool just wrote.

    ROMs are content, not progress -- they never change once written, so unlike
    saves they get no rolling backups and an existing matching ROM is left
    alone rather than rewritten.
    """
    if entry.rom_path is None or not entry.rom_path.is_file():
        return None
    if entry.system is None or not entry.rom_extension:
        return None

    destination = rom_dir / naming.rom_filename(entry.name, entry.rom_extension)

    if destination.is_file():
        # Compare by size first: these are tens of megabytes and identical size
        # with different content is not a case that arises for a ROM we wrote.
        if destination.stat().st_size == entry.rom_path.stat().st_size:
            return Outcome(entry.name, Action.NOTHING, f"ROM already at {destination}")

    if dry_run:
        return Outcome(entry.name, Action.PULL, f"would copy ROM to {destination}")

    copy_atomically(entry.rom_path, destination)
    return Outcome(
        entry.name,
        Action.PULL,
        f"copied ROM ({entry.rom_path.stat().st_size:,} B) to {destination}",
        applied=True,
    )


def push_with_revision(
    paths: "Paths",
    entry: inspect_module.GameEntry,
    source: Path,
    dropbox: "dropbox_api.DropboxClient",
) -> str:
    """Push a save, then repair the record to name its real Dropbox revision.

    Two writes are needed, and the order matters. The save has to reach Dropbox
    before its revision exists, so:

      1. Write the save (record still names the old revision, so Delta ignores
         it -- an inconsistent intermediate state that is safe rather than
         misleading).
      2. Wait for the desktop client to upload it and read the new revision back,
         confirming by content hash that it is *our* upload.
      3. Rewrite the record with that revision and the new hashes.

    If step 2 fails the record still points at the old save, so Delta simply
    carries on with what it had. Nothing is left half-applied.
    """
    save_name = f"GameSave-{entry.identifier}-gameSave"
    delta_writer.push_save(
        paths.delta_folder, entry.identifier, source, paths.backup_dir, revision=None
    )

    expected = dropbox_api.content_hash(paths.delta_folder / save_name)
    remote_path = f"/{paths.delta_folder.name}/{save_name}"
    revision = dropbox.wait_for_revision(remote_path, expected)

    note = delta_writer.push_save(
        paths.delta_folder,
        entry.identifier,
        source,
        paths.backup_dir,
        revision=revision,
    )
    return f"{note}, revision {revision}"


def sync_cheats(
    entry: inspect_module.GameEntry,
    game_cheats: list[dict[str, str]],
    cheat_dir: Path,
    *,
    dry_run: bool = False,
) -> Outcome | None:
    """Write a game's Delta cheats out as a RetroArch `.cht` file.

    Delta -> RetroArch only. A cheat created on the RetroArch side would need a
    brand-new Cheat record in Delta's folder, and a file we create has no Dropbox
    property groups -- which Harmony requires to see a record at all and only
    Delta's app can write. So that direction is not possible, rather than merely
    unimplemented.
    """
    if entry.system is None or not entry.system.retroarch_db_name:
        return None
    if not game_cheats:
        return None

    path = cheats_module.cheat_file_path(
        cheat_dir, entry.system.retroarch_db_name, entry.name
    )
    parsed = [
        cheats_module.Cheat(
            name=cheat.get("name", ""),
            code=cheat.get("code", ""),
            type=cheat.get("type", ""),
        )
        for cheat in game_cheats
    ]

    if dry_run:
        return Outcome(
            entry.name,
            Action.PULL,
            f"would write {len(parsed)} cheat(s) to {path}",
        )

    changed = cheats_module.write_cheat_file(path, parsed)
    if not changed:
        return Outcome(entry.name, Action.NOTHING, f"cheats already current at {path}")
    return Outcome(
        entry.name,
        Action.PULL,
        f"wrote {len(parsed)} cheat(s) to {path}",
        applied=True,
    )


def run_sync(
    paths: Paths,
    entries: list[inspect_module.GameEntry],
    core_name: str,
    sorted_by_core: bool,
    *,
    dry_run: bool = False,
    allow_push: bool = False,
    rom_dir: Path | None = None,
    dropbox: "dropbox_api.DropboxClient | None" = None,
    cheat_dir: Path | None = None,
    cheats_by_game: dict[str, list[dict[str, str]]] | None = None,
) -> SyncReport:
    """Reconcile every supported game. Returns what was done or would be done."""
    state = manifest_module.Manifest.load(paths.manifest_path)
    report = SyncReport()

    for entry in entries:
        if not entry.supported:
            note = (
                entry.system.conversion_note
                if entry.system and entry.system.conversion_note
                else "system not enabled for sync"
            )
            report.outcomes.append(Outcome(entry.name, Action.SKIPPED, note))
            continue

        if rom_dir is not None:
            rom_outcome = sync_rom(entry, rom_dir, dry_run=dry_run)
            if rom_outcome is not None:
                report.outcomes.append(rom_outcome)

        if cheat_dir is not None and cheats_by_game is not None:
            cheat_outcome = sync_cheats(
                entry,
                cheats_by_game.get(entry.identifier, []),
                cheat_dir,
                dry_run=dry_run,
            )
            if cheat_outcome is not None:
                report.outcomes.append(cheat_outcome)

        target = retroarch_save_path(
            paths.save_dir, entry, core_name, sorted_by_core
        )
        action, detail = decide(state.get(entry.identifier), entry.save_path, target)

        if action is Action.PUSH:
            if not allow_push:
                report.outcomes.append(
                    Outcome(
                        entry.name,
                        Action.PUSH,
                        f"{detail}; not pushed (pass --push to enable)",
                    )
                )
                continue
            if dry_run:
                report.outcomes.append(
                    Outcome(entry.name, Action.PUSH, f"{detail} (dry run)")
                )
                continue
            if dropbox is None:
                report.outcomes.append(
                    Outcome(
                        entry.name,
                        Action.PUSH,
                        f"{detail}; {delta_writer.REVISION_MUST_BE_REAL}",
                    )
                )
                continue
            try:
                note = push_with_revision(
                    paths, entry, target, dropbox
                )
            except (OSError, ValueError, dropbox_api.DropboxError) as error:
                report.outcomes.append(
                    Outcome(entry.name, Action.PUSH, f"{detail}; FAILED: {error}")
                )
                continue
            state.record(entry.identifier, entry.save_path, target)
            report.outcomes.append(
                Outcome(entry.name, Action.PUSH, f"{detail}; {note}", applied=True)
            )
            continue

        if action is not Action.PULL:
            report.outcomes.append(Outcome(entry.name, action, detail))
            continue

        if dry_run:
            report.outcomes.append(
                Outcome(entry.name, Action.PULL, f"{detail} (dry run)")
            )
            continue

        assert entry.save_path is not None
        saved = backup(target, paths.backup_dir)
        copy_atomically(entry.save_path, target)
        state.record(entry.identifier, entry.save_path, target)

        note = f"{detail} -> {target}"
        if saved is not None:
            note += f" (previous version backed up)"
        report.outcomes.append(Outcome(entry.name, Action.PULL, note, applied=True))

    if not dry_run:
        state.save()
    return report
