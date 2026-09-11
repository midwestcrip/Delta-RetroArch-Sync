"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config as config_module
from . import delta_writer, discovery, dropbox_api, health, paths, restore, savestate
from . import inspect as inspect_module
from . import emulators as emulators_module
from . import manifest
from . import sync as sync_module
from . import systems


def _force_utf8_output() -> None:
    """Print UTF-8 regardless of the console code page.

    Windows consoles default to cp1252 here, which cannot encode the game names
    Delta stores -- "Pokemon" is spelled with an e-acute and would either be
    mangled or raise UnicodeEncodeError mid-report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _resolve() -> tuple[Path, Path] | None:
    """Locate both sides, printing why if either is missing."""
    config = config_module.load()
    delta = inspect_module._override(
        config.delta_folder, "Delta Emulator folder"
    ) or discovery.find_delta_folder()
    retro = inspect_module._override(
        config.retroarch_config, "retroarch.cfg"
    ) or discovery.find_retroarch_config()

    for found in (delta, retro):
        if not found.found:
            print(f"  [MISS] {found.label}: {found.detail}")
    if delta.path is None or retro.path is None:
        return None
    return delta.path, retro.path


def _core_for(system_cores: tuple[str, ...], installed: dict[str, str]) -> str | None:
    """Pick the installed core RetroArch would use for a system."""
    for name in system_cores:
        if name in installed:
            return name
    return None


def token_path() -> Path:
    return paths.state_dir() / dropbox_api.TOKEN_FILENAME


def load_dropbox() -> "dropbox_api.DropboxClient | None":
    credentials = dropbox_api.Credentials.load(token_path())
    return dropbox_api.DropboxClient(credentials) if credentials else None


def pending_path() -> Path:
    return paths.state_dir() / ".dropbox-auth-pending.json"


def run_auth_command(app_key: str | None, code: str | None) -> int:
    """One-time Dropbox authorisation, needed only for pushing.

    Split into two invocations rather than one interactive prompt, because the
    browser step happens outside this process anyway and a blocking `input()`
    makes the command unusable from any non-interactive context.

    Uses PKCE with no client secret, which is the correct flow for a desktop
    app: a secret shipped in source is not a secret. The app's *secret* is never
    needed and should never be pasted anywhere.
    """
    import json

    if code:
        try:
            pending = json.loads(pending_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print("No pending authorisation. Run `auth --app-key <key>` first.")
            return 1

        try:
            credentials = dropbox_api.exchange_code(
                pending["app_key"], pending["verifier"], code
            )
        except (dropbox_api.DropboxError, KeyError) as error:
            print(f"Authorisation failed: {error}")
            return 1

        credentials.save(token_path())
        pending_path().unlink(missing_ok=True)
        print(f"Authorised. Token saved to {token_path().name} (gitignored).")
        print("Pushing is now available: sync --push")
        return 0

    if not app_key:
        for line in (
            "A Dropbox app key is required.",
            "",
            "  1. https://www.dropbox.com/developers/apps -> Create app",
            "  2. Scoped access -> Full Dropbox",
            "  3. Permissions tab -> tick files.metadata.read -> Submit",
            "  4. Copy the App key from Settings (NOT the app secret --",
            "     PKCE does not use it and it should never be shared)",
            "",
            "Then run:  auth --app-key <key>",
        ):
            print(line)
        return 1

    verifier = dropbox_api.make_verifier()
    # The verifier must survive until the code comes back, and it is a
    # short-lived secret, so it lives in a gitignored file next to the token.
    pending_path().write_text(
        json.dumps({"app_key": app_key, "verifier": verifier}), encoding="utf-8"
    )

    print("Open this URL and approve access:")
    print()
    print(f"  {dropbox_api.build_authorize_url(app_key, verifier)}")
    print()
    print("Then run:  auth --code <the code Dropbox shows you>")
    return 0


def run_doctor_command() -> int:
    """Report whether each game's pushed state is one Delta can act on."""
    resolved = _resolve()
    if resolved is None:
        return 1
    delta_folder, retroarch_config = resolved

    dropbox = load_dropbox()
    if dropbox is None:
        print("Dropbox not authorised: revision checks will be skipped.")
        print()

    activity = health.delta_activity(delta_folder)
    age = activity.age_seconds(time.time())
    if age is None:
        print("Delta has not dated anything in this folder.")
    else:
        print(f"Delta last wrote {health.describe_age(age)} ago.")
    # Only worth suggesting when the number could plausibly be wrong. Saying
    # "if that is older than you expect" under a figure of two minutes just
    # trains people to skip the line.
    if age is None or age > health.SUSPICIOUS_SILENCE_SECONDS:
        print(
            "  If that is older than you expect, Delta may be signed into a "
            "different Dropbox\n"
            "  account than this folder belongs to. Check Settings -> Delta "
            "Sync on your phone."
        )
    print()

    # The clock check needs the RetroArch side, which doctor did not previously
    # look at. Resolved once, here, rather than per game.
    settings = discovery.parse_retroarch_config(retroarch_config)
    save_dir = discovery.resolve_retroarch_dir(
        settings, "savefile_directory", retroarch_config, "saves"
    )
    sorted_by_core = discovery.truthy(settings, "sort_savefiles_enable")
    installed = discovery.installed_cores(retroarch_config, settings)

    entries = inspect_module.collect_games(delta_folder)
    failures = 0
    for entry in entries:
        if entry.save_path is None:
            continue
        print(f"{entry.name}")

        retroarch_clock: Path | None = None
        clock_note = ""
        system = entry.system
        if system is not None and system.delta_clock_id:
            core = _core_for(system.retroarch_cores, installed) or ""
            if not core:
                clock_note = "no core installed, so nothing to compare against"
            elif core not in system.clock_cores:
                clock_note = (
                    f"not synced on {core}: its format has not been verified. "
                    f"Use {system.clock_cores[0]} for the clock to travel too."
                )
            else:
                target = sync_module.retroarch_save_path(
                    save_dir, entry, core, sorted_by_core
                )
                retroarch_clock = sync_module.clock.retroarch_clock_path(
                    target, system.retroarch_clock_ext
                )

        report = health.check_game(
            delta_folder,
            entry.identifier,
            dropbox,
            retroarch_clock=retroarch_clock,
            clock_core_note=clock_note,
        )
        for check in report.checks:
            mark = "ok  " if check.ok else "FAIL"
            print(f"  [{mark}] {check.name}: {check.detail}")
        if not report.ok:
            failures += 1
        print()

    if failures:
        print(f"{failures} game(s) in a state Delta cannot act on.")
        return 2
    print("All checks passed.")
    return 0


