"""Converting between Delta's bare N64 save and RetroArch's combined ``.srm``.

Every other system this tool syncs is a copy with a different extension. N64 is
the one that genuinely needs conversion, because the two sides disagree about
what a save file *is*:

- **Delta** writes one storage, whole. ``N64EmulatorBridge.saveGameSaveToURL:``
  branches on ``g_dev.cart.use_flashram`` -- ``-1`` SRAM, ``0`` EEPROM, ``1``
  FlashRAM -- and copies ``storage->size`` bytes with no header, footer or
  padding. So a Delta N64 save is a bare dump of whichever one the cartridge has.
- **RetroArch's mupen64plus-next** writes a single 296,960-byte ``.srm``
  containing every storage type at fixed offsets, whether the cartridge has them
  or not, plus four Controller Paks.

The layout below is read from
`ra_mp64_srm_convert <https://github.com/drehren/ra_mp64_srm_convert>`_, which is
the working reference the libretro core is matched against:

===================  ==================  ==========
Region               Offset              Size
===================  ==================  ==========
EEPROM               ``0x00000``         ``0x00800``
Controller Pak 1-4   ``0x00800``         ``0x08000`` each
SRAM                 ``0x20800``         ``0x08000``
FlashRAM             ``0x28800``         ``0x20000``
===================  ==================  ==========

**The size of Delta's file names the type**, because Delta copies one storage
whole and the four sizes are distinct. That is what makes this conversion exact
rather than a guess, and it is why extraction never has to infer anything from
the ``.srm`` itself: Delta's record already says how many bytes belong to it.

Two rules this module exists to enforce:

1. **Never touch a region we have nothing for.** Of the four regions, only the
   cartridge save is ever ours. Delta does not sync Controller Pak data at all
   (``GameSave.syncableFiles`` has no entry for it), so those 128 KB belong to
   whatever the player did on the desktop. Overwriting them would silently
   destroy Mario Kart 64 ghosts and anything else living there.
2. **Never widen a save on the way home.** Extraction takes exactly the byte
   count Delta's record states, so what goes back is the same shape that came
   out. This is also why the push size guard needs no exemption for N64 -- an
   earlier plan assumed it would.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Total size of mupen64plus-next's combined save. Anything else is not one.
SRM_SIZE = 0x48800

#: One Controller Pak, and where the four of them live.
PAK_SIZE = 0x8000
PAKS_START = 0x800
PAKS_END = 0x20800
PAK_COUNT = 4

#: Unwritten storage. Delta's own loader memsets to this when no save exists,
#: so it is the correct "nothing here" fill on both sides.
EMPTY = 0xFF


@dataclass(frozen=True)
class Region:
    """One cartridge storage type inside the combined save."""

    name: str
    start: int
    #: How much of the ``.srm`` this storage occupies.
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


EEPROM = Region("EEPROM", 0x00000, 0x0800)
SRAM = Region("SRAM", 0x20800, 0x8000)
FLASHRAM = Region("FlashRAM", 0x28800, 0x20000)

#: Delta save size -> where it belongs. EEPROM comes in two capacities and both
#: sit at the same offset; a 4 Kbit save occupies the first 512 bytes and the
#: rest of the region stays empty, which is exactly how the reference tells the
#: two apart (`self.0[0x200..].all(0xff)`).
BY_SIZE: dict[int, Region] = {
    0x200: EEPROM,    # 4 Kbit EEPROM -- Super Mario 64
    0x800: EEPROM,    # 16 Kbit EEPROM -- Yoshi's Story, Donkey Kong 64
    0x8000: SRAM,     # Ocarina of Time
    0x20000: FLASHRAM,  # Majora's Mask, Paper Mario
}

#: Said in full whenever a size is refused, because "unsupported size" tells
#: someone nothing about what to do next.
KNOWN_SIZES = ", ".join(f"{size:,}" for size in sorted(BY_SIZE))


class ConversionError(ValueError):
    """Raised rather than writing a save we cannot place with certainty."""


def region_for(size: int) -> Region:
    """Which storage a Delta save of this size belongs to.

    Refuses anything unrecognised. A save of an unexpected size is a save whose
    format we do not understand, and placing it at a guessed offset would
    produce a valid-looking `.srm` that loads as corruption -- the one failure
    mode this whole module is arranged to prevent.
    """
    region = BY_SIZE.get(size)
    if region is None:
        raise ConversionError(
            f"a {size:,}-byte N64 save is not a size this tool recognises "
            f"(expected one of {KNOWN_SIZES}). It has not been converted, "
            "because placing it at a guessed offset would corrupt it."
        )
    return region


def _checksum1(buf: bytes) -> bytes:
    total = 0
    for index in range(0, 24, 2):
        total = (total + int.from_bytes(buf[index : index + 2], "big")) & 0xFFFF
    total = (total + int.from_bytes(buf[24:26], "big")) & 0xFFFF
    total = (total + int.from_bytes(buf[26:28], "big")) & 0xFFFF
    return total.to_bytes(2, "big")


def _checksum2(checksum: bytes) -> bytes:
    return ((0xFFF2 - int.from_bytes(checksum, "big")) & 0xFFFF).to_bytes(2, "big")


#: The serial mupen64plus stamps into a pack it formats itself.
_MUPEN64_SERIAL = bytes(
    [
        0xFF, 0xFF, 0xFF, 0xFF, 0x05, 0x1A, 0x5F, 0x13,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    ]
)


def format_controller_pak() -> bytes:
    """A blank but *formatted* Controller Pak, as mupen64plus writes one.

    Filling this region with 0xFF instead would leave the pack looking corrupt,
    and the game would ask the player to format it before it would save. Since
    the structure is small and documented in the reference, writing it properly
    costs little and removes a papercut from every first sync.
    """
    pak = bytearray(PAK_SIZE)

    # Label block: 0x81 then an ascending byte pattern, then the serial and
    # the two checksums over it.
    pak[0] = 0x81
    for index in range(1, 32):
        pak[index] = index
    pak[32:56] = _MUPEN64_SERIAL
    pak[56] = 0xFF
    pak[57] = 0xFF
    pak[58] = 0x01
    pak[59] = 0xFF
    first = _checksum1(bytes(pak[32:64]))
    pak[60:62] = first
    pak[62:64] = _checksum2(first)

    # The label block is repeated at three further offsets.
    for offset in (96, 128, 192):
        pak[offset : offset + 32] = pak[32:64]

    # Index table: every entry marked free (0x0003), which is what "empty"
    # means to anything reading this pack.
    table = bytearray(256)
    table[0] = 0
    table[1] = 113
    for index in range(10, 256):
        table[index] = (index - 10) % 2 * 3
    pak[256:512] = table
    pak[512:768] = table

    return bytes(pak)


def controller_pak_is_empty(pak: bytes) -> bool:
    """True when a Controller Pak holds no saved notes.

    Reads the index table the same way the reference does: every two-byte entry
    from offset 266 must read as free space. Anything else means the player has
    data on this pack.
    """
    if len(pak) < 512:
        return True
    table = pak[256:512]
    for index in range(10, 256, 2):
        if int.from_bytes(table[index : index + 2], "big") != 0x0003:
            return False
    return True


def controller_paks(srm: bytes) -> list[bytes]:
    """The four Controller Paks inside a combined save."""
    return [
        srm[PAKS_START + i * PAK_SIZE : PAKS_START + (i + 1) * PAK_SIZE]
        for i in range(PAK_COUNT)
    ]


def has_controller_pak_data(srm: bytes) -> bool:
    """True when any of the four packs holds something worth keeping."""
    return any(not controller_pak_is_empty(pak) for pak in controller_paks(srm))


def blank_srm() -> bytes:
    """An empty combined save, with all four Controller Paks formatted."""
    data = bytearray([EMPTY]) * SRM_SIZE
    pak = format_controller_pak()
    for index in range(PAK_COUNT):
        start = PAKS_START + index * PAK_SIZE
        data[start : start + PAK_SIZE] = pak
    return bytes(data)


def to_retroarch(delta_save: bytes, existing: bytes | None = None) -> bytes:
    """Place a Delta save into RetroArch's combined save.

    ``existing`` is RetroArch's current ``.srm`` when it has one, and every byte
    of it outside the cartridge region is carried across untouched -- the
    Controller Paks above all, which Delta never syncs and which therefore only
    ever exist on this side.
    """
    region = region_for(len(delta_save))

    if existing is None:
        data = bytearray(blank_srm())
    elif len(existing) == SRM_SIZE:
        data = bytearray(existing)
    else:
        raise ConversionError(
            f"RetroArch's save is {len(existing):,} bytes, not the "
            f"{SRM_SIZE:,} mupen64plus-next writes. Refusing to edit a file "
            "whose layout is not the one this conversion was written against."
        )

    # The region is cleared before the save goes in, so a 4 Kbit EEPROM save
    # never leaves a previous 16 Kbit one trailing behind it.
    data[region.start : region.end] = bytes([EMPTY]) * region.length
    data[region.start : region.start + len(delta_save)] = delta_save
    return bytes(data)


def to_delta(srm: bytes, size: int) -> bytes:
    """Take a Delta-shaped save back out of RetroArch's combined save.

    ``size`` comes from Delta's own record rather than being inferred here. That
    is what keeps the round trip exact: the bytes going home are the same count
    that came out, so the push size guard is satisfied by construction and needs
    no per-system exemption.
    """
    region = region_for(size)
    if len(srm) != SRM_SIZE:
        raise ConversionError(
            f"RetroArch's save is {len(srm):,} bytes, not the {SRM_SIZE:,} "
            "mupen64plus-next writes; refusing to read a save out of it."
        )
    return srm[region.start : region.start + size]
