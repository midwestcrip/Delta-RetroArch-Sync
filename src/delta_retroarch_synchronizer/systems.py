"""Per-system facts about how Delta and RetroArch store battery saves.

Every value here was read out of Delta's own source rather than inferred:

- ``delta_type`` is the ``GameType`` raw value stored in Harmony's record JSON
  (e.g. ``GBADeltaCore/Types/GBATypes.m``).
- ``delta_save_ext`` is the core's ``gameSaveFileExtension`` property
  (e.g. ``GBADeltaCore/GBA.swift``).

The RetroArch side is the frontend's convention, not the core's: RetroArch
names battery saves ``<content name>.srm`` regardless of core, because the
frontend owns writing SRAM to disk. Cores cannot override it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class System:
    key: str
    name: str
    delta_type: str
    delta_core: str
    delta_save_ext: str
    retroarch_save_ext: str
    #: Extra Delta file identifiers synced alongside the main save.
    extra_files: tuple[str, ...] = ()
    #: Delta's file identifier for the real-time-clock companion file, and the
    #: extension RetroArch's core gives the same thing. Empty for systems with
    #: no clock, which is most of them.
    delta_clock_id: str = ""
    retroarch_clock_ext: str = ""
    #: Cores whose clock file format has been checked against a real file.
    #: Deliberately separate from ``retroarch_cores``: a core can be perfectly
    #: fine to play with while storing its clock in a format we have not
    #: verified, and writing the wrong shape over it would corrupt it. A core
    #: absent from here plays normally and simply gets no clock sync.
    clock_cores: tuple[str, ...] = ()
    #: True when a byte-for-byte copy (with a new extension) is sufficient.
    raw_compatible: bool = True
    #: True when this system is *not* a plain copy but its conversion is built
    #: and tested. Separate from ``raw_compatible`` on purpose: the gate that
    #: guards ``ENABLED_SYSTEMS`` should keep saying "this needs conversion",
    #: and gain a second way to be satisfied rather than quietly losing the
    #: first. N64 is the only system in this state.
    converted: bool = False
    #: Set for systems we knowingly do not convert yet. Long on purpose -- the
    #: inspector prints it, and someone asking "why is my N64 game skipped?"
    #: deserves the real answer.
    conversion_note: str = ""
    #: The same fact in one line, for the launcher's log.
    #:
    #: The long note was being reprinted on every sync, four wrapped lines of it,
    #: for a system that is permanently blocked and never changes. On 2026-09-07
    #: that buried a cheat push confirmation off the top of a fourteen-line log
    #: within two games, and the user reasonably concluded nothing had happened.
    #: A recurring message has to be short; the detail belongs where it is asked
    #: for, not repeated at someone who has read it.
    conversion_summary: str = ""
    #: Common ROM extensions, used to name the file we hand to RetroArch.
    rom_exts: tuple[str, ...] = field(default_factory=tuple)
    #: libretro-database's folder name for this system, which is how RetroArch
    #: organises cheat files.
    retroarch_db_name: str = ""
    #: RetroArch core display names ("corename") that can run this system, in
    #: preference order. The name matters beyond selection: RetroArch sorts
    #: saves into a folder named after it.
    retroarch_cores: tuple[str, ...] = field(default_factory=tuple)


SYSTEMS: dict[str, System] = {
    "gba": System(
        key="gba",
        name="Game Boy Advance",
        delta_type="com.rileytestut.delta.game.gba",
        delta_core="visualboyadvance-m",
        delta_save_ext="sav",
        retroarch_save_ext="srm",
        raw_compatible=True,
        rom_exts=("gba",),
        retroarch_db_name="Nintendo - Game Boy Advance",
        retroarch_cores=("mGBA", "VBA-M", "VBA Next", "gpSP", "Beetle GBA"),
    ),
    "gbc": System(
        key="gbc",
        name="Game Boy / Game Boy Color",
        delta_type="com.rileytestut.delta.game.gbc",
        delta_core="gambatte",
        delta_save_ext="sav",
        retroarch_save_ext="srm",
        # Delta syncs the RTC clock file as a second file on the same record.
        extra_files=("gameTimeSave",),
        delta_clock_id="gameTimeSave",
        retroarch_clock_ext="rtc",
        # Gambatte only. Its .rtc is eight little-endian bytes, verified against
        # a real file. mGBA's core writes a .rtc of the same name that is a
        # 48-byte struct, and SameBoy and TGB Dual are unchecked -- so those
        # play fine and get no clock sync rather than a corrupted one.
        clock_cores=("Gambatte",),
        raw_compatible=True,
        rom_exts=("gbc", "gb"),
        retroarch_db_name="Nintendo - Game Boy Color",
        retroarch_cores=("Gambatte", "SameBoy", "mGBA", "TGB Dual"),
    ),
    "nes": System(
        key="nes",
        name="Nintendo Entertainment System",
        delta_type="com.rileytestut.delta.game.nes",
        delta_core="nestopia",
        delta_save_ext="sav",
        retroarch_save_ext="srm",
        raw_compatible=True,
        rom_exts=("nes",),
        retroarch_db_name="Nintendo - Nintendo Entertainment System",
        retroarch_cores=("Nestopia", "Mesen", "FCEUmm", "QuickNES"),
    ),
    "snes": System(
        key="snes",
        name="Super Nintendo",
        delta_type="com.rileytestut.delta.game.snes",
        delta_core="snes9x",
        delta_save_ext="srm",
        retroarch_save_ext="srm",
        raw_compatible=True,
        rom_exts=("sfc", "smc"),
        retroarch_db_name="Nintendo - Super Nintendo Entertainment System",
        retroarch_cores=("Snes9x", "Snes9x - Current", "bsnes", "Beetle Supafaust"),
    ),
    "n64": System(
        key="n64",
        name="Nintendo 64",
        delta_type="com.rileytestut.delta.game.n64",
        delta_core="mupen64plus",
        delta_save_ext="sav",
        retroarch_save_ext="srm",
        # Not a copy: RetroArch's mupen64plus-next packs EEPROM, SRAM, FlashRAM
        # and four Controller Paks into one 296,960-byte .srm at fixed offsets,
        # while Delta writes a single bare dump of whichever storage the
        # cartridge has. The mapping lives in `n64.py`.
        raw_compatible=False,
        converted=True,
        rom_exts=("n64", "z64", "v64"),
        retroarch_db_name="Nintendo - Nintendo 64",
        retroarch_cores=("Mupen64Plus-Next", "ParaLLEl N64"),
    ),
    "ds": System(
        key="ds",
        name="Nintendo DS",
        delta_type="com.rileytestut.delta.game.ds",
        delta_core="melonDS",
        # Delta declares DeSmuME's extension while running melonDS, which looked
        # for a long time like it might mean a footered .dsv payload. It does
        # not -- the extension is a migration leftover and the bytes are raw.
        # See the note above ENABLED_SYSTEMS.
        delta_save_ext="dsv",
        retroarch_save_ext="srm",
        raw_compatible=True,
        rom_exts=("nds",),
        retroarch_db_name="Nintendo - Nintendo DS",
        # melonDS DS first: it is the same emulator Delta runs. DeSmuME is kept
        # as a last resort but unverified here -- RetroArch's frontend owns
        # writing the .srm so it should be raw whatever the core, and "should"
        # is not the standard this list is held to.
        retroarch_cores=("melonDS DS", "melonDS", "DeSmuME"),
    ),
}

#: Systems the sync pass is currently cleared to write. Widen this only once the
#: corresponding conversion has been implemented and tested against a real save
#: file -- silently syncing an unconverted N64 or DS save would corrupt it.
#:
#: snes added 2026-09-06 against a real Super Mario World save from Delta.
#: There is no conversion to implement for it: Delta writes .srm and RetroArch
#: reads .srm, so the "conversion" is a copy. What was actually checked is that
#: the payload is raw SRAM -- 2048 bytes exactly, which is the cartridge's
#: 16 Kbit SRAM, with no header, footer or wrapper of any kind. That last point
#: is the one that matters, because it is precisely how DS differs: Delta
#: declares .dsv there and the payload may carry a trailing DeSmuME marker.
#:
#: gbc added 2026-09-06 against a real Pokemon Crystal save: 32768 bytes exactly,
#: the cartridge's 32KB SRAM, raw with no header or footer.
#:
#: Its clock syncs too, as of 2026-09-07, but only on Gambatte. Delta carries a
#: second file on the same record, `gameTimeSave` -- four bytes, a big-endian
#: Unix timestamp of when the game was last played, used to advance Crystal's
#: real-time clock. Gambatte-libretro stores the same quantity as eight
#: little-endian bytes in a `.rtc` beside the save, so the two are a lossless
#: width-and-byte-order swap. Both were confirmed against real files rather than
#: read out of source, which is why Gambatte is the only entry in `clock_cores`.
#:
#: `clock_cores` is deliberately narrower than `retroarch_cores`. mGBA-libretro
#: writes a 48-byte struct under the same filename and SameBoy and TGB Dual are
#: unchecked, so those play normally and get no clock sync -- writing the wrong
#: shape over one would be the exact mistake this gate exists to prevent. The
#: cost of skipping it is bounded and is not corruption: the save carries across
#: intact and the in-game clock may be out by however long the two sides were
#: apart, which for Crystal affects day/night and daily events.
#:
#: nes added 2026-09-06 against a real Kirby's Adventure save: 8192 bytes
#: exactly, which is the MMC3 mapper's battery-backed 8KB PRG-RAM, raw with no
#: header or footer. Delta runs nestopia and RetroArch's Nestopia core writes
#: the same 8KB region, so this is a copy like SNES was.
#:
#: The game matters here. Super Mario Bros. could never have verified NES --
#: that cartridge has no SRAM at all, which is why Delta stores no save file for
#: it and why it sat in this folder for a day proving nothing. A battery-backed
#: game was required: Zelda, Metroid, Kirby's Adventure, Final Fantasy.
#:
#: ds added 2026-09-07 against a real Pokemon Platinum save, and this one was an
#: open question from the start rather than a formality. Delta declares DeSmuME's
#: `.dsv` extension while running melonDS, and `.dsv` is raw save data followed
#: by a footer ending in the marker `|-DESMUME SAVE-|`. If Delta wrote a real
#: footered .dsv, copying it to RetroArch would hand the core a save with 122
#: bytes of trailing metadata where it expects none.
#:
#: It does not. Measured on the real file:
#:
#:   size            524288 bytes exactly -- 512 KB, the bare chip size, and a
#:                   power of two, which a footered file cannot be
#:   DESMUME marker  absent from the file entirely
#:   tail            0xFF padding, i.e. unwritten flash
#:
#: So the extension is a migration leftover and the payload is melonDS's raw
#: format. DS is a rename, exactly like SNES. The marker was searched for
#: directly rather than inferred from the size, because a footer on a save that
#: happened to be short would have left the size looking right.
#:
#: n64 added 2026-09-07, and it is the only entry here that is not a copy. Its
#: conversion is `n64.py`, written against the offsets in ra_mp64_srm_convert
#: and against six real Delta saves covering all four cartridge storage types:
#: 512 B and 2,048 B EEPROM, 32,768 B SRAM, 131,072 B FlashRAM. Delta copies one
#: storage whole, so the file size names the type -- that is what makes the
#: mapping exact rather than inferred.
#:
#: What is deliberately *not* converted is Controller Pak data. Delta never
#: syncs it (`GameSave.syncableFiles` has no entry), so those four 32 KB regions
#: belong entirely to the desktop and are carried through untouched. A game that
#: keeps progress there -- Mario Kart 64 ghosts, say -- has that progress stay
#: on whichever machine made it, and the sync says so once per game rather than
#: leaving it to be discovered.
ENABLED_SYSTEMS: frozenset[str] = frozenset(
    {"gba", "snes", "gbc", "nes", "ds", "n64"}
)

BY_DELTA_TYPE: dict[str, System] = {s.delta_type: s for s in SYSTEMS.values()}


def for_delta_type(delta_type: str) -> System | None:
    """Look up a system by the ``GameType`` string found in a Harmony record."""
    return BY_DELTA_TYPE.get(delta_type)


def missing_core_advice(system: System) -> str:
    """What to actually do about a system with no core installed.

    Deliberately a set of directions rather than an offer to fetch the core.
    Downloading a .dll and placing it where RetroArch will execute it would make
    this program something that installs executable code from the internet --
    which breaks the claim that it talks to nothing but Dropbox, materially
    worsens the antivirus problem it already has, and reimplements a downloader
    RetroArch ships and keeps matched to its own build.

    So the tool says exactly which core and exactly where the button is, which
    is what turns a dead end into a task. Both naive-user tests found the old
    message -- "no core installed for Super Nintendo" -- unusable by someone who
    does not already know what a core is.
    """
    preferred = system.retroarch_cores[0] if system.retroarch_cores else ""
    if not preferred:
        return f"no core installed for {system.name}."

    others = ", ".join(system.retroarch_cores[1:])
    lines = [
        f"no core installed for {system.name}.",
        f"    In RetroArch: Load Core -> Download a Core -> {preferred}.",
        "    Take the plainly named one; suffixed variants are different cores "
        "and will not be matched.",
    ]
    if others:
        lines.append(f"    These also work: {others}.")
    return "\n".join(lines)