def _restore_context() -> tuple[Path, Path, Path, list, dict[str, str]] | None:
    """Everything both backup commands need: folders, saves and game names."""
    resolved = _resolve()
    if resolved is None:
        return None
    delta_folder, retroarch_config = resolved

    settings = discovery.parse_retroarch_config(retroarch_config)
    save_dir = discovery.resolve_retroarch_dir(
        settings, "savefile_directory", retroarch_config, "saves"
    )
    entries = inspect_module.collect_games(delta_folder)
    names = {entry.identifier: entry.name for entry in entries}
    return delta_folder, retroarch_config, save_dir, entries, names


def run_backups_command() -> int:
    """List what can be put back, newest first."""
    context = _restore_context()
    if context is None:
        return 1
    _, _, _, _, names = context

    backup_dir = sync_module.Paths(
        delta_folder=Path(), retroarch_config=Path(), save_dir=Path(),
        state_dir=paths.state_dir(),
    ).backup_dir

    found = restore.scan(backup_dir)
    points = restore.restore_points(found, names)
    if not points:
        print(f"No backups yet in {backup_dir}.")
        print("One is taken automatically before anything is overwritten.")
        return 0

    print(f"\nBackups in {backup_dir}\n")
    for index, point in enumerate(points, start=1):
        print(f"  [{index:>3}] {point.describe()}")

    skipped = len(found) - len(points)
    if skipped:
        print(
            f"\n  ({skipped} record file(s) also backed up. They are restored "
            "with the save they belong to, not on their own.)"
        )
    print("\nPut one back with:  restore <number> --yes")
    return 0


