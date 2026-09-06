"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as config_module
from . import discovery
from . import inspect as inspect_module
from . import sync as sync_module


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

    entries = inspect_module.collect_games(delta_folder)
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
        wanted = ", ".join(entry.system.retroarch_cores)
        print(
            f"  [MISS] no core installed for {entry.system.name} "
            f"({entry.name}). Install one of: {wanted}"
        )

    syncable = [entry for entry in entries if entry not in missing]
    state_dir = Path(__file__).resolve().parents[2]

    report = sync_module.SyncReport()
    for entry in syncable:
        if entry.system is None:
            continue
        core = _core_for(entry.system.retroarch_cores, installed) or ""
        paths = sync_module.Paths(
            delta_folder=delta_folder,
            retroarch_config=retroarch_config,
            save_dir=save_dir,
            state_dir=state_dir,
        )
        single = sync_module.run_sync(
            paths,
            [entry],
            core,
            sorted_by_core,
            dry_run=dry_run,
            allow_push=allow_push,
            rom_dir=rom_dir,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="delta-retroarch-sync",
        description="Sync saves, ROMs and cheats between Delta and RetroArch.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "inspect",
        help="Report what Delta and RetroArch have on disk. Writes nothing.",
    )
    sync_parser = subparsers.add_parser(
        "sync", help="Reconcile saves between Delta and RetroArch."
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing anything.",
    )
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

    if args.command == "inspect":
        return inspect_module.run()
    if args.command == "sync":
        return run_sync_command(dry_run=args.dry_run, allow_push=args.push)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
