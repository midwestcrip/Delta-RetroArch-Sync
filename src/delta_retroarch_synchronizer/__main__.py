"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config as config_module
from . import delta_writer, discovery, dropbox_api, health, paths, restore
from . import inspect as inspect_module
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

    if point.backup.side == restore.RETROARCH:
        target = restore.find_retroarch_target(
            save_dir, point.backup.original_name
        )
        destination = str(target) if target else "(not found -- restore will fail)"
        print(f"  would overwrite: {destination}")
    else:
        print(f"  would write into Delta's folder: {point.backup.original_name}")

    if not confirmed:
        print(f"\n  Nothing written. Re-run with --yes to do it.")
        print(f"  Afterwards, {restore.RESTORE_IS_A_CHANGE}.")
        return 0

    try:
        if point.backup.side == restore.RETROARCH:
            note = restore.restore_retroarch(
                point, save_dir, sync_paths.backup_dir
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

    report = sync_module.SyncReport()
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
        report.outcomes.extend(single.outcomes)

    header = "Sync (dry run -- nothing written)" if dry_run else "Sync"
    print(f"\n{header}\n")
    for outcome in report.outcomes:
        mark = "*" if outcome.applied else " "
        print(f"  {mark} {outcome.game}")
        print(f"      {outcome.action.value}: {outcome.detail}")

    if report.conflicts:
        print(
            f"\n  {len(report.conflicts)} conflict(s) left untouched. "
            "Resolve by hand -- nothing was overwritten."
        )
        return 2
    if missing:
        return 1
    return 0


def run_start_menu_command(*, remove: bool) -> int:
    """Add or remove the Start menu entry from the command line.

    The window offers this too. It is here because the window is not always the
    way in -- someone scripting a deployment, or running headless over SSH, has
    no button to press.
    """
    from . import shortcut

    if not shortcut.supported():
        print("The Start menu is a Windows feature.")
        return 1

    try:
        if remove:
            if not shortcut.remove():
                print("Nothing to remove; it was not in the Start menu.")
                return 0
            print("Removed from the Start menu.")
            return 0
        link = shortcut.create()
    except shortcut.ShortcutError as error:
        print(f"Failed: {error}")
        return 1

    print(f"Added: {link}")
    print("Search the Start menu for \u201cDelta\u201d to find it.")
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
    start_menu_parser = subparsers.add_parser(
        "start-menu",
        help="Add this program to your Start menu, so you can search for it.",
    )
    start_menu_parser.add_argument(
        "--remove",
        action="store_true",
        help="Take the Start menu entry back out again.",
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
    if args.command == "backups":
        return run_backups_command()
    if args.command == "restore":
        return run_restore_command(args.number, args.yes)
    if args.command == "start-menu":
        return run_start_menu_command(remove=args.remove)
    if args.command == "auth":
        return run_auth_command(args.app_key, args.code)

    # Double-clicking the shortcut passes no arguments, so the window is the
    # right default. The CLI is the secondary interface here.
    from . import launcher

    return launcher.main()


if __name__ == "__main__":
    sys.exit(main())