def run_restore_command(number: int, confirmed: bool) -> int:
    """Put one backup back. Prints what it would do unless --yes is given.

    Deliberately not an interactive prompt. Every other command here works
    without a terminal, and a restore is exactly the operation someone might
    script after a bad sync -- so consent is a flag, and without it this is a
    dry run that says precisely what would change.
    """
    context = _restore_context()
    if context is None:
        return 1
    delta_folder, retroarch_config, save_dir, entries, names = context

    sync_paths = sync_module.Paths(
        delta_folder=delta_folder,
        retroarch_config=retroarch_config,
        save_dir=save_dir,
        state_dir=paths.state_dir(),
    )
    points = restore.restore_points(restore.scan(sync_paths.backup_dir), names)
    if not 1 <= number <= len(points):
        print(f"No backup number {number}. Run `backups` to see the list.")
        return 1

    point = points[number - 1]
    print(f"\n  {point.describe()}")

    if not point.backup.is_delta:
        target = restore.find_target(point, save_dir, config_module.load())
        destination = str(target) if target else "(not found -- restore will fail)"
        print(f"  would overwrite: {destination}")
    else:
        print(f"  would write into Delta's folder: {point.backup.original_name}")

    if not confirmed:
        print(f"\n  Nothing written. Re-run with --yes to do it.")
        print(f"  Afterwards, {restore.RESTORE_IS_A_CHANGE}.")
        return 0

    try:
        if not point.backup.is_delta:
            note = restore.restore_retroarch(
                point, save_dir, sync_paths.backup_dir, config_module.load()
            )
        elif point.backup.kind == "cheat":
            note = restore.restore_delta_cheat(
                point, delta_folder, sync_paths.backup_dir
            )
        else:
            entry = next(
                (e for e in entries if e.identifier == point.backup.identifier),
                None,
            )
            if entry is None:
                print(
                    "\n  Delta no longer has this game, so there is no record to "
                    "write the restored save into."
                )
                return 1
            dropbox = load_dropbox()
            if dropbox is None:
                print(f"\n  Not restored: {delta_writer.REVISION_MUST_BE_REAL}")
                return 1
            system = entry.system
            file_identifier = (
                system.delta_clock_id
                if point.backup.kind == "clock" and system and system.delta_clock_id
                else delta_writer.PRIMARY_FILE
            )
            note = restore.restore_delta_save(
                point, sync_paths, entry, dropbox, file_identifier=file_identifier
            )
    except (OSError, ValueError, dropbox_api.DropboxError) as error:
        print(f"\n  FAILED: {error}")
        return 1

    print(f"\n  {note}")
    print(f"  The previous version was backed up first, so this is undoable.")
    print(f"  Now sync: {restore.RESTORE_IS_A_CHANGE}.")
    return 0


