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
    #: True when a byte-for-byte copy (with a new extension) is sufficient.
    raw_compatible: bool = True
    #: Set for systems we knowingly do not convert yet.
    conversion_note: str = ""
    #: Common ROM extensions, used to name the file we hand to RetroArch.
    rom_exts: tuple[str, ...] = field(default_factory=tuple)


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
        raw_compatible=True,
        rom_exts=("gbc", "gb"),
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
    ),
}

#: Systems the sync pass is currently cleared to write. Widen this only once
#: the corresponding conversion has been implemented and tested against a real
#: save file -- silently syncing an unconverted N64 or DS save would corrupt it.
ENABLED_SYSTEMS: frozenset[str] = frozenset({"gba"})

BY_DELTA_TYPE: dict[str, System] = {s.delta_type: s for s in SYSTEMS.values()}


def for_delta_type(delta_type: str) -> System | None:
    """Look up a system by the ``GameType`` string found in a Harmony record."""
    return BY_DELTA_TYPE.get(delta_type)
