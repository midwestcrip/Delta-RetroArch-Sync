"""The reconcile pass.

Compares each game's save on both sides against the manifest and acts on the
difference. Data loss is the failure mode being designed against, so:

- Delta's Dropbox folder is never written to. See ``PUSH_BLOCKED`` below.
- Anything about to be overwritten is backed up first.
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

from . import inspect as inspect_module
from . import manifest as manifest_module
from . import naming

#: Why the RetroArch -> Delta direction is not implemented.
#:
#: Writing a save back to Delta means overwriting GameSave-<sha1>-gameSave in
#: the Dropbox folder AND updating the record JSON's `sha1` to match, because
#: Delta verifies the file against that hash. The record also carries a
#: top-level `sha1Hash` that Harmony computes over the record itself by a
#: scheme we have not confirmed. Getting that wrong risks Delta rejecting the
#: record, or worse, accepting a save it then treats as corrupt.
#:
#: That is a reverse-engineering exercise whose failure mode is the user's real
#: save file, so it stays unimplemented until it can be tested against a
#: throwaway game rather than guessed at.
PUSH_BLOCKED = (
    "writing to Delta's Dropbox folder is not yet implemented -- see PUSH_BLOCKED"
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


def run_sync(
    paths: Paths,
    entries: list[inspect_module.GameEntry],
    core_name: str,
    sorted_by_core: bool,
    *,
    dry_run: bool = False,
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

        target = retroarch_save_path(
            paths.save_dir, entry, core_name, sorted_by_core
        )
        action, detail = decide(state.get(entry.identifier), entry.save_path, target)

        if action is Action.PUSH:
            report.outcomes.append(
                Outcome(entry.name, Action.PUSH, f"{detail}; {PUSH_BLOCKED}")
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