def run_sync_command(dry_run: bool, allow_push: bool) -> int:
    resolved = _resolve()
    if resolved is None:
        return 1
    delta_folder, retroarch_config = resolved

    settings = discovery.parse_retroarch_config(retroarch_config)
    save_dir = discovery.resolve_retroarch_dir(
        settings, "savefile_directory", retroarch_config, "saves"
    )
    sorted_by_core = discovery.truthy(settings, "sort_savefiles_enable")
    installed = discovery.installed_cores(retroarch_config, settings)

    # RetroArch has no canonical ROM location, so default to a folder beside
    # the install and let config.toml override it.
    config = config_module.load()
    rom_dir = config.retroarch_rom_dir or (retroarch_config.parent / "roms")
    playlist_dir = discovery.resolve_retroarch_dir(
        settings, "playlist_directory", retroarch_config, "playlists"
    )
    cheat_dir = discovery.resolve_retroarch_dir(
        settings, "cheat_database_path", retroarch_config, "cheats"
    )

    entries = inspect_module.collect_games(delta_folder)
    cheats_by_game = inspect_module.collect_cheats(delta_folder)
    if not entries:
        print("Nothing synced by Delta yet.")
        return 1

    # Every supported game must map to an installed core, because the core name
    # decides the save folder. Guessing it would write the save where RetroArch
    # will never look for it.
    missing = [
        entry
        for entry in entries
        if entry.supported
        and entry.system is not None
        and _core_for(entry.system.retroarch_cores, installed) is None
    ]
    for entry in missing:
        assert entry.system is not None
        print(f"  [MISS] {entry.name}: {systems.missing_core_advice(entry.system)}")

    syncable = [entry for entry in entries if entry not in missing]

    dropbox = load_dropbox() if allow_push else None

    # Deliberately not called `paths`: that name is the module imported at the
    # top of this file, and assigning it here made every earlier reference to
    # paths.state_dir() raise UnboundLocalError, because Python treats a name
    # assigned anywhere in a function as local throughout it. The CLI sync
    # command could not run at all.
    sync_paths = sync_module.Paths(
        delta_folder=delta_folder,
        retroarch_config=retroarch_config,
        save_dir=save_dir,
        state_dir=paths.state_dir(),
    )

    chosen = set(config.emulators_enabled)

    def one_pass() -> list[sync_module.Outcome]:
        """Reconcile every target once: RetroArch, then each enabled emulator."""
        outcomes: list[sync_module.Outcome] = []
        for entry in syncable:
            if entry.system is None:
                continue
            core = _core_for(entry.system.retroarch_cores, installed) or ""
            single = sync_module.run_sync(
                sync_paths,
                [entry],
                core,
                sorted_by_core,
                dry_run=dry_run,
                allow_push=allow_push,
                rom_dir=rom_dir,
                playlist_dir=playlist_dir,
                dropbox=dropbox,
                cheat_dir=cheat_dir,
                cheats_by_game=cheats_by_game,
            )
            outcomes.extend(single.outcomes)

        # Standalone emulators, after RetroArch and only for the ones turned on.
        # Each keeps its own agreement with Delta, so syncing to several is
        # several independent reconciles rather than one with several
        # destinations.
        if chosen:
            state = manifest.Manifest.load(sync_paths.manifest_path)
            # One per pass: two emulators for the same system can resolve to the
            # same file, and the second needs to say so rather than reconcile it.
            claimed: dict[Path, str] = {}
            for found in installed_emulators(config):
                if found.emulator.key not in chosen:
                    continue
                for entry in syncable:
                    if entry.system is None or not found.emulator.handles(
                        entry.system.key
                    ):
                        continue
                    outcomes.extend(
                        sync_module.sync_emulator(
                            sync_paths,
                            entry,
                            found,
                            rom_dir=rom_dir,
                            override=config.emulator_save_dirs.get(found.emulator.key),
                            dry_run=dry_run,
                            allow_push=allow_push,
                            dropbox=dropbox,
                            state=state,
                            claimed=claimed,
                        )
                    )
            if not dry_run:
                state.save()
        return outcomes

    report = sync_module.SyncReport()
    report.outcomes.extend(one_pass())
    report.outcomes.extend(
        sync_module.settle(one_pass, report.outcomes, dry_run=dry_run)
    )

    header = "Sync (dry run -- nothing written)" if dry_run else "Sync"
    print(f"\n{header}\n")
    for outcome in report.outcomes:
        mark = "*" if outcome.applied else " "
        print(f"  {mark} {outcome.game}")
        print(f"      {outcome.description}: {outcome.detail}")

    if report.conflicts:
        print(
            f"\n  {len(report.conflicts)} conflict(s) left untouched. "
            "Resolve by hand -- nothing was overwritten."
        )
        return 2
    if missing:
        return 1
    return 0


def installed_emulators(
    config: "config_module.Config",
) -> list["emulators_module.Installed"]:
    """Every standalone emulator found, including any the user pointed at.

    A path in config.toml may name either the executable or the folder holding
    it, because both are things someone reasonably types when asked where a
    program is.
    """
    extra: list[Path] = []
    for raw in config.emulator_paths.values():
        extra.append(raw.parent if raw.is_file() else raw)
    return emulators_module.find_installed(tuple(extra))


