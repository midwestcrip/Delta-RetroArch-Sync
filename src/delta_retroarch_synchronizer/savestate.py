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

**VERIFIED against a real Delta save state, 2026-09-08.** A manual state of
Pokémon Platinum, 19,643,269 bytes, synced from Delta on the user's phone:

- ``MELN`` at offset 0. **No Delta wrapper**, as the FAQ's wording had made
  people assume. The container is melonDS's, unmodified.
- Version ``(9, 0)`` -- ``SAVESTATE_MAJOR`` 9, confirming Delta 1.6 ships
  melonDS 0.9.5.
- The header's declared length equals the file size exactly.
- 26 sections, walked cleanly. ``NDSC`` and ``NDCS`` really do sit **next to
  each other**, which is what made the transposition worth guarding against.
- The save came out at ``NDCS + 0x20``: 524,288 bytes, FLASH 4 Mbit, retail
  cart -- and **SHA-1 identical to Delta's own battery save for that game**.

So the extraction is correct, not merely well-formed. Everything above was read
from melonDS 0.9.5's source before any of it could be measured, and the
measurement agreed with all of it.

The strict validation stays exactly as it was. It cost nothing to keep and it is
what makes the failure modes legible on a file nobody has seen yet.

**Where a test file comes from,** since it is not obvious: ``SaveState``
conforms to ``Syncable`` with ``syncableFiles`` of ``saveState`` and
``thumbnail``, so a state syncs to Dropbox like anything else and lands as
``SaveState-<uuid>-saveState``. One condition, from ``isSyncingEnabled``::

    (self.type != .auto && self.type != .quick)

**Auto-saves and quick-saves do not sync.** Only a manual save state does.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

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
    #: ``(major, minor)`` of the state format. Formats with a single version
    #: number report it as the major with a zero minor.
    version: tuple[int, int]
    #: Which emulator's state this came out of.
    core: str = "melonDS"
    #: True when the length came from Delta's record rather than from the state
    #: itself. Only melonDS states say how long their save is; the rest store a
    #: fixed-size buffer, so the real length has to come from outside.
    size_from_record: bool = False

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


