"""Command line entry point.

Only the read-only inspector exists so far. Sync commands are added once the
inspector has confirmed the real folder layout on this machine.
"""

from __future__ import annotations

import argparse
import sys

from . import inspect as inspect_module


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

    args = parser.parse_args(argv)
    if args.command == "inspect":
        return inspect_module.run()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
