"""The Controller Pak program: one entry point, two ways in.

``--json`` puts it in the mode the main program drives it in -- JSON Lines on
stdout, one object per line, the last one ``{"type": "result", ...}``. Without
it, the same commands print for a person, and no arguments at all opens the
standalone window.

Every command works against ``--folder`` as well as a device. That is not a
testing affordance bolted on: copying Delta's Saves folder off by hand in
Explorer is a real way to use this, and it is the one that works when somebody
has no iTunes, no cable, or a phone that will not pair. It also means every
refusal, every message and every path through this file is exercised without a
phone in the room.

The one thing genuinely gated on hardware is whether ``house_arrest`` opens
Delta's container, and what the files inside are called. ``probe`` and ``list``
exist to answer exactly that, and neither guesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import PROTOCOL, __version__
from .sources import DeviceSource, FolderSource, SourceError


class Reporter:
    """Prints for a machine or for a person, and nothing else knows which."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def progress(self, message: str) -> None:
        if self.as_json:
            self._emit({"type": "progress", "message": message})
        else:
            print(message)

    def result(self, **fields) -> int:
        """The last line. Always printed, success or failure.

        The main program treats a run with no result line as a crash, so this
        is the one thing every path must reach.
        """
        payload = {"type": "result", "protocol": PROTOCOL, **fields}
        if self.as_json:
            self._emit(payload)
        else:
            self._human(payload)
        return 0 if payload.get("ok") else 1

    @staticmethod
    def _emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _human(payload: dict) -> None:
        if not payload.get("ok"):
            print(f"Failed: {payload.get('error', 'unknown error')}")
            return
        for key, value in payload.items():
            if key in ("type", "protocol", "ok"):
                continue
            if not isinstance(value, list):
                print(f"{key}: {value}")
                continue
            print(f"{key}:")
            for item in value:
                print(f"  {Reporter._line(item)}")

    @staticmethod
    def _line(item) -> str:
        """One list entry, for a person rather than for a parser.

        The JSON shapes are the contract and are not flattened for machines;
        printing them raw at a human is just laziness leaking through.
        """
        if isinstance(item, dict):
            if "check" in item:
                mark = "ok " if item.get("ok") else "!  "
                return f"{mark} {item['check']}: {item.get('detail', '')}"
            if "name" in item:
                return f"{item['name']}  ({item.get('size', 0):,} B)"
        return str(item)


def a_source(arguments) -> FolderSource | DeviceSource:
    # getattr throughout: the shared options use SUPPRESS so an unset one on a
    # subcommand cannot overwrite the same option given before it, which means
    # the attribute is genuinely absent rather than None.
    folder = getattr(arguments, "folder", None)
    if folder:
        return FolderSource(Path(folder))
    return DeviceSource(bundle_id=getattr(arguments, "bundle", None))


def command_probe(arguments, out: Reporter) -> int:
    """Report what is and is not in place, one line per thing.

    Deliberately never fails on a missing piece -- a probe that refuses because
    there is no phone tells you nothing you did not know. It answers "what is
    stopping me", which needs all of the answers, not the first one.
    """
    checks: list[dict] = []

    def check(name: str, detail: str, ok: bool) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    try:
        DeviceSource.library()
        check("pymobiledevice3", "installed", True)
    except SourceError as error:
        check("pymobiledevice3", str(error), False)
        return out.result(
            ok=True, ready=False, checks=checks, version=__version__
        )

    try:
        devices = DeviceSource.devices()
    except SourceError as error:
        check("Apple device service", str(error), False)
        return out.result(
            ok=True, ready=False, checks=checks, version=__version__
        )

    check("Apple device service", "running", True)
    if not devices:
        check(
            "iPhone",
            "none connected — plug one in, unlock it, and answer Trust",
            False,
        )
        return out.result(
            ok=True, ready=False, checks=checks, version=__version__
        )
    check("iPhone", f"{len(devices)} connected", True)

    try:
        apps = DeviceSource.installed_apps()
        bundle = DeviceSource.find_delta(apps)
    except Exception as error:  # noqa: BLE001 -- any transport failure
        check("Delta", f"could not list installed apps: {error}", False)
        return out.result(
            ok=True, ready=False, checks=checks, version=__version__
        )

    if not bundle:
        check("Delta", "not installed on this device", False)
        return out.result(
            ok=True, ready=False, checks=checks, version=__version__
        )

    check("Delta", f"found as {bundle}", True)
    return out.result(
        ok=True, ready=True, bundle=bundle, checks=checks, version=__version__
    )


def command_list(arguments, out: Reporter) -> int:
    """Every file where the paks live, with its size.

    This is the measurement. What Delta's build actually names its ``.mpk``
    files -- one bundle of four or one file per controller, and what the stem
    looks like -- is not recorded anywhere reliable, and the reader in the main
    program tells the two shapes apart by size for exactly that reason. This
    prints what is there rather than what was expected.
    """
    source = a_source(arguments)
    out.progress(f"reading {source.describe()}")
    try:
        files = source.listing()
    except SourceError as error:
        return out.result(ok=False, error=str(error))

    return out.result(
        ok=True,
        source=source.describe(),
        files=[item.as_json() for item in files],
        count=len(files),
    )