def run_emulators_command() -> int:
    """Report which standalone emulators are here and what each one would do.

    Reporting is unconditional and writing is opt-in, which is the shape the
    naive-user tests kept asking for: a feature nobody can see is one nobody
    turns on, and an emulator being installed is not a statement that saves
    should be written into it.
    """
    config = config_module.load()
    found = installed_emulators(config)

    print("\nStandalone emulators\n")
    if not found:
        print("  None found.")
        print(
            "  Most of these ship as a zip rather than an installer, so they "
            "are only\n  found once they have been run at least once. If you "
            "have one, name its\n  folder in config.toml:\n"
        )
        print("      [emulators.mgba]")
        print('      path = "C:/Emulators/mGBA"')
        return 1

    enabled = set(config.emulators_enabled)
    for installed in found:
        emulator = installed.emulator
        mark = "*" if emulator.key in enabled else " "
        print(f"  {mark} {emulator.name}  ({installed.executable})")

        for system_key in sorted(emulator.saves):
            system = systems.SYSTEMS[system_key]
            blocked = emulators_module.blocked_reason(emulator, system_key)
            if blocked is not None:
                print(f"      {system.name}: not synced -- {blocked}.")
            elif system_key not in systems.ENABLED_SYSTEMS:
                print(f"      {system.name}: not synced -- system not enabled.")
            else:
                print(f"      {system.name}: ready")

        location = emulators_module.resolve_save_dir(
            installed,
            config.retroarch_rom_dir,
            override=config.emulator_save_dirs.get(emulator.key),
        )
        if location.found:
            print(f"      saves: {location.directory} ({location.source})")
        else:
            print(f"      saves: {location.source}")

        if emulator.note:
            print(f"      note: {emulator.note}")
        if emulator.key not in enabled:
            print(f"      to sync to it, add \"{emulator.key}\" to:")
            print("          [emulators]")
            print('          enabled = [...]')
        print()

    if not enabled:
        print("  Nothing is enabled, so nothing is written to any of them.")
    return 0


def run_shortcuts_command(*, remove: bool) -> int:
    """Add or remove the shortcuts from the command line.

    The window offers this too. It is here because the window is not always the
    way in -- someone scripting a deployment, or running headless over SSH, has
    no button to press.
    """
    from . import shortcut

    if not shortcut.supported():
        print("Shortcuts like these are a Windows feature.")
        return 1

    try:
        changed = shortcut.remove() if remove else shortcut.create()
    except shortcut.ShortcutError as error:
        print(f"Failed: {error}")
        return 1

    if not changed:
        print("Nothing to remove; there were no shortcuts.")
        return 0

    print("Removed:" if remove else "Added:")
    for place, link in changed.items():
        print(f"  {place}: {link}")
    if not remove:
        print(
            "\nSearch the Start menu for \u201cDelta\u201d, "
            "or look on your desktop."
        )
    return 0