def extract_melonds(blob: bytes) -> ExtractedSave:
    """Lift the battery save out of a melonDS save state.

    Raises :class:`SaveStateError` on anything it cannot read with certainty.
    Refusing is the safe outcome here: this is a last-resort recovery for a save
    that exists nowhere else, and a wrong answer that looks right is worse than
    no answer.

    melonDS is the one format that states its own save length, so this needs
    nothing from outside the file.
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


def extract_battery_save(
    blob: bytes, *, expected_size: int | None = None
) -> ExtractedSave:
    """Lift the battery save out of whichever emulator's state this is.

    Every system Delta supports runs a different emulator, so a ``.svs`` is
    whichever of their state formats applies. Dispatch is on the magic, because
    that is the only thing the file itself is willing to say.

    ``expected_size`` is the length Delta's record gives for this game's save.
    melonDS states do not need it. The others store a fixed-size buffer with the
    save as a prefix, so without it they refuse rather than guess.
    """
    if blob.startswith(MAGIC):
        return extract_melonds(blob)
    if blob.startswith(SNES9X_MAGIC):
        return extract_snes9x(blob, expected_size)

    # Not a format we read. Say which one it is when the magic identifies it,
    # so the answer is "not supported yet" rather than "unreadable".
    known = IDENTIFIED_FORMATS.get(blob[:4])
    if known:
        raise SaveStateError(
            f"this is a {known} save state. Only Nintendo DS (melonDS) and "
            "Super Nintendo (snes9x) states can be read so far."
        )
    elsewhere = find_magic(blob)
    if elsewhere is not None:
        raise SaveStateError(
            f"this file does not start with {MAGIC.decode()}, but does contain "
            f"it at offset {elsewhere:#x}. That would mean Delta wraps the "
            "state rather than writing it bare, which contradicts what this "
            "tool was built on -- worth reporting rather than working around."
        )
    raise SaveStateError(
        f"not a save state this tool can read: it begins {blob[:4]!r}, which "
        "matches no format known here."
    )


# ---------------------------------------------------------------------------
# snes9x
#
# Read from snes9x's own ``snapshot.cpp``, then measured against a real Super
# Mario World state synced from Delta.
#
# The file is text-framed. A 14-byte header, ``"#!s9xsnp:%04d\n"``, then a run
# of blocks, each an 11-byte header followed by its payload::
#
#     0   3-character name
#     3   ':'
#     4   six decimal digits of length, or "------"
#    10   ':'
#
# ``FreezeBlock`` writes the length with ``sprintf("%s:%06d:")`` when it fits in
# six digits. When it does not, it writes ``"------"`` and packs the length as a
# big-endian uint32 into bytes 6..9 of the same header, which ``CheckBlockName``
# reads back. Both are handled below; the packed form is rare but a state
# containing one would otherwise be walked into nonsense.
#
# **The block length is not the save length.** ``FreezeBlock(stream, "SRA",
# Memory.SRAM, Memory.SRAM_SIZE)`` writes snes9x's whole fixed SRAM buffer --
# ``SRAM_SIZE`` is a compile-time constant, 0x20000 in the build Delta ships and
# 0x80000 in snes9x today. The cartridge's real SRAM size comes from the ROM
# header at load time and is never written into the state.
#
# Measured on the real file: the ``SRA`` block was 131,072 bytes, of which the
# first 2,048 were byte-for-byte Delta's Super Mario World save and the
# remaining 129,024 were all 0x60 filler. So the save is a *prefix* of the
# block, and nothing in the state says how long that prefix is.
#
# Which is why extraction needs the length from Delta's record, exactly as the
# N64 conversion takes its byte count from the record rather than inferring it
# from the ``.srm``. Guessing from the filler would work on this file and lose
# data on a save that legitimately ends in a run of one byte. No length, no
# extraction.
# ---------------------------------------------------------------------------

SNES9X_MAGIC = b"#!s9xsnp"
#: ``"%s:%04d\n"`` -- magic, colon, four version digits, newline.
SNES9X_HEADER_SIZE = 14
#: Name, colon, six length characters, colon.
SNES9X_BLOCK_HEADER_SIZE = 11
#: The block holding the cartridge's battery-backed RAM.
SNES9X_SRAM_BLOCK = b"SRA"


@dataclass(frozen=True)
class Block:
    """One snes9x block.

    Note the difference from :class:`Section`, which is melonDS's: there
    ``length`` includes the header, here it does not. The two formats disagree
    and conflating them is an easy 11 or 16 bytes of drift.
    """

    name: bytes
    #: Offset of the block header.
    start: int
    #: Payload length, excluding the 11-byte header.
    length: int

    @property
    def payload_start(self) -> int:
        return self.start + SNES9X_BLOCK_HEADER_SIZE

    @property
    def end(self) -> int:
        return self.payload_start + self.length


def read_snes9x_blocks(blob: bytes) -> list[Block]:
    """Walk a snes9x state's blocks the way ``CheckBlockName`` walks them."""
    blocks: list[Block] = []
    offset = SNES9X_HEADER_SIZE
    while offset + SNES9X_BLOCK_HEADER_SIZE <= len(blob):
        header = blob[offset : offset + SNES9X_BLOCK_HEADER_SIZE]
        if header[3:4] != b":":
            break
        if header[4:5] == b"-":
            length = int.from_bytes(header[6:10], "big")
        else:
            try:
                length = int(header[4:10].decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                break
        if length <= 0:
            break
        end = offset + SNES9X_BLOCK_HEADER_SIZE + length
        if end > len(blob):
            raise SaveStateError(
                f"block {header[:3]!r} at {offset:#x} claims {length} bytes but "
                f"only {len(blob) - offset - SNES9X_BLOCK_HEADER_SIZE} remain. "
                "The state is truncated."
            )
        blocks.append(Block(name=header[:3], start=offset, length=length))
        offset = end
    return blocks


def extract_snes9x(blob: bytes, expected_size: int | None) -> ExtractedSave:
    """Lift the battery save out of a snes9x state.

    ``expected_size`` is required and comes from Delta's record. See the note
    above: the state stores a fixed buffer and never says how much of it is the
    cartridge's.
    """
    if len(blob) < SNES9X_HEADER_SIZE or not blob.startswith(SNES9X_MAGIC):
        raise SaveStateError("not a snes9x save state.")

    try:
        version = int(blob[9:13].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise SaveStateError(
            f"unreadable snes9x snapshot version in {blob[:14]!r}."
        ) from None

    blocks = read_snes9x_blocks(blob)
    sram = next((b for b in blocks if b.name == SNES9X_SRAM_BLOCK), None)
    if sram is None:
        found = ", ".join(sorted({b.name.decode("latin-1") for b in blocks}))
        raise SaveStateError(
            f"no {SNES9X_SRAM_BLOCK.decode()} block in the state. Blocks "
            f"present: {found or 'none'}."
        )

    if expected_size is None:
        raise SaveStateError(
            "a snes9x state does not record how large the cartridge's save is "
            f"-- its {SNES9X_SRAM_BLOCK.decode()} block is a fixed "
            f"{sram.length:,}-byte buffer whose tail is filler. The length has "
            "to come from Delta's record for this game, so this only works on "
            "a state still sitting in Delta's synced folder. Refusing rather "
            "than handing back the whole buffer."
        )

    if expected_size <= 0 or expected_size > sram.length:
        raise SaveStateError(
            f"Delta's record says the save is {expected_size:,} bytes, which "
            f"does not fit in the {sram.length:,}-byte "
            f"{SNES9X_SRAM_BLOCK.decode()} block. Refusing."
        )

    start = sram.payload_start
    return ExtractedSave(
        data=blob[start : start + expected_size],
        save_type=f"cartridge SRAM, {expected_size:,} B",
        cart_variant=None,
        version=(version, 0),
        core="snes9x",
        size_from_record=True,
    )


# ---------------------------------------------------------------------------
# Checking an extraction against Delta's own copy
#
# A state that synced from Delta lands in the same flat folder as everything
# else, next to the record naming its game -- which means the game's *battery
# save* is usually sitting right there too. Comparing the two turns the first
# real run into a self-checking experiment instead of something someone has to
# remember to verify by hand.
#
# This is a report, never a gate. Extraction has already succeeded by the time
# any of this runs, and every way it can fail to find a counterpart returns
# None. A state copied off the phone by hand, or one whose game has no save yet,
# simply gets no comparison.
# ---------------------------------------------------------------------------

#: The file identifier Delta attaches the state itself under, from
#: ``SaveState.syncableFiles``. The record is the same name without it.
STATE_FILE_SUFFIX = "-saveState"

#: How much of each state the listing reads to identify it. A DS state is ~20 MB
#: and there can be many, so this must stay small; sixteen bytes clears the
#: longest magic with room to spare.
MAGIC_PEEK = 16


@dataclass(frozen=True)
class DeltaComparison:
    """An extracted save measured against Delta's own battery save."""

    battery_path: Path
    game_name: str | None
    #: ``None`` when the two are different lengths, since a byte count would be
    #: meaningless -- and a length mismatch is the interesting failure anyway.
    differing_bytes: int | None
    delta_size: int
    extracted_size: int

    @property
    def identical(self) -> bool:
        return self.differing_bytes == 0

    @property
    def same_length(self) -> bool:
        return self.delta_size == self.extracted_size

    def describe(self) -> str:
        """One line saying how much this run proves."""
        if self.identical:
            return (
                f"identical to Delta's own battery save ({self.delta_size:,} B). "
                "The extraction is confirmed correct, not merely well-formed."
            )
        if not self.same_length:
            return (
                f"DIFFERENT LENGTH from Delta's battery save: recovered "
                f"{self.extracted_size:,} B against Delta's {self.delta_size:,} B. "
                "That should not happen and is worth investigating before "
                "trusting either file."
            )
        assert self.differing_bytes is not None
        share = self.differing_bytes / self.delta_size * 100
        # A handful of bytes out of half a megabyte rounds to "0.00%", which
        # reads as a bug rather than as reassurance. Below a hundredth of a
        # percent the count is the only number worth printing.
        measured = f" ({share:.2f}%)" if share >= 0.01 else ""
        return (
            f"same length as Delta's battery save, {self.differing_bytes:,} of "
            f"{self.delta_size:,} bytes differ{measured}. Expected if the game "
            "wrote to SRAM after its last in-game save; a large share is not."
        )


def is_in_delta_folder(state_path: Path) -> bool:
    """Whether this state is still sitting in Delta's synced folder.

    Decided by whether its record sits beside it, which is the same link the
    cross-check follows. Matters because the recovered save must never be
    written *into* that folder: it would be a new file in another app's storage,
    and Dropbox would dutifully sync it to every device.
    """
    name = state_path.name
    if not name.lower().endswith(STATE_FILE_SUFFIX.lower()):
        return False
    return (state_path.parent / name[: -len(STATE_FILE_SUFFIX)]).is_file()


def suggested_output(state_path: Path, fallback_dir: Path) -> Path:
    """Where to put the recovered save when the user has not said.

    Beside the state, unless that is Delta's folder -- then somewhere of ours.
    Named after the game when the records can say what it is, because
    ``SaveState-1B4E28BA-2FA1-11D2-883F-0016D3CCA427-saveState.sav`` is not a
    filename anyone wants to look at afterwards.
    """
    from . import harmony

    name: str | None = None
    if is_in_delta_folder(state_path):
        folder = state_path.parent
        record = harmony.parse_record(
            folder / state_path.name[: -len(STATE_FILE_SUFFIX)]
        )
        if record is not None:
            game_id = record.related_identifier("game")
            if game_id:
                game = harmony.parse_record(folder / f"Game-{game_id}")
                if game is not None and game.name:
                    name = game.name
        stem = name or state_path.name[: -len(STATE_FILE_SUFFIX)]
        safe = "".join(c for c in stem if c not in r'<>:"/\|?*').strip() or "recovered"
        return fallback_dir / f"{safe}.sav"

    return state_path.with_suffix(".sav")


def delta_battery_save(state_path: Path) -> Path | None:
    """Delta's own battery save for the game this state belongs to.

    The link runs through the records: the ``SaveState`` record names its game,
    and the game's identifier names the ``GameSave`` file. ``None`` whenever any
    link is missing, which is the normal case for a state copied out of Delta's
    folder first.

    Two callers want this. The cross-check compares against it, and every format
    except melonDS needs its *length* -- those states store a fixed-size buffer
    and never say how much of it is the cartridge's.
    """
    from . import harmony

    name = state_path.name
    if not name.lower().endswith(STATE_FILE_SUFFIX.lower()):
        return None

    folder = state_path.parent
    record_path = folder / name[: -len(STATE_FILE_SUFFIX)]
    if not record_path.is_file():
        return None

    record = harmony.parse_record(record_path)
    if record is None or record.type.lower() != "savestate":
        return None

    game_id = record.related_identifier("game")
    if not game_id:
        return None

    battery = folder / f"GameSave-{game_id}-gameSave"
    return battery if battery.is_file() else None


def delta_save_size(state_path: Path) -> int | None:
    """How many bytes Delta holds for this game's battery save, if it can say."""
    battery = delta_battery_save(state_path)
    if battery is None:
        return None
    try:
        return battery.stat().st_size
    except OSError:
        return None


def compare_with_delta(state_path: Path, extracted: bytes) -> DeltaComparison | None:
    """Compare an extracted save against Delta's battery save for the same game.

    Returns ``None`` when there is nothing to compare against -- see
    :func:`delta_battery_save`.
    """
    from . import harmony

    battery = delta_battery_save(state_path)
    if battery is None:
        return None

    try:
        delta_bytes = battery.read_bytes()
    except OSError:
        return None

    folder = state_path.parent
    record = harmony.parse_record(
        folder / state_path.name[: -len(STATE_FILE_SUFFIX)]
    )
    game_id = record.related_identifier("game") if record is not None else None
    game_record = (
        harmony.parse_record(folder / f"Game-{game_id}") if game_id else None
    )
    game_name = game_record.name if game_record is not None else None

    differing: int | None = None
    if len(delta_bytes) == len(extracted):
        differing = sum(1 for a, b in zip(delta_bytes, extracted) if a != b)

    return DeltaComparison(
        battery_path=battery,
        game_name=game_name,
        differing_bytes=differing,
        delta_size=len(delta_bytes),
        extracted_size=len(extracted),
    )


# ---------------------------------------------------------------------------
# Finding the save states Delta has synced
#
# Typing a path with a UUID in it is a needless way to get something wrong, and
# the interesting question is usually "what have I got?" rather than "read this
# exact file". Listing them also answers, for free, the open question of what
# the *other* cores write into a .svs -- every state that is not melonDS shows
# its own magic instead of being a silent failure.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundState:
    """A save state sitting in Delta's synced folder."""

    path: Path
    game_name: str | None
    #: Display name of the system, when the game's record identifies one.
    system: str | None
    #: The emulator Delta runs for that system, which is whose state format
    #: this file is in. The reason only DS is readable today: each of these is
    #: a different emulator with its own layout.
    delta_core: str | None
    #: What the user called the slot in Delta.
    slot_name: str | None
    size: int
    #: The first bytes of the file. Long enough to tell the formats apart --
    #: melonDS's magic is four bytes, snes9x's is eight.
    magic: bytes

    @property
    def format_name(self) -> str | None:
        """Which emulator's format this is, when the magic identifies one."""
        for prefix, name in READABLE_FORMATS.items():
            if self.magic.startswith(prefix):
                return name
        for prefix, name in IDENTIFIED_FORMATS.items():
            if self.magic.startswith(prefix):
                return name
        return None

    @property
    def recoverable(self) -> bool:
        return any(self.magic.startswith(p) for p in READABLE_FORMATS)

    def describe_format(self) -> str:
        """What this state is, in the terms someone reading a list needs."""
        if self.recoverable:
            return f"{self.format_name} - can recover"
        printable = "".join(
            chr(b) if 0x20 <= b < 0x7F else "." for b in self.magic[:4]
        )
        core = self.delta_core or "another core"
        return f"{core} - not readable ({printable})"


def find_states(folder: Path) -> list[FoundState]:
    """Every synced save state in Delta's folder, newest game name first.

    Reads only the first four bytes of each state. A DS state is ~16 MB and
    there may be a lot of them, so identifying the format must not mean loading
    them all.
    """
    from . import harmony, systems

    found: list[FoundState] = []
    for path in sorted(folder.glob("*")):
        if not path.is_file():
            continue
        if not path.name.lower().endswith(STATE_FILE_SUFFIX.lower()):
            continue

        record = harmony.parse_record(folder / path.name[: -len(STATE_FILE_SUFFIX)])
        game_name: str | None = None
        system_name: str | None = None
        core_name: str | None = None
        slot_name: str | None = None
        if record is not None:
            slot_name = record.name
            game_id = record.related_identifier("game")
            if game_id:
                game = harmony.parse_record(folder / f"Game-{game_id}")
                if game is not None:
                    game_name = game.name
                    system = systems.for_delta_type(str(game.fields.get("type", "")))
                    if system is not None:
                        system_name = system.name
                        core_name = system.delta_core

        try:
            with path.open("rb") as handle:
                # Enough to tell every known format apart without reading a
                # state that can be 20 MB. snes9x's magic alone is eight bytes.
                magic = handle.read(MAGIC_PEEK)
            size = path.stat().st_size
        except OSError:
            continue

        found.append(
            FoundState(
                path=path,
                game_name=game_name,
                system=system_name,
                delta_core=core_name,
                slot_name=slot_name,
                size=size,
                magic=magic,
            )
        )

    return sorted(found, key=lambda s: ((s.game_name or "").lower(), s.path.name))


# ---------------------------------------------------------------------------
# The format registries
#
# At the bottom because they name every format in this module and so have to
# follow all of them. Nothing reads them at import time -- the dispatcher and
# the listing both look them up when called.
# ---------------------------------------------------------------------------

#: Formats this tool can actually extract from, by magic prefix. The prefixes
#: are mutually exclusive, so lookup order does not matter.
READABLE_FORMATS: dict[bytes, str] = {
    MAGIC: "melonDS",
    SNES9X_MAGIC: "snes9x",
}

#: Magic bytes measured on real Delta save states, 2026-09-08, one manual state
#: per system. Used to answer "not supported yet" rather than "unreadable".
IDENTIFIED_FORMATS: dict[bytes, str] = {
    b"NST\x1a": "Nintendo Entertainment System (nestopia)",
    b"\x1f\x8b\x08\x00": "gzip-compressed (mupen64plus or visualboyadvance-m)",
    b"\x00\x01\x00\x00": "Game Boy (gambatte)",
}
