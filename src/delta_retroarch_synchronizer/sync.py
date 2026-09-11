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

import glob as glob_module
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from . import cheats as cheats_module
from . import clock, harmony
from . import delta_writer, dropbox_api
from . import inspect as inspect_module
from . import manifest as manifest_module
from . import emulators, n64, naming, playlist

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
    #: Which desktop side this outcome is about. The Action's own wording names
    #: RetroArch, which was true of every outcome until standalone emulators
    #: became a target and is now wrong for most of them -- a save copied into
    #: mGBA was reported as "pull: Delta -> RetroArch", naming a program that
    #: had not been touched.
    target: str = "RetroArch"
    applied: bool = False
    #: Set when the action was attempted and went wrong, as opposed to being
    #: declined or left alone. A refusal ("not pushed, no Dropbox auth") is a
    #: warning; a write that raised is an error, and the log colours them
    #: differently. Kept structural so the severity cannot drift away from the
    #: wording of the message.
    failed: bool = False

    @property
    def description(self) -> str:
        """The action, naming the side it actually happened to."""
        if self.action is Action.PULL:
            return f"pull: Delta -> {self.target}"
        if self.action is Action.PUSH:
            return f"push: {self.target} -> Delta"
        return self.action.value


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
    *,
    target: str = manifest_module.RETROARCH,
    label: str = "RetroArch",
) -> tuple[Action, str]:
    """Work out what to do for one game, from the two files and the manifest.

    Pure: it reads file contents but changes nothing, so the decision can be
    tested and shown to the user (``--dry-run``) before anything is written.

    ``target`` names which desktop side is being reconciled, because there can
    be more than one -- RetroArch and any standalone emulator each keep their
    own agreement with Delta. ``label`` is the same thing said for a human, and
    is only ever used in the returned explanation.
    """
    delta_exists = delta_save is not None and delta_save.is_file()
    retro_exists = retroarch_save is not None and retroarch_save.is_file()

    # Both halves come from this target's own agreement. Reading the Delta half
    # from the entry as a whole would mean asking "has Delta changed since
    # *someone* synced" when the only useful question is "since this target
    # did" -- and with RetroArch reconciled first, the answer for every
    # standalone emulator behind it was permanently "no".
    agreed = entry.agreement(target)
    agreed_delta = agreed.delta
    agreed_desktop = agreed.desktop

    if not delta_exists and not retro_exists:
        return Action.MISSING, "neither side has a save"

    # First sync for this game *to this target*: whichever side has the only
    # save is the source.
    #
    # Keyed on this target's own history, not on the entry as a whole. It used
    # to also require ``entry.delta is None``, which was indistinguishable while
    # RetroArch was the only target -- ``record`` writes both halves together --
    # and became wrong the moment a second one existed. Enabling a new emulator
    # that already had its own save for a game gave: Delta unchanged since the
    # last RetroArch sync, this target "changed" because it has no history, so
    # PUSH -- quietly sending that emulator's unrelated save to the phone and
    # over the real one, with no conflict reported. Now it is a conflict, which
    # is what two saves and no history has always meant here.
    if agreed_desktop is None:
        if delta_exists and not retro_exists:
            return Action.PULL, "first sync, only Delta has a save"
        if retro_exists and not delta_exists:
            return Action.PUSH, f"first sync, only {label} has a save"
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
        return Action.PULL, f"{label}'s save is missing; restoring from Delta"
    if not delta_exists:
        return Action.PUSH, f"Delta's save is missing; restoring from {label}"

    delta_changed = not (
        agreed_delta is not None
        and delta_save is not None
        and agreed_delta.matches(delta_save)
    )
    retro_changed = not (
        agreed_desktop is not None
        and retroarch_save is not None
        and agreed_desktop.matches(retroarch_save)
    )

    if delta_changed and retro_changed:
        return Action.CONFLICT, "both sides changed since the last sync"
    if delta_changed:
        return Action.PULL, "Delta changed since the last sync"
    if retro_changed:
        return Action.PUSH, f"{label} changed since the last sync"
    return Action.NOTHING, "unchanged on both sides"


def pushed_to_delta(outcomes: "list[Outcome]") -> bool:
    """Whether this pass actually moved Delta, as opposed to planning to."""
    return any(o.action is Action.PUSH and o.applied for o in outcomes)


def settle(
    run_pass: "Callable[[], list[Outcome]]",
    first: "list[Outcome]",
    *,
    dry_run: bool = False,
) -> list[Outcome]:
    """Reconcile again when a push moved Delta mid-run, and report what that did.

    Targets are reconciled one after another, so a push from a later one leaves
    every target already visited holding the older save -- while the run reports
    success, which is the part that makes it dangerous rather than merely
    untidy. Play RetroArch before the next sync and that push has silently
    arranged a conflict: both sides will have changed.

    So when Delta actually moved, everything is reconciled once more. Whatever
    was left behind pulls the new save; anything genuinely divergent surfaces as
    the conflict it is.

    One extra pass, never a loop. A second push in the second pass is not
    possible: Delta has changed since every remaining target's agreement, so a
    target with its own newer save is a conflict now rather than a push. Capping
    it structurally is better than trusting that argument to stay true -- this
    runs against the user's real saves, and a reconcile that can iterate is one
    that can iterate forever.
    """
    if dry_run or not pushed_to_delta(first):
        return []
    return run_pass()


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

    # The name is escaped because it goes into a glob pattern and a save is
    # named after its ROM, where "[U]", "[!]" and "[T+Eng]" are ordinary. An
    # unescaped "[U]" is a character class matching the single letter U, so the
    # pattern never matches this file's own backups: pruning silently stops and
    # they grow without bound. It matches "Zelda U.srm" instead, which is the
    # other half of why the name has no business being read as a pattern.
    prefix = glob_module.escape(path.name)
    existing = sorted(backup_dir.glob(f"{prefix}.*.bak"))
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


