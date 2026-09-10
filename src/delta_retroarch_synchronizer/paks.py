"""Controller Pak files on disk, and RetroArch's combined save.

The Controller Pak feature has two halves, and only one of them needs anything
unusual:

- **Getting the ``.mpk`` files off the phone** needs ``pymobiledevice3``, Apple's
  usbmux service, a cable and a trust pairing. That is the add-on's job -- see
  :mod:`addons` for why it is a separate download and a separate process.
- **Putting them into RetroArch's save, and taking them back out**, needs
  nothing at all. It is this module, it lives in the main program, and it has no
  dependency beyond the standard library.

Splitting it that way is the point. The component with the third-party
dependency in it moves files and nothing else; the writing -- with the rolling
backups, the size guards and the refusals -- stays here with every other write
this program makes. It also means the feature is not all-or-nothing: Delta sets
``UIFileSharingEnabled``, so the paks can be dragged off in Explorer or the
Files app by hand, and this half works on that folder exactly as it works on a
folder the add-on filled.

**Only the pak regions are ever written.** ``n64.to_retroarch`` writes the
cartridge save and refuses to touch the paks; this writes the paks and touches
nothing else. Every byte of the combined save has exactly one owner, and that is
what stops the two halves eating each other's data.

**What the files look like is not settled yet.** mupen64plus builds differ:
some write one file holding all four paks, some write one file per controller.
Both shapes are read here, told apart by size rather than by name, because the
name is the part that varies. What Delta's own build writes is a measurement
waiting on a cable -- so this refuses anything it does not recognise instead of
assuming.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import n64

#: One file holding all four paks, which is what mupen64plus-core writes when it
#: keeps them together. Identical in size and layout to the pak region of
#: RetroArch's combined save, so moving between them is a straight copy.
BUNDLE_SIZE = n64.PAK_COUNT * n64.PAK_SIZE

#: Extensions worth looking at in a folder of pak files. ``.mpk1``-``.mpk4`` is
#: what standalone mupen64plus and Project64 write.
PAK_SUFFIXES = re.compile(r"\.mpk([1-4])?$", re.IGNORECASE)

#: A controller number in the **extension** -- ``.mpk1`` to ``.mpk4``. This is
#: the shape standalone mupen64plus and Project64 write, and the digit here can
#: only be a slot.
SLOT_IN_SUFFIX = re.compile(r"\.mpk([1-4])$", re.IGNORECASE)

#: A stem that is nothing but a pak word and a number -- ``mempak3``, ``pak2``,
#: ``controllerpak1``. Here too the digit cannot be part of a title.
SLOT_AS_WHOLE_STEM = re.compile(
    r"^(?:controller[\s_-]*pak|mem[\s_-]*pak|pak|mpk)[\s_-]*([1-4])$",
    re.IGNORECASE,
)


class PakError(Exception):
    """Controller Pak data that cannot be used, with the reason."""


@dataclass(frozen=True)
class Pak:
    """One Controller Pak, and where it came from."""

    slot: int
    data: bytes
    source: str

    @property
    def empty(self) -> bool:
        return n64.controller_pak_is_empty(self.data)

    @property
    def label(self) -> str:
        return f"Controller Pak {self.slot + 1}"

    def describe(self) -> str:
        state = "empty" if self.empty else "has saved notes"
        return f"{self.label}: {state}  ({self.source})"


@dataclass(frozen=True)
class PakSet:
    """Whatever paks were found, keyed by controller slot."""

    paks: tuple[Pak, ...]

    def __bool__(self) -> bool:
        return bool(self.paks)

    @property
    def slots(self) -> tuple[int, ...]:
        return tuple(pak.slot for pak in self.paks)

    def with_data(self) -> tuple[Pak, ...]:
        """The ones actually worth writing."""
        return tuple(pak for pak in self.paks if not pak.empty)

    def as_mapping(self) -> dict[int, bytes]:
        return {pak.slot: pak.data for pak in self.paks}

    def describe(self) -> str:
        if not self.paks:
            return "no Controller Pak data"
        written = len(self.with_data())
        return (
            f"{len(self.paks)} Controller Pak(s), {written} with saved notes"
        )


def _slot_from_name(path: Path) -> int | None:
    """Which controller a single-pak file belongs to, from its name.

    **A digit in a game's title is not a slot number**, and an earlier version
    of this read any trailing 1-4 as one. mupen64plus names its files after the
    ROM, so ``Mario Kart 64.mpk`` was read as Controller Pak *4* and
    ``Doom 64.mpk`` likewise -- both of them games whose only storage is the
    Controller Pak, so the data landing in the wrong controller is the whole
    save going missing. ``Turok 2``, ``Extreme-G XG2`` and ``Quake II`` are the
    same trap.

    So a digit counts only where it cannot be part of a title: in the extension
    (``.mpk3``), or as the entire stem beside a pak word (``mempak3``).
    Anything else returns ``None``, meaning "the name does not say" -- which a
    lone file answers by being the only one, and several files answer by being
    refused. Refusing costs a rename; guessing costs a save.
    """
    suffix = SLOT_IN_SUFFIX.search(path.name)
    if suffix:
        return int(suffix.group(1)) - 1

    whole = SLOT_AS_WHOLE_STEM.match(path.stem)
    if whole:
        return int(whole.group(1)) - 1

    return None


def split_bundle(data: bytes, source: str) -> list[Pak]:
    """The four paks inside a single all-in-one file."""
    if len(data) != BUNDLE_SIZE:
        raise PakError(
            f"{source} is {len(data):,} bytes; a four-pak file is "
            f"{BUNDLE_SIZE:,}"
        )
    return [
        Pak(
            slot=index,
            data=data[index * n64.PAK_SIZE : (index + 1) * n64.PAK_SIZE],
            source=source,
        )
        for index in range(n64.PAK_COUNT)
    ]


def read_folder(folder: Path) -> PakSet:
    """Every Controller Pak in a folder, however that folder names them.

    Told apart by **size**, not by name. The name is the part that varies
    between builds and between the two shapes; the sizes -- one pak, or four
    packed together -- are fixed by the hardware.
    """
    if not folder.is_dir():
        raise PakError(f"{folder} is not a folder")

    candidates = sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and PAK_SUFFIXES.search(path.name)
    )
    if not candidates:
        raise PakError(
            f"no Controller Pak files in {folder}. They are named .mpk, or "
            f".mpk1 to .mpk4."
        )

    singles: list[Path] = []
    found: dict[int, Pak] = {}

    for path in candidates:
        size = path.stat().st_size
        if size == BUNDLE_SIZE:
            for pak in split_bundle(path.read_bytes(), path.name):
                found.setdefault(pak.slot, pak)
        elif size == n64.PAK_SIZE:
            singles.append(path)
        else:
            raise PakError(
                f"{path.name} is {size:,} bytes, which is neither one "
                f"Controller Pak ({n64.PAK_SIZE:,}) nor four "
                f"({BUNDLE_SIZE:,}). Refusing to guess what it is."
            )

    for path in singles:
        slot = _slot_from_name(path)
        if slot is None:
            if len(singles) == 1 and not found:
                slot = 0
            else:
                raise PakError(
                    f"{path.name} is one Controller Pak but its name does not "
                    f"say which controller. Rename it to end in 1, 2, 3 or 4."
                )
        if slot in found:
            raise PakError(
                f"two files both claim Controller Pak {slot + 1}: "
                f"{found[slot].source} and {path.name}"
            )
        found[slot] = Pak(slot=slot, data=path.read_bytes(), source=path.name)

    return PakSet(tuple(found[slot] for slot in sorted(found)))


@dataclass(frozen=True)
class PakInstallPlan:
    """What installing these paks into a combined save would change."""

    target: Path
    writing: tuple[Pak, ...]
    #: Slots skipped because the incoming pak is blank and the save already has
    #: something in that slot. Never silently overwritten -- see below.
    protecting: tuple[Pak, ...]
    #: Slots skipped because they are blank on both sides. Nothing to say.
    already_blank: tuple[Pak, ...]
    creating: bool

    @property
    def changes_anything(self) -> bool:
        return bool(self.writing)

    def describe(self) -> str:
        lines = [f"{'Creating' if self.creating else 'Updating'} {self.target}"]
        for pak in self.writing:
            lines.append(f"  write   {pak.label}  ({pak.source})")
        for pak in self.protecting:
            lines.append(
                f"  keep    {pak.label} — the phone's is blank and this PC's "
                f"is not"
            )
        for pak in self.already_blank:
            lines.append(f"  skip    {pak.label} — blank on both sides")
        if not self.writing:
            lines.append("  nothing to write")
        return "\n".join(lines)


def plan_install(paks: PakSet, target: Path) -> PakInstallPlan:
    """Work out which pak slots would actually change.

    **A blank pak never overwrites one with data.** That is the rule this
    function exists for. A phone with an empty Controller Pak 2 and a desktop
    with a season of Mario Kart 64 ghosts in slot 2 is the ordinary case, not
    the exotic one, and a straight copy would erase them. The reverse -- writing
    a pak that has notes on it -- is what the person asked for.
    """
    if not paks:
        raise PakError("there are no Controller Paks to install")

    creating = not target.is_file()
    if creating:
        existing = n64.blank_srm()
    else:
        existing = target.read_bytes()
        if len(existing) != n64.SRM_SIZE:
            raise PakError(
                f"{target} is {len(existing):,} bytes, and a combined N64 save "
                f"is {n64.SRM_SIZE:,}. This is not a Mupen64Plus-Next save."
            )

    current = n64.controller_paks(existing)
    writing: list[Pak] = []
    protecting: list[Pak] = []
    already_blank: list[Pak] = []

    for pak in paks.paks:
        if not pak.empty:
            writing.append(pak)
        elif n64.controller_pak_is_empty(current[pak.slot]):
            already_blank.append(pak)
        else:
            protecting.append(pak)

    return PakInstallPlan(
        target=target,
        writing=tuple(writing),
        protecting=tuple(protecting),
        already_blank=tuple(already_blank),
        creating=creating,
    )


def install(plan: PakInstallPlan, backup_dir: Path) -> str:
    """Write the planned paks into the combined save, backing it up first.

    Goes through the same rolling backup as every other write here, so an
    install that turns out to be the wrong way round comes back off the Backups
    tab like anything else.
    """
    from . import sync

    if not plan.changes_anything:
        raise PakError(
            "none of these Controller Paks hold anything to write. Nothing "
            "was changed."
        )

    saved = sync.backup(plan.target, backup_dir)

    existing = (
        plan.target.read_bytes() if plan.target.is_file() else n64.blank_srm()
    )
    merged = n64.with_controller_paks(
        existing, {pak.slot: pak.data for pak in plan.writing}
    )

    plan.target.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan.target.with_name(plan.target.name + ".partial")
    try:
        temporary.write_bytes(merged)
        temporary.replace(plan.target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

    slots = ", ".join(str(pak.slot + 1) for pak in plan.writing)
    where = f"; previous save backed up as {saved.name}" if saved else ""
    return f"wrote Controller Pak {slots} into {plan.target.name}{where}"


def export(srm_path: Path, folder: Path, *, stem: str = "mempak") -> list[Path]:
    """Write the four paks out of a combined save as separate files.

    The other direction, and the one that makes the round trip possible: these
    are the files to put back on the phone. Written one per controller rather
    than as a bundle, because a single pak is the shape that can be dropped into
    a specific slot without disturbing the other three.
    """
    data = srm_path.read_bytes()
    if len(data) != n64.SRM_SIZE:
        raise PakError(
            f"{srm_path} is {len(data):,} bytes, and a combined N64 save is "
            f"{n64.SRM_SIZE:,}."
        )

    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, pak in enumerate(n64.controller_paks(data), start=1):
        out = folder / f"{stem}{index}.mpk"
        out.write_bytes(pak)
        written.append(out)
    return written
