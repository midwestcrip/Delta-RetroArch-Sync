"""Standalone emulators as a sync target, alongside RetroArch.

RetroArch is one program with one config, so the whole of ``discovery.py`` can
ask it where its saves go. A standalone emulator is a different problem in three
ways, and only the first is about file formats:

1. **The emulator owns the save format, not the frontend.** RetroArch's frontend
   writes the ``.srm`` itself, which is why every system except N64 is a plain
   copy there *whatever core is loaded*. Nothing like that is true here: mGBA,
   Nestopia and DeSmuME each decide their own layout, so "it worked on RetroArch"
   carries no weight at all.
2. **There is usually no central save folder.** Most of these default to writing
   the save *beside the ROM*, and the ones that do not disagree about where.
3. **Most of them ship as a zip.** No installer, so no uninstall registry entry
   and no App Paths registration -- see :func:`discovery.muicache_dirs`, which is
   often the only registry source that knows a standalone emulator exists.

The N64 row is the one that gets *easier*. RetroArch's mupen64plus-next packs
every storage type into one 296,960-byte ``.srm`` (see ``n64.py``); standalone
mupen64plus and Project64 write separate ``.eep`` / ``.sra`` / ``.fla`` files,
which is the shape Delta already stores. So a standalone N64 save is a copy with
the right extension chosen by size, and no conversion at all.

**Nothing here writes on the strength of a documented format.** Where a save
already exists, its bytes are measured before anything is overwritten -- see
:func:`check_shape`, which is what actually clears a write. Documentation says
Nestopia's ``.sav`` is raw on Windows and gzip on macOS; that is exactly the kind
of claim this module refuses to bet a save on.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import n64

#: Delta writes a bare dump of whichever storage the cartridge has, and the four
#: sizes are distinct, so the size names the file extension a standalone N64
#: emulator expects. ``n64.BY_SIZE`` already does the first half of this.
N64_EXTENSIONS: dict[str, str] = {
    "EEPROM": "eep",
    "SRAM": "sra",
    "FlashRAM": "fla",
}


class Where(Enum):
    """Where an emulator puts a save when its config does not say otherwise."""

    BESIDE_ROM = "beside the ROM"
    OWN_FOLDER = "a folder of its own"


@dataclass(frozen=True)
class SaveFile:
    """How one emulator stores one system's battery save.

    ``extension`` empty means the save's *size* names the extension, which is
    true only of N64 -- see :func:`extension_for`.

    ``blocked`` is the same gate as ``System.converted_cores`` in ``systems.py``,
    for the same reason and with the same bias: an emulator whose layout is
    known to differ from Delta's is refused outright rather than written to and
    hoped about. An emulator that is merely *unmeasured* is not blocked -- it is
    checked against its own existing save at write time instead.
    """

    extension: str
    blocked: str = ""


@dataclass(frozen=True)
class ConfigKey:
    """A setting in an emulator's own config file that names its save folder.

    Treated as a hint, never as the answer. Every one of these is read
    opportunistically: if the file is missing, or the key is absent, or it names
    a folder that does not exist, resolution moves on to the next source. That
    keeps a wrong guess about some emulator's config format from being able to
    send a save anywhere -- the worst case is that it contributes nothing.
    """

    #: Candidate paths, with ``{install}``, ``{appdata}`` and ``{localappdata}``
    #: substituted. Portable copies keep their config beside the executable, so
    #: that candidate comes first wherever both are possible.
    files: tuple[str, ...]
    key: str


@dataclass(frozen=True)
class Emulator:
    key: str
    name: str
    #: Filenames to look for in the registry and on disk. Matched whole, so an
    #: installer's own executable is not mistaken for the program.
    executables: tuple[str, ...]
    #: Substring of the uninstall entry's DisplayName, for the ones that have an
    #: installer. Empty for the zip-only ones, which is most of them.
    uninstall_match: str = ""
    #: System key -> how this emulator stores that system's save.
    saves: dict[str, SaveFile] = field(default_factory=dict)
    where: Where = Where.BESIDE_ROM
    #: Candidate save folders when ``where`` is OWN_FOLDER, same substitutions
    #: as ``ConfigKey.files``.
    save_dirs: tuple[str, ...] = ()
    config: ConfigKey | None = None
    #: How this emulator names a save file.
    #:
    #: ``"rom"`` -- after the ROM file, which is what most of these do and what
    #: RetroArch does, so the sanitised game name works.
    #:
    #: ``"mupen64plus"`` -- after the ROM's *contents*: the ``GoodName`` from
    #: its own ``mupen64plus.ini``, looked up by the ROM's MD5, plus the first
    #: eight hex digits of that MD5. Measured against a real install, because
    #: nothing short of running it would have said so -- and a save under any
    #: other name is one the emulator silently never opens.
    naming: str = "rom"
    note: str = ""

    def handles(self, system_key: str) -> bool:
        return system_key in self.saves

    def extension_for(self, system_key: str, save_size: int) -> str:
        """The extension this emulator wants, given Delta's save size.

        The size only matters for N64, where it names the storage type and
        therefore the file. Everything else ignores it.
        """
        layout = self.saves[system_key]
        if layout.extension:
            return layout.extension
        region = n64.BY_SIZE.get(save_size)
        if region is None:
            raise ValueError(
                f"{save_size} bytes is not one of the four N64 storage sizes, "
                "so there is no way to tell which file it belongs in"
            )
        return N64_EXTENSIONS[region.name]


#: Every standalone emulator this tool knows how to place a save for.
#:
#: The save shapes come from the same survey recorded in ``docs/research.md``.
#: What is written here is which file the emulator reads, not a promise about
#: its contents -- the contents are settled per-save by :func:`check_shape`.
EMULATORS: dict[str, Emulator] = {
    "mgba": Emulator(
        key="mgba",
        name="mGBA",
        executables=("mGBA.exe", "mgba-qt.exe"),
        uninstall_match="mGBA",
        # mGBA runs GBA and Game Boy / Color from the same binary and writes a
        # raw .sav for both, which is the same shape Delta stores.
        saves={"gba": SaveFile("sav"), "gbc": SaveFile("sav")},
        where=Where.BESIDE_ROM,
        config=ConfigKey(
            files=("{install}/config.ini", "{appdata}/mGBA/config.ini"),
            key="savegamePath",
        ),
        # Said out loud because it is the one thing a GBC player loses by
        # choosing mGBA over Gambatte, and it is invisible until the in-game
        # clock is wrong. Same gate as `System.clock_cores`.
        note=(
            "Game Boy Color clock files are not synced to mGBA: it stores the "
            "clock as a 48-byte struct where Gambatte stores eight bytes, and "
            "only Gambatte's layout has been checked against a real file."
        ),
    ),
    "vbam": Emulator(
        key="vbam",
        name="VisualBoyAdvance-M",
        executables=("visualboyadvance-m.exe",),
        uninstall_match="visualboyadvance",
        # The same emulator Delta runs for GBA, which is as close to a matching
        # pair as this table gets.
        saves={"gba": SaveFile("sav"), "gbc": SaveFile("sav")},
        where=Where.BESIDE_ROM,
        config=ConfigKey(
            files=(
                "{install}/vbam.ini",
                "{appdata}/visualboyadvance-m/vbam.ini",
            ),
            key="batteryDir",
        ),
    ),
    "snes9x": Emulator(
        key="snes9x",
        name="Snes9x",
        executables=("snes9x.exe", "snes9x-x64.exe"),
        uninstall_match="snes9x",
        # Delta runs snes9x too, and Delta's own save extension is already .srm.
        saves={"snes": SaveFile("srm")},
        where=Where.OWN_FOLDER,
        save_dirs=("{install}/Saves", "{install}/Sram"),
        note=(
            "Snes9x has kept its saves in more than one place across versions. "
            "An existing .srm found on disk is preferred over any of these."
        ),
    ),
    "nestopia": Emulator(
        key="nestopia",
        name="Nestopia UE",
        executables=("nestopia.exe",),
        uninstall_match="nestopia",
        # Blocked rather than merely unmeasured. The nesdev thread that reports
        # the compression also carries the correction that Windows writes raw --
        # so the likely outcome is that this is a plain copy, and "likely" is
        # not what a save is worth. One real file settles it and unblocks it.
        saves={
            "nes": SaveFile(
                "sav",
                blocked=(
                    "Nestopia compresses its save on some platforms and writes "
                    "it raw on others, and no real Windows file has been "
                    "measured here. Writing a raw save over a compressed one "
                    "would destroy it"
                ),
            )
        },
        where=Where.BESIDE_ROM,
    ),
    "melonds": Emulator(
        key="melonds",
        name="melonDS",
        executables=("melonDS.exe",),
        # The same emulator Delta runs for DS, and Delta's payload was measured
        # raw -- 524,288 bytes with no DeSmuME footer. See systems.py.
        saves={"ds": SaveFile("sav")},
        where=Where.BESIDE_ROM,
        config=ConfigKey(
            files=("{install}/melonDS.ini", "{localappdata}/melonDS/melonDS.ini"),
            key="SaveFilePath",
        ),
    ),
    "desmume": Emulator(
        key="desmume",
        name="DeSmuME",
        executables=("DeSmuME.exe",),
        uninstall_match="desmume",
        # The trap this project already walked into from the other direction.
        # Delta declares DeSmuME's .dsv extension while running melonDS, and its
        # payload is raw -- so it is *not* a .dsv, and handing DeSmuME one would
        # give it a save with no footer where it requires one.
        saves={
            "ds": SaveFile(
                "dsv",
                blocked=(
                    "DeSmuME's .dsv is raw save data plus a footer ending in "
                    "'|-DESMUME SAVE-|', and Delta's save is raw with no footer "
                    "despite sharing the extension. Copying one to the other "
                    "needs a conversion nobody has written or tested"
                ),
            )
        },
        where=Where.BESIDE_ROM,
    ),
    "mupen64plus": Emulator(
        key="mupen64plus",
        name="Mupen64Plus",
        executables=("mupen64plus-ui-console.exe", "mupen64plus-gui.exe"),
        # The prize. Delta stores one storage whole and so does this, so the
        # conversion RetroArch needs disappears: it is a copy, to a filename
        # the save's own size chooses.
        saves={"n64": SaveFile("")},
        where=Where.OWN_FOLDER,
        # The install-relative one is for a portable copy, which keeps its whole
        # data directory beside the executable rather than under %APPDATA%.
        save_dirs=("{appdata}/Mupen64Plus/save", "{install}/save"),
        naming="mupen64plus",
        config=ConfigKey(
            files=("{appdata}/Mupen64Plus/mupen64plus.cfg",),
            key="SaveSRAMPath",
        ),
    ),
    "project64": Emulator(
        key="project64",
        name="Project64",
        executables=("Project64.exe",),
        uninstall_match="project64",
        saves={
            "n64": SaveFile(
                "",
                blocked=(
                    "Project64 has shipped versions that store SRAM and "
                    "FlashRAM byte-swapped relative to mupen64plus, and no real "
                    "file has been measured here to say which this one does. "
                    "Use Mupen64Plus, where the bytes are known to match"
                ),
            )
        },
        where=Where.OWN_FOLDER,
        save_dirs=("{install}/Save", "{appdata}/Project64/Save"),
        config=ConfigKey(
            files=("{install}/Project64.cfg",),
            key="Save Directory",
        ),
    ),
    "sameboy": Emulator(
        key="sameboy",
        name="SameBoy",
        executables=("sameboy.exe", "SameBoy.exe"),
        saves={"gbc": SaveFile("sav")},
        where=Where.BESIDE_ROM,
        note=(
            "Game Boy Color clock files are not synced to SameBoy: its clock "
            "layout has not been checked against a real file."
        ),
    ),
    "bgb": Emulator(
        key="bgb",
        name="BGB",
        executables=("bgb.exe", "bgb64.exe"),
        saves={"gbc": SaveFile("sav")},
        where=Where.BESIDE_ROM,
        note=(
            "Game Boy Color clock files are not synced to BGB: its clock "
            "layout has not been checked against a real file."
        ),
    ),
}


def for_key(key: str) -> Emulator | None:
    return EMULATORS.get(key)


def handling(system_key: str) -> list[Emulator]:
    """Every emulator in the table that runs this system, blocked or not."""
    return [e for e in EMULATORS.values() if e.handles(system_key)]


def _substitutions(install: Path | None) -> dict[str, str]:
    return {
        "install": str(install) if install is not None else "",
        "appdata": os.environ.get("APPDATA", ""),
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
    }


def _expand(template: str, install: Path | None) -> Path | None:
    """Fill a templated path, or None when the variable it needs is unset.

    A missing ``%APPDATA%`` would otherwise produce a path rooted at the drive,
    which is both wrong and the kind of wrong that creates folders.
    """
    values = _substitutions(install)
    for name, value in values.items():
        marker = "{" + name + "}"
        if marker in template:
            if not value:
                return None
            template = template.replace(marker, value)
    return Path(template)


@dataclass
class Installed:
    """One standalone emulator found on this machine."""

    emulator: Emulator
    executable: Path

    @property
    def install_dir(self) -> Path:
        return self.executable.parent

    @property
    def name(self) -> str:
        return self.emulator.name


#: How far below a search root to look for an executable. Two levels finds
#: ``Emulators/nintendo/Mupen64Plus/mupen64plus-ui-console.exe`` from
#: ``Emulators``, which is the layout that prompted this. Deeper would start
#: walking ROM libraries, which can be tens of thousands of files on a network
#: drive, for no gain.
SEARCH_DEPTH = 3

#: A hard stop on how many directories one scan will visit, whatever the depth
#: allows. A search that is occasionally incomplete is fine -- ``path`` in
#: config.toml is the answer for an unusual layout -- but one that hangs the
#: launcher on somebody's NAS is not.
SEARCH_BUDGET = 4000


def scan_for_executables(
    roots: Sequence[Path], executables: Sequence[str], *, budget: int = SEARCH_BUDGET
) -> list[Path]:
    """Look under ``roots`` for any of ``executables``, breadth first.

    The registry cannot answer for most of these. They ship as a zip, so there
    is no installer to write an uninstall entry or an App Paths registration,
    and **MuiCache is written by the Windows shell rather than by execution** --
    so an emulator launched from a terminal, a script or another program leaves
    no trace there at all. Measured: a fresh Mupen64Plus, extracted and then run
    from the command line, was invisible to all three registry sources.

    So the last source is looking, and where to look is itself discovered
    rather than guessed -- the folder RetroArch was found in, because people
    keep their emulators together. See :func:`search_roots`.
    """
    wanted = {name.lower() for name in executables}
    found: list[Path] = []
    seen: set[Path] = set()
    queue: list[tuple[Path, int]] = []

    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            queue.append((resolved, 0))

    visited = 0
    while queue and visited < budget:
        directory, depth = queue.pop(0)
        visited += 1
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower() in wanted:
                    path = Path(entry.path)
                    if path not in found:
                        found.append(path)
                elif entry.is_dir() and depth < SEARCH_DEPTH:
                    child = Path(entry.path)
                    if child not in seen:
                        seen.add(child)
                        queue.append((child, depth + 1))
            except OSError:
                continue
    return found


def search_roots(extra_dirs: Sequence[Path] = ()) -> list[Path]:
    """Folders worth scanning, most likely first.

    The first is the folder RetroArch lives in. That is not a guess: RetroArch's
    own location is discovered from the registry or from its config, and an
    emulator collection is a folder of folders -- on this machine RetroArch sits
    at ``Emulators/RetroArch`` and Mupen64Plus at ``Emulators/nintendo/
    Mupen64Plus``, so the one finds the other.
    """
    from . import discovery

    roots: list[Path] = [Path(d) for d in extra_dirs]

    config = discovery.find_retroarch_config()
    if config.path is not None:
        roots.append(config.path.parent.parent)

    for directory in discovery.install_dirs(("retroarch.exe",), "retroarch"):
        roots.append(directory.parent)

    unique: list[Path] = []
    for root in roots:
        # Never a drive root. Taking the parent of an install folder is how
        # "the folder emulators are kept in" is found, and it gives ``C:\`` for
        # anything installed at the top of a drive -- which this machine has,
        # as a stale uninstall entry naming a long-deleted C:\RetroArch-Win64.
        # Measured: that one entry turned a 136-directory scan taking no
        # measurable time into 29,649 directories and thirteen seconds, on a
        # path the launcher runs when its window opens.
        if root.parent == root:
            continue
        if root not in unique:
            unique.append(root)
    return unique


def find_executable(emulator: Emulator, extra_dirs: tuple[Path, ...] = ()) -> Path | None:
    """Locate one emulator's executable, or None.

    ``extra_dirs`` is for folders the user has named -- a config override, or the
    folder a sibling emulator was found in, since people keep them together. The
    registry sources come first because they are what the machine itself says.
    """
    from . import discovery

    directories = list(discovery.install_dirs(
        emulator.executables, emulator.uninstall_match
    ))
    directories.extend(extra_dirs)

    for directory in directories:
        for executable in emulator.executables:
            candidate = directory / executable
            if candidate.is_file():
                return candidate

    # Nothing in the registry and nowhere the user named. Look where emulators
    # are kept -- which for most of these is the only source that ever answers,
    # because a zip install writes no registry entry and running one from
    # anywhere but Explorer writes no MuiCache entry either.
    scanned = scan_for_executables(search_roots(), emulator.executables)
    return scanned[0] if scanned else None


def find_installed(extra_dirs: tuple[Path, ...] = ()) -> list[Installed]:
    """Every emulator from the table that is actually on this machine.

    One scan for all of them rather than one per emulator: the search roots are
    the same, and walking a collection folder ten times over would turn a cheap
    lookup into a visible pause in the launcher.
    """
    found: list[Installed] = []
    pending: list[Emulator] = []

    for emulator in EMULATORS.values():
        executable = _from_registry_or_named(emulator, extra_dirs)
        if executable is not None:
            found.append(Installed(emulator, executable))
        else:
            pending.append(emulator)

    if pending:
        wanted = [name for emulator in pending for name in emulator.executables]
        by_name: dict[str, Path] = {}
        for path in scan_for_executables(search_roots(extra_dirs), wanted):
            by_name.setdefault(path.name.lower(), path)
        for emulator in pending:
            for name in emulator.executables:
                path = by_name.get(name.lower())
                if path is not None:
                    found.append(Installed(emulator, path))
                    break

    found.sort(key=lambda item: item.emulator.name.lower())
    return found


def _from_registry_or_named(
    emulator: Emulator, extra_dirs: Sequence[Path]
) -> Path | None:
    """The cheap half of :func:`find_executable`, with no directory walk."""
    from . import discovery

    directories = list(
        discovery.install_dirs(emulator.executables, emulator.uninstall_match)
    )
    directories.extend(extra_dirs)
    for directory in directories:
        for executable in emulator.executables:
            candidate = Path(directory) / executable
            if candidate.is_file():
                return candidate
    return None


#: ``mupen64plus.ini`` is ~430 KB and keyed by ROM MD5. Parsed once per install
#: and kept, because a sync asks it once per N64 game.
_GOODNAMES: dict[Path, dict[str, str]] = {}


def goodnames(ini_path: Path) -> dict[str, str]:
    """Map ROM MD5 (upper hex) to GoodName, from ``mupen64plus.ini``.

    The file's section headers *are* the MD5s::

        [20B854B239203BAF6C961B850A4A51A2]
        GoodName=Super Mario 64 (U) [!]
        CRC=635A2BFF 8B022326
        SaveType=Eeprom 4KB
    """
    cached = _GOODNAMES.get(ini_path)
    if cached is not None:
        return cached

    found: dict[str, str] = {}
    try:
        text = ini_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _GOODNAMES[ini_path] = found
        return found

    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().upper()
        elif section and stripped.lower().startswith("goodname="):
            found[section] = stripped.partition("=")[2].strip()
    _GOODNAMES[ini_path] = found
    return found


#: Mupen64Plus cuts the GoodName at 32 characters before using it as a
#: filename. Measured: a GoodName of 37 characters produced a save whose name
#: held the first 32 and nothing more.
MUPEN64PLUS_NAME_LIMIT = 32

#: And replaces each of these with an underscore. Measured by putting all nine
#: into a GoodName and reading back what it wrote. It is the set Windows
#: forbids, which is why eight real entries in its own database -- "Doom 64:
#: Complete Edition" among them -- would otherwise produce a filename Windows
#: cannot even represent.
MUPEN64PLUS_ILLEGAL = '<>:"/\\|?*'


def mupen64plus_filename(goodname: str, digest: str) -> str:
    """The stem Mupen64Plus builds from a GoodName and a ROM MD5.

    Three steps, all measured against a real 2.6.0 install rather than read out
    of its source: cut to 32 characters, replace the characters Windows forbids
    with underscores, then append the MD5's first eight hex digits.

    Getting any of the three wrong produces a real file under a name the
    emulator never looks for -- which is indistinguishable, from the outside,
    from the sync having quietly done nothing.
    """
    trimmed = goodname[:MUPEN64PLUS_NAME_LIMIT]
    safe = "".join("_" if c in MUPEN64PLUS_ILLEGAL else c for c in trimmed)
    return f"{safe}-{digest[:8].upper()}"


#: The first four bytes of an N64 ROM, which say how the rest is arranged.
#: Every dump is the same data; only the byte order differs.
Z64_MAGIC = b"\x80\x37\x12\x40"   # big-endian, the cartridge's own order
V64_MAGIC = b"\x37\x80\x40\x12"   # 16-bit byte-swapped
N64_MAGIC = b"\x40\x12\x37\x80"   # 32-bit little-endian


#: The smallest file Mupen64Plus will open as a ROM. Measured by bisection
#: against a real 2.6.0: 0x800 gives "core failed to open ROM image file",
#: 0x1000 loads. Anything below this is not a ROM as far as it is concerned, so
#: deriving a save name from one would name a save for a game it cannot run.
MINIMUM_ROM_SIZE = 0x1000


def to_z64(data: bytes) -> bytes | None:
    """A ROM in native big-endian order, whatever order it arrived in.

    Mupen64Plus converts a ROM to this order *before* computing the MD5 it looks
    its database up by, so hashing the file as it sits on disk finds nothing for
    a byte-swapped dump -- and the refusal would blame its database for not
    knowing a game it knows perfectly well.

    None rather than a guess for anything that is not a ROM this emulator would
    load. Two ways that happens, and the second is the one that bites:

    - **Too small.** See :data:`MINIMUM_ROM_SIZE`.
    - **A swapped dump whose length does not divide by the swap width.** The
      conversion has no defined answer for the leftover bytes, and an earlier
      version quietly dropped them -- which changes the bytes being hashed, and
      so the MD5, and so the filename. Mupen64Plus does load such a file, so
      this is deliberately stricter than it is: refusing names no save, while
      guessing writes one under a name it will never open.
    """
    if len(data) < MINIMUM_ROM_SIZE:
        return None

    head = data[:4]
    if head == Z64_MAGIC:
        return data
    if head == V64_MAGIC:
        if len(data) % 2:
            return None
        return bytes(data[i ^ 1] for i in range(len(data)))
    if head == N64_MAGIC:
        if len(data) % 4:
            return None
        return b"".join(data[i : i + 4][::-1] for i in range(0, len(data), 4))
    return None


#: Where a ROM carries its own name, 20 bytes at offset 0x20 of the header.
#: Mupen64Plus falls back to this when its database does not know the ROM.
_HEADER_NAME = slice(0x20, 0x34)


#: What Mupen64Plus calls a ROM whose header carries no name at all.
UNNAMED_ROM = "unknown"


def internal_name(native: bytes) -> str:
    """The name a ROM carries in its own header, as Mupen64Plus reads it.

    Two steps, in this order, and the order is the whole of it. Measured across
    five headers against a real 2.6.0:

    ===========================  ===========  ===============================
    header bytes                 becomes      why
    ===========================  ===========  ===============================
    ``b"\\x00AB..."``             ``unknown``  read as a **C string**, so it
                                              stops at the first NUL and what
                                              is left is empty
    twenty NULs                  ``unknown``  same
    twenty spaces                ``""``       not empty, so no substitution --
                                              it simply strips to nothing, and
                                              the save is named ``-<md5>``
    ``b"AB" + spaces``           ``AB``       stripped
    ``b"  AB" + spaces``         ``AB``       stripped at both ends
    ===========================  ===========  ===============================

    So "empty" is decided *before* stripping, not after: twenty NULs and twenty
    spaces are both nameless to a reader, and Mupen64Plus gives them different
    filenames. Reading the field as text and stripping it -- which is the
    obvious implementation, and was this one -- gets all three odd rows wrong.
    """
    raw = native[_HEADER_NAME]
    text = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    if not text:
        return UNNAMED_ROM
    return text.strip()


def mupen64plus_stem(install_dir: Path, rom: Path) -> str | None:
    """What Mupen64Plus will call this ROM's save, without the extension.

    Both halves come from the same MD5, so a ROM the database does not know
    yields None rather than a guessed name -- and a save written under a guessed
    name is one the emulator never opens, which looks exactly like the sync
    having done nothing.
    """
    try:
        native = to_z64(rom.read_bytes())
    except OSError:
        return None
    if native is None:
        return None

    digest = hashlib.md5(native).hexdigest().upper()
    good = goodnames(install_dir / "mupen64plus.ini").get(digest)
    if not good:
        # Not in the database is not a dead end. Mupen64Plus falls back to the
        # name the ROM carries in its own header -- measured by hiding a known
        # ROM's entry and running it: it reported the GoodName as "SUPER MARIO
        # 64 (unknown rom)" and wrote "SUPER MARIO 64-20B854B2.eep", so the
        # suffix is for display and the filename uses the bare header name.
        #
        # Worth following rather than refusing: hacks, translations and
        # homebrew are ordinary things to have in Delta, and every one of them
        # is an "unknown rom" here.
        # Deliberately not refused when this comes back empty. A header of
        # twenty spaces really does give Mupen64Plus a save called
        # "-<md5>.eep", and writing anything else there -- including nothing --
        # is the same failure as every other name this rule has got wrong.
        good = internal_name(native)
    return mupen64plus_filename(good, digest)


def save_stem(installed: "Installed", game_name: str, rom: Path | None) -> str | None:
    """The filename stem this emulator expects for one game, or None.

    None means "this emulator's own name for the save cannot be worked out",
    which is a refusal rather than a fallback: writing ``Super Mario 64.eep``
    where Mupen64Plus looks for ``Super Mario 64 (U) [!]-20B854B2.eep`` puts a
    real save on disk that nothing will ever read.
    """
    from . import naming

    if installed.emulator.naming == "mupen64plus":
        if rom is None or not rom.is_file():
            return None
        return mupen64plus_stem(installed.install_dir, rom)
    return naming.safe_filename(game_name)


_SETTING = re.compile(r'^\s*(?P<key>[^=;#\[\]]+?)\s*=\s*"?(?P<value>.*?)"?\s*$')


def read_setting(path: Path, key: str) -> str:
    """Read one ``key = value`` setting out of an emulator's config.

    Section-blind on purpose. These files are INI-shaped but not consistently
    so, the keys wanted here are distinctive enough not to collide, and being
    wrong about a section name would silently return nothing -- which is the
    same as the file being missing, and is handled the same way.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    wanted = key.strip().lower()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;[":
            continue
        match = _SETTING.match(line)
        if match and match["key"].strip().lower() == wanted:
            return match["value"].strip()
    return ""


@dataclass
class SaveLocation:
    """Where this emulator's saves go, and how that was decided.

    ``source`` is carried so the launcher can print it. "Beside the ROM" and
    "its config says" are very different confidence levels, and someone whose
    save went somewhere unexpected needs to know which one they got.
    """

    directory: Path | None
    source: str

    @property
    def found(self) -> bool:
        return self.directory is not None


def resolve_save_dir(
    installed: Installed,
    rom_dir: Path | None,
    *,
    override: Path | None = None,
    observed: Path | None = None,
) -> SaveLocation:
    """Decide where to put a save for this emulator, best evidence first.

    The order is the point. Evidence beats construction:

    1. What the user set, which ends the argument.
    2. **An existing save found on disk**, which is the machine telling us where
       this emulator actually writes rather than us telling it. Passed in by the
       caller, because finding it needs the filename we are about to use.
    3. The emulator's own config, when it has the key and the folder exists.
    4. A candidate folder that exists.
    5. Beside the ROM, which is the documented default for most of these.
    """
    if override is not None:
        return SaveLocation(override, "set in config.toml")

    if observed is not None:
        return SaveLocation(observed.parent, f"where {observed.name} already is")

    emulator = installed.emulator
    if emulator.config is not None:
        for template in emulator.config.files:
            config_path = _expand(template, installed.install_dir)
            if config_path is None or not config_path.is_file():
                continue
            raw = read_setting(config_path, emulator.config.key)
            if not raw:
                continue
            # Relative paths in these files are relative to the install.
            named = Path(os.path.expandvars(raw)).expanduser()
            if not named.is_absolute():
                named = installed.install_dir / named
            if named.is_dir():
                return SaveLocation(named, f"{config_path.name} says so")

    for template in emulator.save_dirs:
        candidate = _expand(template, installed.install_dir)
        if candidate is not None and candidate.is_dir():
            return SaveLocation(candidate, "its usual folder")

    if emulator.where is Where.BESIDE_ROM and rom_dir is not None:
        return SaveLocation(rom_dir, "beside the ROM, which is its default")

    return SaveLocation(
        None,
        "could not be determined -- set it in config.toml under "
        f"[emulators.{emulator.key}]",
    )


#: gzip, which is the shape Nestopia is reported to write on some platforms.
GZIP_MAGIC = b"\x1f\x8b"

#: DeSmuME stamps this at the end of a .dsv. Delta's DS save carries the same
#: extension and none of this, which is exactly why it is checked for.
DESMUME_MARKER = b"|-DESMUME SAVE-|"


def check_shape(existing: bytes, incoming: bytes) -> str | None:
    """Why the save already on disk must not be overwritten, or None.

    This is what actually clears a write, and it is deliberately not a table
    lookup: it measures the user's own file at the moment it matters, which is
    the only evidence available about an emulator nobody here has run.

    The three findings are all the same finding -- the file on disk is not the
    shape Delta's save is -- and each one means a plain copy would replace a
    working save with something the emulator cannot read.
    """
    if existing.startswith(GZIP_MAGIC) and not incoming.startswith(GZIP_MAGIC):
        return (
            "the save already there is gzip-compressed and Delta's is not, so "
            "this emulator does not store saves the way Delta does"
        )
    if existing.endswith(DESMUME_MARKER) and not incoming.endswith(DESMUME_MARKER):
        return (
            "the save already there ends in DeSmuME's footer and Delta's does "
            "not, so this emulator wants a wrapped save rather than a raw one"
        )
    if len(existing) != len(incoming):
        return (
            f"the save already there is {len(existing):,} bytes and Delta's is "
            f"{len(incoming):,}, so the two are not the same kind of file"
        )
    return None


def blocked_reason(emulator: Emulator, system_key: str) -> str | None:
    """Why this emulator will not be written to for this system, or None."""
    layout = emulator.saves.get(system_key)
    if layout is None:
        return f"{emulator.name} does not run this system"
    return layout.blocked or None