#: Said once per game, never repeated. Deliberately explains the consequence
#: rather than the mechanism: "syncableFiles has no entry for it" is true and
#: useless to someone who just wants to know whether their ghosts will travel.
CONTROLLER_PAK_NOTICE = (
    "this game keeps data on a Controller Pak, and Delta does not sync those. "
    "The cartridge save travels normally; anything on the pak stays on the "
    "machine that made it. Said once."
)

#: The same fact, for an N64 game whose progress can only be on a pak.
CONTROLLER_PAK_ONLY_NOTICE = (
    "this game has no cartridge save in Delta but has Controller Pak data on "
    "this PC, so its progress lives entirely on the pak -- which Delta does not "
    "sync. Nothing will travel for this game. Said once."
)


def controller_pak_notice(
    entry: inspect_module.GameEntry,
    target: Path,
    state: manifest_module.Manifest,
) -> Outcome | None:
    """Say once when a game is keeping data somewhere that will not sync.

    Detected from the save itself rather than from a list of titles. A hardcoded
    list of "games that use the Controller Pak" would be a guess dressed as a
    fact, and this project has been wrong four times that way; a pack with data
    in it is evidence. The cost is that the notice arrives after the player has
    used the pack rather than before, which is also when it starts to matter.
    """
    system = entry.system
    if system is None or system.key != "n64" or not target.is_file():
        return None

    key = f"{entry.identifier}:controller-pak"
    if state.already_said(key):
        return None

    try:
        srm = target.read_bytes()
    except OSError:
        return None
    if len(srm) != n64.SRM_SIZE or not n64.has_controller_pak_data(srm):
        return None

    state.record_said(key)
    has_cartridge_save = entry.save_path is not None and entry.save_path.is_file()
    return Outcome(
        entry.name,
        Action.SKIPPED,
        CONTROLLER_PAK_NOTICE if has_cartridge_save else CONTROLLER_PAK_ONLY_NOTICE,
    )


def unverified_save_core(entry: inspect_module.GameEntry, core_name: str) -> str | None:
    """Why a game is not being synced, when its core's layout is unchecked.

    The same gate ``clock_cores`` applies to the Game Boy clock, applied to the
    save itself. It only bites on a converted system: for a plain copy the
    frontend owns the file and the core does not change its shape.

    Worth saying out loud rather than skipping silently -- a game that quietly
    never syncs looks exactly like a bug.
    """
    system = entry.system
    if system is None or not system.converted:
        return None
    if not system.converted_cores or core_name in system.converted_cores:
        return None
    return (
        f"not synced: {core_name} may not lay out its save the same way as "
        f"{system.converted_cores[0]}, and this conversion was only checked "
        f"against that one. Nothing was written. Use {system.converted_cores[0]} "
        f"for {system.name} and it will sync."
    )


