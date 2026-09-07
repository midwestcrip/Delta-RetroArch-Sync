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
    #: Set for systems we knowingly do not convert yet.
    conversion_note: str = ""
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
        raw_compatible=False,
        conversion_note=(
            "RetroArch's mupen64plus-next packs EEPROM/SRAM/FlashRAM/mempaks "
            "into one 290KB .srm at fixed offsets; Delta writes a single "
            "bare save of whichever type the cart uses. Needs an offset-mapping "
            "conversion step (see ra_mp64_srm_convert) before it is safe to sync."
        ),
        rom_exts=("n64", "z64", "v64"),
        retroarch_db_name="Nintendo - Nintendo 64",
        retroarch_cores=("Mupen64Plus-Next", "ParaLLEl N64"),
    ),
    "ds": System(
        key="ds",
        name="Nintendo DS",
        delta_type="com.rileytestut.delta.game.ds",
        delta_core="melonDS",
        delta_save_ext="dsv",
        retroarch_save_ext="srm",
        raw_compatible=False,
        conversion_note=(
            "Delta declares the DeSmuME '.dsv' extension while running melonDS. "
            "Unverified whether the payload carries the DeSmuME footer "
            "(trailing '|-DESMUME SAVE-|' marker) or is already raw. Inspect a "
            "real save before converting; strip or append the footer accordingly."
        ),
        rom_exts=("nds",),
        retroarch_db_name="Nintendo - Nintendo DS",
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
#: Its clock is deliberately not synced. Delta carries a second file on the same
#: record, `gameTimeSave` -- four bytes, a big-endian Unix timestamp of when the
#: game was last played, used to advance Crystal's real-time clock. Nothing in
#: this tool writes it: `extra_files` is read by the inspector for reporting and
#: by nothing else, so only the battery save moves. What RetroArch's Gambatte and
#: mGBA cores expect for RTC state has not been checked against a real file, and
#: guessing would be the exact mistake this gate exists to prevent.
#:
#: The consequence is bounded and not corruption: a save carries across intact,
#: and the in-game clock may be out by however long the two sides were apart,
#: which for Crystal affects day/night and daily events.
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
ENABLED_SYSTEMS: frozenset[str] = frozenset({"gba", "snes", "gbc", "nes"})

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
