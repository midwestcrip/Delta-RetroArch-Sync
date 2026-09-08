"""Recovering the battery save out of a Delta save state (``.svs``).

This is a **recovery path, not a sync path**. Save states do not sync and are not
going to: a state is a memory dump whose layout moves whenever the emulator does,
and Delta 1.6 ships melonDS 0.9.5 while RetroArch's melonDS DS core wraps
melonDS 1.x. What *is* recoverable is the battery save sitting inside the state,
which is an ordinary ``.sav`` once you know where to cut. That matters when the
only copy of someone's progress is inside a state.

**A Delta DS ``.svs`` is a melonDS save state, byte for byte.** There is no Delta
wrapper -- the FAQ's "Delta save states are only compatible with Delta" is a
simplification of the version lock, not a statement about the container. If the
magic is missing this module says so rather than guessing, because a wrapper
turning up would overturn that finding and is worth hearing about.

Layout, read from melonDS 0.9.5's own ``Savestate.cpp`` and ``NDSCart.cpp``
rather than from a summary of them:

File header, 16 bytes::

    00  magic "MELN"
    04  version major (u16 LE)   -- 9 for 0.9.5; a different major is incompatible
    06  version minor (u16 LE)
    08  total file length (u32 LE)
    0C  reserved

Section header, 16 bytes, first section at ``0x10``::

    00  section magic
    04  section length (u32 LE)  -- INCLUDES these 16 bytes
    08  reserved
    0C  reserved

``Savestate::Section`` writes ``len = pos - CurSection``, measuring from the
start of the header, which is why the length includes it. Its reader walks
sections by seeking ``length - 8`` after consuming magic and length, and stops
at a zero magic. Both are mirrored below.

**The battery save lives in section ``NDCS``, not ``NDSC``.** Those are two
different sections whose names are transpositions of each other, they sit next
to each other in the file, and picking the wrong one lands you in the middle of
a 16 KB transfer buffer:

- ``NDSC`` is ``NDSCart::DoSavestate`` -- the cart *controller*: SPI registers,
  ``TransferData[0x4000]``, the cart type and checksum.
- ``NDCS`` is ``CartCommon::DoSavestate`` -- the cart *itself*, and
  ``CartRetail::DoSavestate`` appends the SRAM to it immediately afterwards.

Inside ``NDCS``, counting from the start of its header::

    00  section header (16 bytes)
    10  CmdEncMode  (u32)
    14  DataEncMode (u32)
    18  DSiMode     (Bool32 -- written as u32)
    1C  SRAMLength  (u32)
    20  the battery save, SRAMLength bytes
        SRAMCmd (u8), SRAMAddr (u32), SRAMStatus (u8)
        then whatever the cart subclass adds

So the save begins at ``NDCS + 0x20`` and its length is stated four bytes
earlier. Nothing here is inferred from the data itself, which is what keeps the
extraction exact -- the same property that makes the N64 conversion exact.

**Unverified against a real file.** No ``.svs`` exists on the development
machine, so every claim above is read from melonDS's source and none has been
measured. That is a weaker footing than anything else in this project, and it is
why this module validates hard and refuses rather than guessing: a recovery tool
that hands back a plausible-looking wrong file is worse than one that declines.

**Getting a real one is easy, though, and does not need a cable.** ``SaveState``
conforms to ``Syncable`` with ``syncableFiles`` of ``saveState`` and
``thumbnail``, so a state syncs to Dropbox like anything else and lands as
``SaveState-<uuid>-saveState``. One condition, from ``isSyncingEnabled``::

    (self.type != .auto && self.type != .quick)

**Auto-saves and quick-saves do not sync.** Only a manual save state does.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: melonDS's savestate magic, at offset 0 of an unwrapped state.
MAGIC = b"MELN"

#: Both the file header and every section header are this long.
HEADER_SIZE = 0x10
SECTION_HEADER_SIZE = 0x10

#: ``SAVESTATE_MAJOR`` in melonDS 0.9.5's ``Savestate.h``. melonDS itself treats
#: a different major as incompatible and refuses to load, so we do too.
SAVESTATE_MAJOR = 9

#: The cart section carrying the SRAM. Not ``NDSC`` -- see the module docstring.
SRAM_SECTION = b"NDCS"

#: The cart *controller* section. Named here only so the error message can tell
#: someone who went looking for the wrong one what they actually found.
CART_CONTROLLER_SECTION = b"NDSC"

#: Where ``SRAMLength`` and the save itself sit, from the start of ``NDCS``.
SRAM_LENGTH_OFFSET = 0x1C
SRAM_DATA_OFFSET = 0x20

#: Bytes ``CartRetail::DoSavestate`` writes after the save: ``SRAMCmd`` (u8),
#: ``SRAMAddr`` (u32), ``SRAMStatus`` (u8).
SRAM_TRAILER_SIZE = 6

#: Every save length melonDS can produce, from ``CartRetail::SetupSave``'s
#: ``sramlen[]``. A length outside this table means we are not reading a save
#: length, so extraction refuses rather than trusting it as a byte count.
SAVE_SIZES: dict[int, str] = {
    512: "EEPROM 4 Kbit",
    8 * 1024: "EEPROM 64 Kbit",
    64 * 1024: "EEPROM 512 Kbit",
    128 * 1024: "EEPROM 1 Mbit",
    256 * 1024: "FLASH 2 Mbit",
    512 * 1024: "FLASH 4 Mbit",
    1024 * 1024: "FLASH 8 Mbit",
    8192 * 1024: "NAND 64 Mbit",
    16384 * 1024: "NAND 128 Mbit",
    65536 * 1024: "NAND 512 Mbit",
}

#: What each cart subclass adds after ``CartRetail``'s trailer, so an unexpected
#: remainder can be reported as "this is not a shape we recognise" instead of
#: being silently ignored. ``CartRetailNAND`` adds ``SRAMBase``, ``SRAMWindow``,
#: ``SRAMWriteBuffer[0x800]`` and ``SRAMWritePos``; ``CartRetailIR`` adds
#: ``IRCmd``; ``CartRetailBT`` adds nothing.
CART_VARIANTS: dict[int, str] = {
    0: "retail",
    1: "retail with infrared",
    4 + 4 + 0x800 + 4: "retail NAND",
}


class SaveStateError(ValueError):
    """A ``.svs`` that cannot be read, or cannot be read safely."""


@dataclass(frozen=True)
class Section:
    """One section of a save state, as the file itself delimits them."""

    magic: bytes
    #: Offset of the section header.
    start: int
    #: Length from ``start``, including the 16-byte header.
    length: int

    @property
    def body_start(self) -> int:
        return self.start + SECTION_HEADER_SIZE

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class ExtractedSave:
    """A battery save lifted out of a state, with what was known about it."""

    data: bytes
    #: Human name for the size, from :data:`SAVE_SIZES`.
    save_type: str
    #: Which cart subclass the trailing bytes match, or ``None`` if unrecognised.
    cart_variant: str | None
    #: ``(major, minor)`` of the state format.
    version: tuple[int, int]

    @property
    def size(self) -> int:
        return len(self.data)


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def find_magic(blob: bytes, *, limit: int = 1 << 20) -> int | None:
    """Where ``MELN`` occurs, if it is not at the start.

    Only used to explain a failure. A hit at a non-zero offset would mean Delta
    wraps the state after all, which contradicts the finding this module is
    built on -- so it is worth naming the offset rather than reporting a flat
    "not a save state".
    """
    found = blob.find(MAGIC, 0, limit)
    return None if found < 0 else found


def read_sections(blob: bytes) -> list[Section]:
    """Every section, walked the way melonDS's own reader walks them.

    Stops at a zero magic, which is how ``Savestate::Section`` detects the end.
    A section that runs past the end of the file, or one that cannot advance,
    is an error rather than something to skip -- a malformed state should not
    silently yield a short list that happens to omit the section we want.
    """
    sections: list[Section] = []
    offset = HEADER_SIZE
    while offset + SECTION_HEADER_SIZE <= len(blob):
        magic = blob[offset : offset + 4]
        if magic == b"\0\0\0\0":
            break
        length = _u32(blob, offset + 4)
        if length < SECTION_HEADER_SIZE:
            raise SaveStateError(
                f"section {magic!r} at {offset:#x} claims a length of {length} "
                f"bytes, which is smaller than its own {SECTION_HEADER_SIZE}-byte "
                "header. The file is damaged or is not a melonDS save state."
            )
        if offset + length > len(blob):
            raise SaveStateError(
                f"section {magic!r} at {offset:#x} claims {length} bytes but only "
                f"{len(blob) - offset} remain. The file is truncated."
            )
        sections.append(Section(magic=magic, start=offset, length=length))
        offset += length
    return sections


def _check_header(blob: bytes) -> tuple[int, int]:
    """Validate the file header and return its version, or explain the refusal."""
    if len(blob) < HEADER_SIZE:
        raise SaveStateError(
            f"file is {len(blob)} bytes, too short to be a save state."
        )

    if blob[:4] != MAGIC:
        elsewhere = find_magic(blob)
        if elsewhere is not None:
            raise SaveStateError(
                f"this file does not start with {MAGIC.decode()}, but does "
                f"contain it at offset {elsewhere:#x}. That would mean Delta "
                "wraps the state rather than writing it bare, which contradicts "
                "what this tool was built on -- worth reporting rather than "
                "working around."
            )
        raise SaveStateError(
            f"not a melonDS save state: expected {MAGIC.decode()} at the start, "
            f"found {blob[:4]!r}. Only Nintendo DS states have this format; a "
            "state for another system is that core's own format and this tool "
            "cannot read it."
        )

    major, minor = struct.unpack_from("<HH", blob, 4)
    if major != SAVESTATE_MAJOR:
        raise SaveStateError(
            f"save state version {major}.{minor}, but this reader understands "
            f"major version {SAVESTATE_MAJOR} (melonDS 0.9.5, which is what "
            "Delta ships). melonDS treats a different major as incompatible, so "
            "the layout cannot be assumed to match."
        )

    declared = _u32(blob, 8)
    if declared != len(blob):
        raise SaveStateError(
            f"the header says the file is {declared} bytes but it is "
            f"{len(blob)}. melonDS refuses to load a state whose length "
            "disagrees, and so does this. The file is probably truncated."
        )
    return major, minor


def extract_battery_save(blob: bytes) -> ExtractedSave:
    """Lift the battery save out of a melonDS save state.

    Raises :class:`SaveStateError` on anything it cannot read with certainty.
    Refusing is the safe outcome here: this is a last-resort recovery for a save
    that exists nowhere else, and a wrong answer that looks right is worse than
    no answer.
    """
    version = _check_header(blob)
    sections = read_sections(blob)

    cart = next((s for s in sections if s.magic == SRAM_SECTION), None)
    if cart is None:
        found = ", ".join(sorted({s.magic.decode("latin-1") for s in sections}))
        if any(s.magic == CART_CONTROLLER_SECTION for s in sections):
            raise SaveStateError(
                f"no {SRAM_SECTION.decode()} section. The state has "
                f"{CART_CONTROLLER_SECTION.decode()}, the cart controller, but "
                "not the cart itself -- so there is no battery save in it. A "
                "state taken with no cartridge save, or a homebrew cart, looks "
                "like this."
            )
        raise SaveStateError(
            f"no {SRAM_SECTION.decode()} section in the state. Sections present: "
            f"{found or 'none'}."
        )

    if cart.start + SRAM_DATA_OFFSET > len(blob):
        raise SaveStateError(
            f"the {SRAM_SECTION.decode()} section is truncated before the save "
            "length field."
        )

    length = _u32(blob, cart.start + SRAM_LENGTH_OFFSET)
    if length == 0:
        raise SaveStateError(
            "this state's cartridge has no battery save -- melonDS recorded a "
            "save length of zero. There is nothing to recover."
        )
    if length not in SAVE_SIZES:
        raise SaveStateError(
            f"the save length field reads {length} bytes, which is not a size "
            "the DS can produce. Either this is not the field it should be, or "
            "the state is damaged. Refusing rather than copying that many bytes "
            "from a guessed offset."
        )

    start = cart.start + SRAM_DATA_OFFSET
    end = start + length
    if end + SRAM_TRAILER_SIZE > cart.end:
        raise SaveStateError(
            f"a {length}-byte save does not fit in the "
            f"{SRAM_SECTION.decode()} section, which is {cart.length} bytes. "
            "The state is damaged."
        )

    remainder = cart.end - (end + SRAM_TRAILER_SIZE)
    return ExtractedSave(
        data=blob[start:end],
        save_type=SAVE_SIZES[length],
        cart_variant=CART_VARIANTS.get(remainder),
        version=version,
    )