def write_to_retroarch(
    entry: inspect_module.GameEntry, source: Path, target: Path
) -> str:
    """Put Delta's save where RetroArch will find it, converting if needed.

    Everything but N64 is a copy. N64 is a merge into the combined `.srm`, which
    is why this cannot be `copy_atomically` for every system: overwriting that
    file wholesale would take the Controller Paks with it.
    """
    system = entry.system
    if system is None or not system.converted:
        copy_atomically(source, target)
        return f"copied to {target}"

    existing = target.read_bytes() if target.is_file() else None
    merged = n64.to_retroarch(source.read_bytes(), existing)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    try:
        temporary.write_bytes(merged)
        temporary.replace(target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

    region = n64.region_for(len(source.read_bytes()))
    kept = "Controller Paks kept" if existing is not None else "Controller Paks formatted"
    return f"merged {region.name} into {target} ({kept})"


def stage_for_delta(
    entry: inspect_module.GameEntry, target: Path, state_dir: Path
) -> tuple[Path, bool]:
    """The file to push, and whether it is a temporary one to clean up.

    For a converted system the save has to come *out* of RetroArch's combined
    file first. The byte count is taken from Delta's own copy rather than
    guessed, so what goes home is the shape Delta expects -- which is also why
    the push size guard needs no exemption here.
    """
    system = entry.system
    if system is None or not system.converted:
        return target, False

    if entry.save_path is None or not entry.save_path.is_file():
        raise ValueError(
            f"{entry.name}: Delta has no save to tell us how much of "
            "RetroArch's combined file belongs to this cartridge"
        )

    size = entry.save_path.stat().st_size
    extracted = n64.to_delta(target.read_bytes(), size)

    state_dir.mkdir(parents=True, exist_ok=True)
    staged = state_dir / f"{entry.identifier}.extracted"
    staged.write_bytes(extracted)
    return staged, True


@dataclass
class Paths:
    delta_folder: Path
    retroarch_config: Path
    save_dir: Path
    state_dir: Path

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / BACKUP_DIRNAME

    def emulator_backup_dir(self, key: str) -> Path:
        """A folder of its own for one standalone emulator's backups.

        The flat folder cannot hold these. Two emulators for the same system use
        the same extension -- mGBA and VBA-M both write ``Pokemon.sav`` -- so
        their backups arrive under one name, which loses three things at once:
        which emulator a restore point came from, where it would be restored to,
        and its own rolling history, because ``backup``'s keep-the-last-ten
        prunes by name and the two would delete each other's.

        RetroArch keeps the flat folder it has always had, so every backup taken
        before this still reads exactly as it did.
        """
        return self.backup_dir / key

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


def find_saves(save_dir: Path, filename: str) -> list[Path]:
    """Every file under RetroArch's save folder with this name.

    The companion to :func:`retroarch_save_path`, and preferred over it when
    the file already exists: that function computes where a save *would* go,
    which needs the core RetroArch picked and whether it sorts saves into
    folders, and both can have changed since the file was written. The file
    itself is unambiguous.

    All of them, not the first. Turning ``sort_savefiles_enable`` on and then
    off leaves a copy loose in the folder *and* one inside a core's subfolder,
    and only one of them is the file RetroArch reads -- so a caller about to
    write needs to know there is a choice rather than being handed one.
    """
    if not save_dir.is_dir():
        return []
    lowered = filename.lower()
    return sorted(
        path
        for path in save_dir.rglob("*")
        if path.is_file() and path.name.lower() == lowered
    )


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


def register_in_playlist(
    entry: inspect_module.GameEntry,
    rom_dir: Path,
    playlist_dir: Path | None,
    *,
    dry_run: bool = False,
) -> Outcome | None:
    """Make a copied ROM visible on RetroArch's menu.

    Copying a ROM into a folder does nothing for RetroArch, which lists content
    from playlists. On a fresh install there are none, so the menu stays empty
    and the game can only be reached through Load Content.
    """
    if playlist_dir is None or entry.system is None or not entry.rom_extension:
        return None
    if not entry.system.retroarch_db_name:
        return None

    destination = rom_dir / naming.rom_filename(entry.name, entry.rom_extension)
    if not destination.is_file():
        return None

    target = playlist.playlist_path(playlist_dir, entry.system.retroarch_db_name)

    if dry_run:
        if playlist.contains(playlist.load(target), destination):
            return None
        return Outcome(
            entry.name, Action.PULL, f"would add to playlist {target.name}"
        )

    written = playlist.register(
        playlist_dir, entry.system.retroarch_db_name, destination, entry.name
    )
    if written is None:
        return None
    return Outcome(
        entry.name,
        Action.PULL,
        f"added to RetroArch playlist {written.name}",
        applied=True,
    )


def push_with_revision(
    paths: "Paths",
    entry: inspect_module.GameEntry,
    source: Path,
    dropbox: "dropbox_api.DropboxClient",
    *,
    file_identifier: str = delta_writer.PRIMARY_FILE,
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
    save_name = harmony.resolve_existing(
        paths.delta_folder, f"GameSave-{entry.identifier}-{file_identifier}"
    ).name
    delta_writer.push_save(
        paths.delta_folder,
        entry.identifier,
        source,
        paths.backup_dir,
        file_identifier=file_identifier,
        revision=None,
    )

    expected = dropbox_api.content_hash(paths.delta_folder / save_name)
    remote_path = f"/{paths.delta_folder.name}/{save_name}"
    revision = dropbox.wait_for_revision(remote_path, expected)

    note = delta_writer.push_save(
        paths.delta_folder,
        entry.identifier,
        source,
        paths.backup_dir,
        file_identifier=file_identifier,
        revision=revision,
    )
    return f"{note}, revision {revision}"


def clock_files(
    entry: inspect_module.GameEntry, save_target: Path, core_name: str
) -> tuple[Path, Path] | None:
    """Delta's and RetroArch's clock files for a game, if both are meaningful.

    Returns None -- meaning "this game has no clock to sync" -- when the system
    has no clock file at all (every system but Game Boy Color), or when the core
    in use is not one whose format has been verified. See ``System.clock_cores``.
    """
    system = entry.system
    if system is None or not system.delta_clock_id or not system.retroarch_clock_ext:
        return None
    if core_name not in system.clock_cores:
        return None
    if entry.save_path is None:
        return None

    delta_clock = harmony.resolve_existing(
        entry.save_path.parent, f"GameSave-{entry.identifier}-{system.delta_clock_id}"
    )
    retro_clock = clock.retroarch_clock_path(
        save_target, system.retroarch_clock_ext
    )
    return delta_clock, retro_clock


def unverified_clock_core(
    entry: inspect_module.GameEntry, core_name: str
) -> str | None:
    """Why a game with a clock is not getting one synced, if that is the case.

    Worth saying out loud rather than silently skipping: the save will carry
    across perfectly and the in-game date will not, which looks like a bug in
    the sync rather than a deliberate limit.
    """
    system = entry.system
    if system is None or not system.delta_clock_id:
        return None
    if not system.clock_cores or core_name in system.clock_cores:
        return None
    return (
        f"clock not synced: {core_name} stores it in a format this tool has not "
        f"verified. The save is unaffected. Use "
        f"{system.clock_cores[0]} for the in-game clock to follow too."
    )


def pull_clock(
    entry: inspect_module.GameEntry,
    save_target: Path,
    core_name: str,
    backup_dir: Path,
    *,
    dry_run: bool = False,
) -> Outcome | None:
    """Bring Delta's real-time clock across to RetroArch.

    Only ever called in the same direction as the save it belongs to, and only
    when the save actually moved. The clock is a reference point paired with the
    save: writing one without the other makes the emulator measure elapsed time
    from the wrong instant, and writing an older clock over a newer one would
    move the in-game date backwards, which Pokemon Crystal notices.
    """
    unverified = unverified_clock_core(entry, core_name)
    if unverified is not None:
        return Outcome(entry.name, Action.SKIPPED, unverified)

    resolved = clock_files(entry, save_target, core_name)
    if resolved is None:
        return None
    delta_clock, retro_clock = resolved
    if not delta_clock.is_file():
        return None

    try:
        stored = delta_clock.read_bytes()
        converted = clock.to_retroarch(stored)
    except (OSError, ValueError) as error:
        return Outcome(entry.name, Action.SKIPPED, f"clock not synced: {error}")

    if retro_clock.is_file() and retro_clock.read_bytes() == converted:
        return None
    if dry_run:
        return Outcome(entry.name, Action.PULL, f"would write clock to {retro_clock}")

    backup(retro_clock, backup_dir)
    retro_clock.parent.mkdir(parents=True, exist_ok=True)
    retro_clock.write_bytes(converted)
    when = clock.describe(clock.delta_timestamp(stored))
    return Outcome(
        entry.name,
        Action.PULL,
        # Not "last played": this is the instant the cartridge clock read zero,
        # which is what Gambatte counts forward from. Calling it a play time
        # invited exactly the wrong conclusion when the in-game clock did not
        # move during fast-forward.
        f"clock base set to {when}; the in-game clock counts real time from there",
        applied=True,
    )


def push_clock(
    paths: "Paths",
    entry: inspect_module.GameEntry,
    save_target: Path,
    core_name: str,
    dropbox: "dropbox_api.DropboxClient | None",
    *,
    dry_run: bool = False,
) -> Outcome | None:
    """Send RetroArch's real-time clock back to Delta.

    Writes into Delta's folder, so it goes through ``delta_writer`` with the
    same record handling and revision round-trip the save gets. The clock is a
    second file on the same record with its own hash and revision.
    """
    unverified = unverified_clock_core(entry, core_name)
    if unverified is not None:
        return Outcome(entry.name, Action.SKIPPED, unverified)

    resolved = clock_files(entry, save_target, core_name)
    if resolved is None:
        return None
    delta_clock, retro_clock = resolved
    if not retro_clock.is_file() or not delta_clock.is_file():
        return None

    try:
        converted = clock.to_delta(retro_clock.read_bytes())
    except (OSError, ValueError) as error:
        return Outcome(entry.name, Action.SKIPPED, f"clock not pushed: {error}")

    if delta_clock.read_bytes() == converted:
        return None
    if dry_run:
        return Outcome(entry.name, Action.PUSH, "would push clock to Delta (dry run)")
    if dropbox is None:
        return Outcome(
            entry.name, Action.PUSH, f"clock not pushed; {delta_writer.REVISION_MUST_BE_REAL}"
        )

    system = entry.system
    assert system is not None
    staged = paths.state_dir / f"{entry.identifier}.{system.delta_clock_id}.staged"
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(converted)
        note = push_with_revision(
            paths, entry, staged, dropbox, file_identifier=system.delta_clock_id
        )
    except (OSError, ValueError, dropbox_api.DropboxError) as error:
        return Outcome(
            entry.name, Action.PUSH, f"clock FAILED: {error}", failed=True
        )
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    return Outcome(
        entry.name,
        Action.PUSH,
        f"clock sent to Delta ({clock.describe(clock.delta_timestamp(converted))}); {note}",
        applied=True,
    )


def match_cheats(
    delta_cheats: list[cheats_module.Cheat],
    sources: list[dict[str, str]],
    existing: list[cheats_module.Cheat],
    state: manifest_module.Manifest | None,
) -> tuple[list[cheats_module.Cheat | None], list[cheats_module.Cheat]]:
    """Pair each Delta cheat with its `.cht` entry, tolerating one rename.

    A `.cht` carries no UUID, so the name is the only link between the two
    sides -- which means a rename breaks it, and the cheat silently reappears as
    an uncreatable RetroArch-only one. Three passes, strongest signal first:

    1. **Exact name.** Nothing was renamed; the overwhelmingly common case.
    2. **The last agreed name.** Delta was renamed, so the `.cht` still holds
       the old name.
    3. **The code.** RetroArch was renamed, so the name is gone but the code is
       still the one both sides last agreed on.
    4. **Elimination**, and only when exactly one is left unpaired on each side.
       Both fields were edited at once -- an ordinary thing to do in RetroArch's
       cheat editor -- so nothing is left to match on, but with one candidate
       apiece the pairing is forced rather than chosen. With two or more
       remaining it is a genuine choice, so they stay unpaired and are reported.

    Returns a counterpart for each Delta cheat (``None`` where unmatched) and
    whatever `.cht` entries were left over.
    """
    remaining = list(existing)
    matched: list[cheats_module.Cheat | None] = [None] * len(delta_cheats)

    def take(predicate) -> cheats_module.Cheat | None:
        for candidate in remaining:
            if predicate(candidate):
                remaining.remove(candidate)
                return candidate
        return None

    for index, cheat in enumerate(delta_cheats):
        matched[index] = take(lambda c: c.name == cheat.name)

    for index, (cheat, source) in enumerate(zip(delta_cheats, sources)):
        if matched[index] is not None or state is None:
            continue
        agreed_name = state.cheat_name(source.get("identifier", ""))
        if agreed_name:
            matched[index] = take(lambda c: c.name == agreed_name)

    for index, (cheat, source) in enumerate(zip(delta_cheats, sources)):
        if matched[index] is not None:
            continue
        wanted = {cheats_module.canonical_code(cheat.code)}
        if state is not None:
            agreed_code = state.cheat_code(source.get("identifier", ""))
            if agreed_code:
                wanted.add(agreed_code)
        matched[index] = take(
            lambda c: cheats_module.canonical_code(c.code) in wanted
        )

    unpaired = [i for i, found in enumerate(matched) if found is None]
    if len(unpaired) == 1 and len(remaining) == 1:
        matched[unpaired[0]] = remaining.pop()

    return matched, remaining


def merge_cheat_actions(
    code: tuple[Action, str], name: tuple[Action, str]
) -> tuple[Action, str]:
    """Combine the code decision and the name decision for one cheat.

    A cheat can have been edited and renamed at once. If both point the same way
    that is simply a push or a pull carrying both changes; if they point
    different ways -- the code changed on the phone while the name changed on
    the desktop -- there is no answer that does not discard somebody's edit, so
    it is reported.
    """
    code_action, code_detail = code
    name_action, name_detail = name

    if code_action is name_action:
        if code_action is Action.NOTHING:
            return Action.NOTHING, "unchanged on both sides"
        if code_detail == name_detail:
            return code_action, code_detail
        return code_action, f"{code_detail} (code and name)"
    if code_action is Action.NOTHING:
        return name_action, f"renamed: {name_detail}"
    if name_action is Action.NOTHING:
        return code_action, code_detail
    return (
        Action.CONFLICT,
        f"the code and the name moved in different directions "
        f"(code {code_detail}; name {name_detail})",
    )


def decide_cheat(
    agreed: str | None, delta_code: str, retroarch_code: str
) -> tuple[Action, str]:
    """What to do about one cheat, from both sides and the last agreed code.

    The same four-way comparison ``decide`` makes for saves, on canonical codes
    rather than file hashes. Pure, so the matrix can be tested directly.

    The order matters. Equality is checked before history, because two sides that
    already agree need no history to prove it -- otherwise every cheat would
    report a conflict the first time this ran, having never been recorded before.
    """
    if delta_code == retroarch_code:
        return Action.NOTHING, "unchanged on both sides"
    if agreed is None:
        return Action.CONFLICT, "codes differ and there is no agreed history"

    delta_changed = delta_code != agreed
    retro_changed = retroarch_code != agreed
    if delta_changed and retro_changed:
        return Action.CONFLICT, "both sides changed since the last sync"
    if delta_changed:
        return Action.PULL, "changed in Delta"
    return Action.PUSH, "changed in RetroArch"


def sync_cheats(
    entry: inspect_module.GameEntry,
    game_cheats: list[dict[str, str]],
    cheat_dir: Path,
    *,
    delta_folder: Path | None = None,
    backup_dir: Path | None = None,
    state: manifest_module.Manifest | None = None,
    allow_push: bool = False,
    dry_run: bool = False,
) -> list[Outcome]:
    """Reconcile a game's cheats between Delta and RetroArch.

    Editing works both ways; creating does not. A `.cht` entry that matches a
    Delta cheat by name can be pushed home, because that cheat's record already
    exists and carries the Dropbox property groups Harmony needs. An entry with
    no match would need a brand-new record, and a file this tool creates has no
    property groups -- Harmony drops it from the listing silently. So new cheats
    are reported as unpushable, which is the honest answer rather than a write
    that appears to work and never reaches the phone.

    Pushing a cheat needs no Dropbox authorisation, unlike a save: a cheat record
    has no attached file and therefore no revision to read back.

    The `.cht` is only rewritten when nothing is in conflict. Rewriting it while
    one cheat disagreed would destroy the RetroArch-side edit before anyone could
    look at it, which is the silent overwrite this whole design exists to avoid.
    """
    if entry.system is None or not entry.system.retroarch_db_name:
        return []
    if not game_cheats:
        return []

    path = cheats_module.cheat_file_path(
        cheat_dir, entry.system.retroarch_db_name, entry.name
    )
    delta_cheats = [
        cheats_module.Cheat(
            name=cheat.get("name", ""),
            code=cheat.get("code", ""),
            type=cheat.get("type", ""),
        )
        for cheat in game_cheats
    ]

    existing: list[cheats_module.Cheat] = []
    if path.is_file():
        try:
            existing = cheats_module.parse_cheat_file(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            existing = []
    matched, orphans = match_cheats(delta_cheats, game_cheats, existing, state)

    outcomes: list[Outcome] = []
    conflicted = False
    pushed: list[tuple[str, str, str]] = []

    for cheat, source, counterpart in zip(delta_cheats, game_cheats, matched):
        identifier = source.get("identifier", "")
        if counterpart is None:
            # Nothing on the RetroArch side to compare against yet; the file
            # write below brings it into being.
            continue

        delta_code = cheats_module.canonical_code(cheat.code)
        retro_code = cheats_module.canonical_code(counterpart.code)
        agreed = state.cheat_code(identifier) if state is not None else None
        agreed_name = state.cheat_name(identifier) if state is not None else None

        action, detail = merge_cheat_actions(
            decide_cheat(agreed, delta_code, retro_code),
            decide_cheat(agreed_name, cheat.name, counterpart.name),
        )

        label = f"{entry.name}: cheat \"{cheat.name}\""

        if action is Action.NOTHING:
            if state is not None and not dry_run:
                state.record_cheat(identifier, delta_code, cheat.name)
            continue
        if action is Action.CONFLICT:
            conflicted = True
            outcomes.append(
                Outcome(
                    label,
                    Action.CONFLICT,
                    f"{detail}; neither side touched. Delta has "
                    f"{cheats_module.to_retroarch_code(cheat.code, cheat.type)}, "
                    f"RetroArch has {counterpart.code}",
                )
            )
            continue
        if action is Action.PULL:
            # Handled by rewriting the whole file below, which is how a `.cht`
            # is written at all -- there is no per-entry edit.
            continue

        # Push: RetroArch's copy is the newer one.
        if not allow_push:
            outcomes.append(
                Outcome(label, Action.PUSH, f"{detail}; not pushed (pass --push to enable)")
            )
            continue
        if dry_run:
            outcomes.append(Outcome(label, Action.PUSH, f"{detail} (dry run)"))
            continue
        if delta_folder is None or backup_dir is None or not identifier:
            continue

        new_code = cheats_module.to_delta_code(counterpart.code, cheat.type)
        # Only sent when it actually differs, so an unchanged name is never
        # rewritten and `push_cheat` can report what it really did.
        new_name = counterpart.name if counterpart.name != cheat.name else None
        try:
            note = delta_writer.push_cheat(
                delta_folder, identifier, new_code, backup_dir, new_name=new_name
            )
        except (OSError, ValueError) as error:
            outcomes.append(
                Outcome(label, Action.PUSH, f"{detail}; FAILED: {error}", failed=True)
            )
            conflicted = True
            continue

        pushed.append((identifier, retro_code, counterpart.name))
        outcomes.append(
            Outcome(label, Action.PUSH, f"{detail}; {note}", applied=True)
        )

    # A `.cht` entry that matched nothing is a cheat made in RetroArch. Worth
    # saying once per game rather than per entry, and worth saying at all:
    # silently ignoring it is what makes people think it synced.
    #
    # Taken from what the matcher could not pair rather than from a name
    # comparison, so a renamed cheat is no longer mistaken for a new one.
    if orphans:
        names = ", ".join(cheat.name for cheat in orphans)
        outcomes.append(
            Outcome(
                entry.name,
                Action.SKIPPED,
                f"{len(orphans)} cheat(s) exist only in RetroArch and cannot be "
                f"created in Delta ({names}). Make them on the phone "
                "and they will come the other way.",
            )
        )

    if conflicted:
        outcomes.append(
            Outcome(
                entry.name,
                Action.SKIPPED,
                f"{path.name} left as it is while a cheat is in conflict",
            )
        )
        return outcomes

    # Render from Delta's cheats, with anything just pushed folded in -- the
    # record on disk now holds the RetroArch code, so re-reading would be the
    # only alternative and this avoids a second pass over the folder.
    pushed_by_id = {identifier: (code, name) for identifier, code, name in pushed}
    rendered = [
        cheats_module.Cheat(
            name=pushed_by_id.get(source.get("identifier", ""), (None, cheat.name))[1],
            code=pushed_by_id.get(source.get("identifier", ""), (cheat.code, None))[0],
            type=cheat.type,
        )
        for cheat, source in zip(delta_cheats, game_cheats)
    ]

    if dry_run:
        # Report what a real run would actually do, not just what it would
        # consider. A dry run that claims a write it would skip is worse than
        # no dry run, because it teaches you to ignore the output.
        current = ""
        if path.is_file():
            try:
                current = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = ""
        if current != cheats_module.render(rendered):
            outcomes.append(
                Outcome(
                    entry.name,
                    Action.PULL,
                    f"would write {len(rendered)} cheat(s) to {path}",
                )
            )
        return outcomes

    changed = cheats_module.write_cheat_file(path, rendered)
    if changed:
        outcomes.append(
            Outcome(
                entry.name,
                Action.PULL,
                f"wrote {len(rendered)} cheat(s) to {path}",
                applied=True,
            )
        )

    # Both sides now hold the same thing, so record it as agreed. Done after the
    # file write rather than per cheat, because a cheat is only truly reconciled
    # once the `.cht` reflects it.
    if state is not None:
        for cheat, source in zip(rendered, game_cheats):
            identifier = source.get("identifier", "")
            if identifier:
                state.record_cheat(
                    identifier, cheats_module.canonical_code(cheat.code), cheat.name
                )

    return outcomes


def run_sync(
    paths: Paths,
    entries: list[inspect_module.GameEntry],
    core_name: str,
    sorted_by_core: bool,
    *,
    dry_run: bool = False,
    allow_push: bool = False,
    rom_dir: Path | None = None,
    playlist_dir: Path | None = None,
    dropbox: "dropbox_api.DropboxClient | None" = None,
    cheat_dir: Path | None = None,
    cheats_by_game: dict[str, list[dict[str, str]]] | None = None,
) -> SyncReport:
    """Reconcile every supported game. Returns what was done or would be done."""
    state = manifest_module.Manifest.load(paths.manifest_path)
    report = SyncReport()

    for entry in entries:
        if not entry.supported:
            # The one-line form, because this repeats on every sync for a system
            # that is permanently blocked. The full note is what `inspect`
            # prints, where it is asked for once rather than repeated forever.
            system = entry.system
            note = "system not enabled for sync"
            if system is not None:
                note = (
                    system.conversion_summary
                    or system.conversion_note
                    or note
                )
            report.outcomes.append(Outcome(entry.name, Action.SKIPPED, note))
            continue

        if rom_dir is not None:
            rom_outcome = sync_rom(entry, rom_dir, dry_run=dry_run)
            if rom_outcome is not None:
                report.outcomes.append(rom_outcome)

            # Registered whether or not the ROM was just copied: a game put
            # there by an earlier version, or by hand, is equally invisible
            # until it is in a playlist, and RetroArch shows content from
            # playlists rather than from directories.
            playlist_outcome = register_in_playlist(
                entry, rom_dir, playlist_dir, dry_run=dry_run
            )
            if playlist_outcome is not None:
                report.outcomes.append(playlist_outcome)

        if cheat_dir is not None and cheats_by_game is not None:
            report.outcomes.extend(
                sync_cheats(
                    entry,
                    cheats_by_game.get(entry.identifier, []),
                    cheat_dir,
                    delta_folder=paths.delta_folder,
                    backup_dir=paths.backup_dir,
                    state=state,
                    allow_push=allow_push,
                    dry_run=dry_run,
                )
            )

        target = retroarch_save_path(
            paths.save_dir, entry, core_name, sorted_by_core
        )

        # Checked before anything is decided: a core whose layout is unverified
        # must not have its save read *or* written, so this cannot sit inside
        # either branch.
        unverified = unverified_save_core(entry, core_name)
        if unverified is not None:
            report.outcomes.append(Outcome(entry.name, Action.SKIPPED, unverified))
            continue

        action, detail = decide(state.get(entry.identifier), entry.save_path, target)

        # Checked before the action rather than after it, so it is said whatever
        # happens -- including when there is nothing to sync, which is exactly
        # the case for a game whose progress lives only on a pak.
        if not dry_run:
            pak_notice = controller_pak_notice(entry, target, state)
            if pak_notice is not None:
                report.outcomes.append(pak_notice)

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
            staged: Path | None = None
            try:
                source, temporary = stage_for_delta(entry, target, paths.state_dir)
                staged = source if temporary else None
                note = push_with_revision(
                    paths, entry, source, dropbox
                )
            except (OSError, ValueError, dropbox_api.DropboxError) as error:
                report.outcomes.append(
                    Outcome(
                        entry.name,
                        Action.PUSH,
                        f"{detail}; FAILED: {error}",
                        failed=True,
                    )
                )
                continue
            finally:
                if staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        pass
            state.record(entry.identifier, entry.save_path, target)
            report.outcomes.append(
                Outcome(
                    entry.name,
                    Action.PUSH,
                    f"{detail}; {note}",
                    applied=True,
                )
            )
            clock_outcome = push_clock(
                paths, entry, target, core_name, dropbox, dry_run=dry_run
            )
            if clock_outcome is not None:
                report.outcomes.append(clock_outcome)
            continue

        if action is not Action.PULL:
            report.outcomes.append(Outcome(entry.name, action, detail))
            continue

        if dry_run:
            report.outcomes.append(
                Outcome(entry.name, Action.PULL, f"{detail} (dry run)")
            )
            clock_outcome = pull_clock(
                entry, target, core_name, paths.backup_dir, dry_run=True
            )
            if clock_outcome is not None:
                report.outcomes.append(clock_outcome)
            continue

        assert entry.save_path is not None
        saved = backup(target, paths.backup_dir)
        try:
            written = write_to_retroarch(entry, entry.save_path, target)
        except (OSError, ValueError) as error:
            report.outcomes.append(
                Outcome(entry.name, Action.PULL, f"FAILED: {error}", failed=True)
            )
            continue
        state.record(entry.identifier, entry.save_path, target)

        note = f"{detail}; {written}"
        if saved is not None:
            note += f" (previous version backed up)"
        report.outcomes.append(Outcome(entry.name, Action.PULL, note, applied=True))

        pak_notice = controller_pak_notice(entry, target, state)
        if pak_notice is not None:
            report.outcomes.append(pak_notice)

        clock_outcome = pull_clock(
            entry, target, core_name, paths.backup_dir, dry_run=dry_run
        )
        if clock_outcome is not None:
            report.outcomes.append(clock_outcome)

    if not dry_run:
        state.save()
    return report


# --- standalone emulators ---------------------------------------------------
#
# The same reconcile, against an emulator that owns its own save format. Kept
# apart from `run_sync` rather than folded into it because the standalone path
# genuinely has less to do -- no core name deciding the folder, no combined
# `.srm` to merge into, no playlist, no cheat database -- and because the one
# thing it has that RetroArch does not is a shape check on the file it is about
# to overwrite. See `emulators.check_shape`.


def emulator_search_dirs(
    installed: emulators.Installed, rom_dir: Path | None
) -> list[Path]:
    """Where a save for this emulator could already be sitting.

    Deliberately a short list of named folders rather than a walk. These are
    ordinary user folders -- someone's ROM library is plausibly tens of
    thousands of files across a network drive -- and the question being asked is
    only "has this emulator written here before", which its own folders and the
    ROM folder answer.
    """
    directories: list[Path] = []
    for candidate in (
        rom_dir,
        installed.install_dir,
        *(
            emulators._expand(template, installed.install_dir)
            for template in installed.emulator.save_dirs
        ),
    ):
        if candidate is not None and candidate not in directories:
            directories.append(candidate)
    return directories


def find_emulator_save(
    installed: emulators.Installed,
    entry: inspect_module.GameEntry,
    rom_dir: Path | None,
) -> Path | None:
    """An existing save this emulator has already written for this game.

    Worth more than any constructed path: it is the machine saying where this
    emulator writes, rather than this table saying where it ought to. Every
    extension the emulator could use is checked, because for N64 the extension
    depends on the cartridge and Delta may have no save to name it.
    """
    system = entry.system
    if system is None or not installed.emulator.handles(system.key):
        return None

    layout = installed.emulator.saves[system.key]
    extensions = (
        (layout.extension,)
        if layout.extension
        else tuple(emulators.N64_EXTENSIONS.values())
    )

    for directory in emulator_search_dirs(installed, rom_dir):
        if not directory.is_dir():
            continue
        for extension in extensions:
            candidate = directory / naming.save_filename(entry.name, extension)
            if candidate.is_file():
                return candidate
    return None


def emulator_target(
    installed: emulators.Installed,
    entry: inspect_module.GameEntry,
    rom_dir: Path | None,
    *,
    override: Path | None = None,
) -> tuple[Path | None, str]:
    """Where this game's save belongs for this emulator, and why -- or why not.

    The filename always comes from Delta's save size when Delta has a save, even
    when an existing file was found under a different one. For N64 that size
    names the cartridge's storage type, so it is the authority on which of
    ``.eep`` / ``.sra`` / ``.fla`` this cartridge uses; a file on disk with a
    different extension belongs to a different dump, not to this save. The
    existing file is used to locate the *folder* and nothing else.
    """
    system = entry.system
    assert system is not None

    existing = find_emulator_save(installed, entry, rom_dir)
    location = emulators.resolve_save_dir(
        installed, rom_dir, override=override, observed=existing
    )
    if location.directory is None:
        return None, location.source

    if entry.save_path is not None and entry.save_path.is_file():
        try:
            extension = installed.emulator.extension_for(
                system.key, entry.save_path.stat().st_size
            )
        except ValueError as error:
            return None, str(error)
    elif existing is not None:
        # Nothing from Delta to name the file, so the file that is already
        # there names itself. This is the push direction.
        extension = existing.suffix.lstrip(".")
    else:
        return None, "neither side has a save"

    filename = naming.save_filename(entry.name, extension)
    return location.directory / filename, location.source


def sync_emulator(
    paths: Paths,
    entry: inspect_module.GameEntry,
    installed: emulators.Installed,
    *,
    rom_dir: Path | None = None,
    override: Path | None = None,
    dry_run: bool = False,
    allow_push: bool = False,
    dropbox: "dropbox_api.DropboxClient | None" = None,
    state: manifest_module.Manifest | None = None,
) -> list[Outcome]:
    """Reconcile one game between Delta and one standalone emulator.

    Returns the outcomes rather than a report, so a caller syncing several
    emulators can gather them into one.
    """
    emulator = installed.emulator
    system = entry.system
    name = f"{entry.name} [{emulator.name}]"

    def made(action: Action, detail: str, **flags: bool) -> Outcome:
        """One outcome, already naming the emulator it is about.

        Every outcome below goes through this. ``Action.PULL``'s own wording is
        "pull: Delta -> RetroArch", which was true of every outcome this program
        produced until standalone emulators became a target -- so a save copied
        into mGBA reported a program that had not been touched. Constructing
        them in one place is what keeps a branch added later from reintroducing
        that.
        """
        return Outcome(name, action, detail, target=emulator.name, **flags)

    if system is None or not entry.supported:
        return [made(Action.SKIPPED, "system not enabled for sync")]

    blocked = emulators.blocked_reason(emulator, system.key)
    if blocked is not None:
        return [made(Action.SKIPPED, blocked)]

    target, why = emulator_target(installed, entry, rom_dir, override=override)
    if target is None:
        return [made(Action.SKIPPED, why)]

    if state is None:
        state = manifest_module.Manifest.load(paths.manifest_path)
        owned = True
    else:
        owned = False

    # Before the decision, not inside the pull branch. Nobody here has run this
    # emulator, so what it writes is unknown -- except where it has already
    # written something, which is evidence and is treated as such.
    #
    # It comes first because a shape mismatch outranks every action `decide`
    # could return, including a conflict. "Both sides changed, resolve it by
    # hand" invites someone to pick a side, and picking either side is wrong
    # when the two files are not the same kind of file: this emulator does not
    # store saves the way Delta does, and no choice between them fixes that.
    if target.is_file() and entry.save_path is not None and entry.save_path.is_file():
        try:
            mismatch = emulators.check_shape(
                target.read_bytes(), entry.save_path.read_bytes()
            )
        except OSError as error:
            return [made(Action.SKIPPED, f"FAILED: {error}", failed=True)]
        if mismatch is not None:
            return [
                made(
                    Action.SKIPPED,
                    f"not written: {mismatch}. Nothing was changed.",
                )
            ]

    action, detail = decide(
        state.get(entry.identifier),
        entry.save_path,
        target,
        target=emulator.key,
        label=emulator.name,
    )

    outcomes: list[Outcome] = []

    if action is Action.PULL:
        assert entry.save_path is not None

        if dry_run:
            return [made(Action.PULL, f"{detail}; would write {target}")]

        saved = backup(target, paths.emulator_backup_dir(emulator.key))
        try:
            copy_atomically(entry.save_path, target)
        except OSError as error:
            return [made(Action.PULL, f"FAILED: {error}", failed=True)]
        state.record(entry.identifier, entry.save_path, target, emulator.key)

        note = f"{detail}; copied to {target} ({why})"
        if saved is not None:
            note += " (previous version backed up)"
        outcomes.append(made(Action.PULL, note, applied=True))

    elif action is Action.PUSH:
        if not allow_push:
            outcomes.append(
                made(Action.PUSH, f"{detail}; not pushed (pass --push to enable)")
            )
        elif dry_run:
            outcomes.append(made(Action.PUSH, f"{detail} (dry run)"))
        elif dropbox is None:
            outcomes.append(
                made(Action.PUSH, f"{detail}; {delta_writer.REVISION_MUST_BE_REAL}")
            )
        else:
            # No staging step, unlike RetroArch. A standalone emulator stores one
            # cartridge storage per file, which is already the shape Delta wants,
            # so what is on disk is what goes home -- including for N64, where
            # the RetroArch path has to extract a region from the combined .srm.
            try:
                pushed = push_with_revision(paths, entry, target, dropbox)
            except (OSError, ValueError, dropbox_api.DropboxError) as error:
                outcomes.append(
                    made(Action.PUSH, f"{detail}; FAILED: {error}", failed=True)
                )
            else:
                state.record(entry.identifier, entry.save_path, target, emulator.key)
                outcomes.append(made(Action.PUSH, f"{detail}; {pushed}", applied=True))

    else:
        outcomes.append(made(action, detail))

    # Said once per game per emulator rather than every run, like the pak notice.
    if emulator.note and not state.already_said(f"{emulator.key}-note"):
        outcomes.append(made(Action.NOTHING, emulator.note))
        if not dry_run:
            state.record_said(f"{emulator.key}-note")

    if owned and not dry_run:
        state.save()
    return outcomes