def run_extract_save_command(
    source: str, destination: str | None, force: bool, size: int | None = None
) -> int:
    """Lift the battery save out of a Delta save state.

    Deliberately not part of any sync pass. This reads one file the user names
    and writes one new file beside it; it never touches Delta's folder,
    RetroArch's saves, or the manifest. That keeps a last-resort recovery from
    being able to damage anything that is currently working.
    """
    state_path = Path(source).expanduser()
    if not state_path.is_file():
        print(f"No such file: {state_path}")
        return 1

    try:
        blob = state_path.read_bytes()
    except OSError as error:
        print(f"Could not read {state_path}: {error}")
        return 1

    try:
        # Only melonDS states record their own save length. The rest keep a
        # fixed-size buffer, so the length has to come from Delta's record.
        extracted = savestate.extract_battery_save(
            blob, expected_size=size or savestate.delta_save_size(state_path)
        )
    except savestate.SaveStateError as error:
        print(f"Cannot extract a save from {state_path.name}:\n  {error}")
        return 1

    # Never beside the state when the state is in Delta's synced folder: that
    # would put a new file into another app's storage and Dropbox would sync it
    # everywhere. suggested_output falls back to our own state directory.
    recovered_dir = paths.state_dir() / "recovered"
    target = (
        Path(destination).expanduser()
        if destination
        else savestate.suggested_output(state_path, recovered_dir)
    )
    if target.exists() and not force:
        print(f"{target} already exists. Nothing written.")
        print("Pass --force to overwrite it, or --output to write elsewhere.")
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(extracted.data)
    except OSError as error:
        print(f"Could not write {target}: {error}")
        return 1

    print(f"\nRecovered {extracted.size:,} B from {state_path.name}")
    print(f"  emulator:    {extracted.core}")
    print(f"  save type:   {extracted.save_type}")
    if extracted.cart_variant is not None:
        print(f"  cartridge:   {extracted.cart_variant}")
    if extracted.version is not None:
        print(
            f"  state format: version {extracted.version[0]}.{extracted.version[1]}"
        )
    if extracted.size_from_record:
        print("  length:      from Delta's record — the state does not say")
    print(f"  written to:  {target}")

    # Only possible when the state is still in Delta's synced folder, where the
    # records link it to its game and the game to its battery save. When it is,
    # this is the difference between "the file parsed" and "the extraction is
    # correct", so it is worth doing unasked.
    comparison = savestate.compare_with_delta(state_path, extracted.data)
    if comparison is not None:
        game = comparison.game_name or "this game"
        print(f"\nChecked against Delta's own save for {game}:")
        print(f"  {comparison.describe()}")
    else:
        print(
            "\nNo cross-check: this state is not sitting in Delta's synced "
            "folder beside its record, so there is nothing to compare against."
        )

    print(
        "\nThis is the raw battery save. Delta and RetroArch both take it as-is "
        "-- rename it to what the other side expects rather than converting it."
    )
    if comparison is None or not comparison.identical:
        print(
            "Check it before relying on it: the layout is read from melonDS's "
            "source and has not been verified against a real Delta state."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="delta-retroarch-synchronizer",
        description="Sync saves, ROMs and cheats between Delta and RetroArch.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "gui", help="Open the launcher window."
    )
    subparsers.add_parser(
        "inspect",
        help="Report what Delta and RetroArch have on disk. Writes nothing.",
    )
    subparsers.add_parser(
        "doctor",
        help="Check that pushed saves are in a state Delta can act on.",
    )
    sync_parser = subparsers.add_parser(
        "sync", help="Reconcile saves between Delta and RetroArch."
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing anything.",
    )
    subparsers.add_parser(
        "emulators",
        help="List standalone emulators found here and what each would sync.",
    )
    subparsers.add_parser(
        "backups", help="List the saves and cheats that can be put back."
    )
    restore_parser = subparsers.add_parser(
        "restore", help="Put one backup back. Shows what it would do without --yes."
    )
    restore_parser.add_argument(
        "number", type=int, help="Which backup, from the `backups` list."
    )
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually do it. Without this, nothing is written.",
    )
    shortcuts_parser = subparsers.add_parser(
        "shortcuts",
        help="Put this program in your Start menu and on your desktop.",
    )
    shortcuts_parser.add_argument(
        "--remove",
        action="store_true",
        help="Take the shortcuts back out again.",
    )
    extract_parser = subparsers.add_parser(
        "extract-save",
        help="Recover the battery save from a Delta save state (.svs). DS only.",
    )
    extract_parser.add_argument("state", help="Path to the .svs file.")
    extract_parser.add_argument(
        "--output",
        help="Where to write the .sav. Defaults to beside the state.",
    )
    extract_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output if it already exists.",
    )
    extract_parser.add_argument(
        "--size",
        type=int,
        help=(
            "How many bytes the save is. Only melonDS states say this "
            "themselves; the others store a fixed buffer, so a state outside "
            "Delta's folder needs the length given here."
        ),
    )
    auth_parser = subparsers.add_parser(
        "auth", help="Authorise Dropbox once, so pushing can read file revisions."
    )
    auth_parser.add_argument("--app-key", help="Dropbox app key (not the secret).")
    auth_parser.add_argument("--code", help="Authorisation code from the browser step.")
    sync_parser.add_argument(
        "--push",
        action="store_true",
        help=(
            "Also write RetroArch's newer saves back into Delta's Dropbox "
            "folder. Off by default: this is the only direction that writes to "
            "Delta, and it should be verified on a throwaway save first."
        ),
    )

    args = parser.parse_args(argv)
    _force_utf8_output()

    if args.command == "gui":
        from . import launcher

        return launcher.main()
    if args.command == "doctor":
        return run_doctor_command()
    if args.command == "inspect":
        return inspect_module.run()
    if args.command == "sync":
        return run_sync_command(dry_run=args.dry_run, allow_push=args.push)
    if args.command == "emulators":
        return run_emulators_command()
    if args.command == "backups":
        return run_backups_command()
    if args.command == "restore":
        return run_restore_command(args.number, args.yes)
    if args.command == "shortcuts":
        return run_shortcuts_command(remove=args.remove)
    if args.command == "extract-save":
        return run_extract_save_command(
            args.state, args.output, args.force, args.size
        )
    if args.command == "auth":
        return run_auth_command(args.app_key, args.code)

    # Double-clicking the shortcut passes no arguments, so the window is the
    # right default. The CLI is the secondary interface here.
    from . import launcher

    return launcher.main()


if __name__ == "__main__":
    sys.exit(main())