def command_pull(arguments, out: Reporter) -> int:
    """Copy the pak files into a folder for the main program to merge."""
    source = a_source(arguments)
    destination = Path(arguments.into)

    try:
        files = source.listing()
    except SourceError as error:
        return out.result(ok=False, error=str(error))

    wanted = [item for item in files if item.name.lower().endswith(tuple(SUFFIXES))]
    if not wanted:
        return out.result(
            ok=False,
            error=(
                f"no Controller Pak files in {source.describe()}. Delta only "
                f"writes them once an N64 game has been played."
            ),
        )

    out.progress(
        f"copying {len(wanted)} file(s), "
        f"{sum(item.size for item in wanted):,} B"
    )
    # One call, not one per file. Against a device each call is a fresh usbmux
    # handshake and container vend, so reading four paks separately pays that
    # four times over for no reason.
    try:
        fetched = source.read_many([item.name for item in wanted])
    except (SourceError, OSError) as error:
        return out.result(ok=False, error=str(error))

    destination.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for item in wanted:
        try:
            (destination / item.name).write_bytes(fetched[item.name])
        except OSError as error:
            return out.result(ok=False, error=f"could not write {item.name}: {error}")
        written.append(item.name)

    return out.result(
        ok=True, into=str(destination), files=written, count=len(written)
    )


def command_push(arguments, out: Reporter) -> int:
    """Copy pak files back, which is the half that writes to the phone."""
    source = a_source(arguments)
    folder = Path(getattr(arguments, "from"))

    if not folder.is_dir():
        return out.result(ok=False, error=f"{folder} is not a folder")

    sending = sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.name.lower().endswith(tuple(SUFFIXES))
    )
    if not sending:
        return out.result(ok=False, error=f"no Controller Pak files in {folder}")

    out.progress(f"sending {len(sending)} file(s)")
    try:
        source.write_many({path.name: path.read_bytes() for path in sending})
    except (SourceError, OSError) as error:
        return out.result(ok=False, error=str(error))

    written = [path.name for path in sending]
    return out.result(ok=True, files=written, count=len(written))


#: What counts as a pak file by name. The *contents* are checked by size in the
#: main program, which is the check that matters; this only decides what is
#: worth copying rather than what is valid.
SUFFIXES = (".mpk", ".mpk1", ".mpk2", ".mpk3", ".mpk4")

COMMANDS = {
    "probe": command_probe,
    "list": command_list,
    "pull": command_pull,
    "push": command_push,
}


def build_parser() -> argparse.ArgumentParser:
    """Accepts the shared options on either side of the subcommand.

    ``prog list --folder X`` is what anyone types, and plain argparse rejects it
    -- options declared on the top-level parser are only accepted *before* the
    subcommand. So they are declared in both places, with
    ``default=argparse.SUPPRESS`` on the copy each subcommand inherits: without
    that, a subcommand's unset ``--folder`` overwrites the one given before it
    with ``None``, which is worse than the error it replaced because it looks
    like it worked and talks to the phone instead.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    common.add_argument(
        "--folder",
        default=argparse.SUPPRESS,
        help="read and write a folder on this PC instead of a connected phone",
    )
    common.add_argument(
        "--bundle",
        default=argparse.SUPPRESS,
        help="Delta's bundle identifier, if it is not found automatically",
    )

    parser = argparse.ArgumentParser(
        prog="Delta-RetroArch Controller Pak",
        parents=[common],
        description=(
            "Move Nintendo 64 Controller Pak files between Delta on an iPhone "
            "and this PC. Delta does not sync these, so a cable is the only "
            "way — or copy the folder off by hand and use --folder."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "probe", parents=[common],
        help="report what is in place and what is missing",
    )
    sub.add_parser(
        "list", parents=[common], help="list the pak files, with their sizes"
    )

    pull = sub.add_parser(
        "pull", parents=[common], help="copy pak files off the phone"
    )
    pull.add_argument("--into", required=True, help="folder to write them into")

    push = sub.add_parser(
        "push", parents=[common], help="copy pak files back onto the phone"
    )
    push.add_argument("--from", required=True, help="folder to read them from")

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    out = Reporter(as_json=getattr(arguments, "json", False))

    if arguments.command is None:
        if getattr(arguments, "json", False):
            return out.result(ok=False, error="no command given")
        from .window import run_window

        return run_window()

    handler = COMMANDS[arguments.command]
    try:
        return handler(arguments, out)
    except SourceError as error:
        return out.result(ok=False, error=str(error))
    except Exception as error:  # noqa: BLE001
        # The main program reads a missing result line as a crash. An
        # unexpected failure is still an answer, and saying what it was beats
        # a traceback nobody sees because stdout is a pipe.
        return out.result(ok=False, error=f"unexpected failure: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
