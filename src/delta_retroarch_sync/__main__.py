"""Command line entry point.

Only the read-only inspector exists so far. Sync commands are added once the
inspector has confirmed the real folder layout on this machine.
"""

from __future__ import annotations

import argparse
import sys

from . import inspect as inspect_module


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
    _force_utf8_output()

    if args.command == "inspect":
        return inspect_module.run()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
