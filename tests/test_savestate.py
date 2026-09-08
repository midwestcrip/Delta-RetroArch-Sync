"""Tests for lifting a battery save out of a melonDS save state.

**These fixtures are synthetic, and that is a real limitation.** No ``.svs``
exists on the development machine and Delta does not appear to sync save states
to Dropbox, so unlike every other format in this project the layout has not been
measured against a real file. The builder below is written to mirror melonDS
0.9.5's ``Savestate.cpp`` and ``NDSCart.cpp`` byte for byte -- which means these
tests prove the parser matches *the layout as read from that source*, and prove
nothing about whether Delta writes exactly that.

So the offsets and sizes are asserted as literals rather than computed from the
module. If a real file ever contradicts them, the failure should land on a
stated number that someone chose, not on arithmetic that quietly agrees with
whatever the module happens to do.

The one behaviour worth protecting above all others: the save lives in ``NDCS``,
and ``NDSC`` is a different section sitting right next to it. ``DecoySectionTests``
exists because reading the wrong one is the mistake this format invites.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

#: The four offsets that define the format, restated here as literals.
FILE_HEADER = 0x10
SECTION_HEADER = 0x10
SRAM_LENGTH_AT = 0x1C
SRAM_DATA_AT = 0x20


def build_section(magic: bytes, body: bytes) -> bytes:
    """One section, framed the way ``Savestate::Section`` frames it.

    The length counts from the start of the header and therefore includes it --
    ``len = pos - CurSection`` in melonDS. Getting this wrong by 16 bytes is the
    other easy mistake in this format, so the fixture commits to it explicitly.
    """
    length = SECTION_HEADER + len(body)
    return magic + struct.pack("<I", length) + b"\x00" * 8 + body


def build_cart_body(
    sram: bytes,
    *,
    cmd_enc: int = 0,
    data_enc: int = 0,
    dsi_mode: int = 0,
    extra: bytes = b"",
) -> bytes:
    """The inside of an ``NDCS`` section for a retail cart.

    ``CartCommon::DoSavestate`` writes the three mode words, then
    ``CartRetail::DoSavestate`` writes the length, the save, and the SPI trailer.
    ``extra`` is whatever a subclass appends after that.
    """
    return (
        struct.pack("<IIII", cmd_enc, data_enc, dsi_mode, len(sram))
        + sram
        + struct.pack("<BIB", 0, 0, 0)  # SRAMCmd, SRAMAddr, SRAMStatus
        + extra
    )


def build_state(*sections: bytes, major: int = 9, minor: int = 0) -> bytes:
    """A whole save state, with the header's length field filled in correctly."""
    body = b"".join(sections)
    total = FILE_HEADER + len(body)
    return (
        b"MELN"
        + struct.pack("<HH", major, minor)
        + struct.pack("<I", total)
        + b"\x00" * 4
        + body
    )


def a_state_holding(sram: bytes, *, extra: bytes = b"") -> bytes:
    """A plausible state: some other sections, then the cart and its save."""
    return build_state(
        build_section(b"NDSG", b"\x00" * 64),
        build_section(b"ARM9", b"\x00" * 32),
        build_section(b"NDSC", b"\x11" * 128),
        build_section(b"NDCS", build_cart_body(sram, extra=extra)),
        build_section(b"WIFI", b"\x00" * 16),
    )


PLATINUM = bytes(range(256)) * 2048  # 512 KB, the real Pokemon Platinum size


class LayoutTests(unittest.TestCase):
    def test_the_save_lives_in_NDCS_not_NDSC(self) -> None:
        """The single fact this module turns on."""
        self.assertEqual(savestate.SRAM_SECTION, b"NDCS")
        self.assertEqual(savestate.CART_CONTROLLER_SECTION, b"NDSC")
        self.assertNotEqual(
            savestate.SRAM_SECTION, savestate.CART_CONTROLLER_SECTION
        )

    def test_the_offsets_are_where_melonDS_puts_them(self) -> None:
        self.assertEqual(savestate.HEADER_SIZE, FILE_HEADER)
        self.assertEqual(savestate.SECTION_HEADER_SIZE, SECTION_HEADER)
        self.assertEqual(savestate.SRAM_LENGTH_OFFSET, SRAM_LENGTH_AT)
        self.assertEqual(savestate.SRAM_DATA_OFFSET, SRAM_DATA_AT)

    def test_the_length_field_sits_immediately_before_the_data(self) -> None:
        self.assertEqual(savestate.SRAM_LENGTH_OFFSET + 4, savestate.SRAM_DATA_OFFSET)

    def test_the_save_sizes_are_the_ones_SetupSave_can_produce(self) -> None:
        """From ``sramlen[]`` in ``CartRetail::SetupSave``, minus the zero entry."""
        self.assertEqual(
            sorted(savestate.SAVE_SIZES),
            [
                512,
                8192,
                65536,
                131072,
                262144,
                524288,
                1048576,
                8388608,
                16777216,
                67108864,
            ],
        )

    def test_the_version_is_the_one_Delta_ships(self) -> None:
        self.assertEqual(savestate.SAVESTATE_MAJOR, 9)


class SectionWalkTests(unittest.TestCase):
    def test_sections_are_found_in_order(self) -> None:
        blob = a_state_holding(b"\x00" * 512)
        magics = [s.magic for s in savestate.read_sections(blob)]
        self.assertEqual(magics, [b"NDSG", b"ARM9", b"NDSC", b"NDCS", b"WIFI"])

    def test_a_section_length_includes_its_own_header(self) -> None:
        blob = build_state(build_section(b"NDSG", b"\xAB" * 100))
        (section,) = savestate.read_sections(blob)
        self.assertEqual(section.length, SECTION_HEADER + 100)
        self.assertEqual(section.body_start, section.start + SECTION_HEADER)

    def test_the_first_section_starts_after_the_file_header(self) -> None:
        blob = build_state(build_section(b"NDSG", b""))
        (section,) = savestate.read_sections(blob)
        self.assertEqual(section.start, FILE_HEADER)

    def test_walking_stops_at_a_zero_magic(self) -> None:
        blob = build_state(build_section(b"NDSG", b"\x00" * 16)) + b"\x00" * 32
        # The trailing zeros must not be read as another section, but the
        # declared length now disagrees, so walk the sections directly.
        magics = [s.magic for s in savestate.read_sections(blob)]
        self.assertEqual(magics, [b"NDSG"])

    def test_a_section_running_past_the_end_is_an_error(self) -> None:
        blob = build_state(build_section(b"NDSG", b"\x00" * 32))[:-8]
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_sections(blob)
        self.assertIn("truncated", str(caught.exception))

    def test_a_length_shorter_than_its_header_is_an_error(self) -> None:
        blob = build_state(b"NDSG" + struct.pack("<I", 4) + b"\x00" * 8)
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_sections(blob)
        self.assertIn("smaller than its own", str(caught.exception))


class ExtractionTests(unittest.TestCase):
    def test_the_save_comes_back_byte_for_byte(self) -> None:
        extracted = savestate.extract_battery_save(a_state_holding(PLATINUM))
        self.assertEqual(extracted.data, PLATINUM)
        self.assertEqual(extracted.size, 512 * 1024)
        self.assertEqual(extracted.save_type, "FLASH 4 Mbit")

    def test_every_save_size_round_trips(self) -> None:
        for size, name in sorted(savestate.SAVE_SIZES.items()):
            if size > 1024 * 1024:
                continue  # the NAND sizes are 8-64 MB; the shape is identical
            with self.subTest(size=size, name=name):
                sram = bytes((i * 7) % 256 for i in range(size))
                extracted = savestate.extract_battery_save(a_state_holding(sram))
                self.assertEqual(extracted.data, sram)
                self.assertEqual(extracted.save_type, name)

    def test_the_version_is_reported(self) -> None:
        extracted = savestate.extract_battery_save(a_state_holding(b"\x00" * 512))
        self.assertEqual(extracted.version, (9, 0))

    def test_a_plain_retail_cart_is_recognised(self) -> None:
        extracted = savestate.extract_battery_save(a_state_holding(b"\x00" * 512))
        self.assertEqual(extracted.cart_variant, "retail")

    def test_an_infrared_cart_is_recognised(self) -> None:
        blob = a_state_holding(b"\x00" * 512, extra=b"\x00")
        self.assertEqual(
            savestate.extract_battery_save(blob).cart_variant, "retail with infrared"
        )

    def test_a_NAND_cart_is_recognised(self) -> None:
        blob = a_state_holding(b"\x00" * 512, extra=b"\x00" * (4 + 4 + 0x800 + 4))
        self.assertEqual(
            savestate.extract_battery_save(blob).cart_variant, "retail NAND"
        )

    def test_an_unrecognised_trailer_still_yields_the_save(self) -> None:
        """An unknown subclass is not a reason to withhold the save -- the save's
        own offset and length are stated, so it is readable either way."""
        blob = a_state_holding(PLATINUM, extra=b"\x00" * 99)
        extracted = savestate.extract_battery_save(blob)
        self.assertEqual(extracted.data, PLATINUM)
        self.assertIsNone(extracted.cart_variant)


class DecoySectionTests(unittest.TestCase):
    """``NDSC`` sits right next to ``NDCS`` and reading it instead is the mistake
    this format invites. These make that mistake fail loudly."""

    def test_a_convincing_NDSC_section_is_not_mistaken_for_the_cart(self) -> None:
        wrong = b"\xEE" * 512
        decoy = build_section(b"NDSC", build_cart_body(wrong))
        blob = build_state(
            decoy,
            build_section(b"NDCS", build_cart_body(PLATINUM)),
        )
        extracted = savestate.extract_battery_save(blob)
        self.assertEqual(extracted.data, PLATINUM)
        self.assertNotEqual(extracted.data[:512], wrong)

    def test_NDSC_without_NDCS_says_there_is_no_save_rather_than_reading_it(
        self,
    ) -> None:
        blob = build_state(build_section(b"NDSC", build_cart_body(b"\xEE" * 512)))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        message = str(caught.exception)
        self.assertIn("NDCS", message)
        self.assertIn("cart controller", message)


class RefusalTests(unittest.TestCase):
    def test_a_file_that_is_not_a_state_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(b"\x00" * 4096)
        self.assertIn("not a melonDS save state", str(caught.exception))

    def test_a_wrapped_state_names_the_offset_it_found(self) -> None:
        """If Delta ever does wrap the state, that overturns a documented
        finding, so the message says where rather than just refusing."""
        blob = b"DELTA!!!" + a_state_holding(b"\x00" * 512)
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        message = str(caught.exception)
        self.assertIn("0x8", message)
        self.assertIn("contradicts", message)

    def test_a_short_file_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(b"MELN")
        self.assertIn("too short", str(caught.exception))

    def test_a_different_major_version_is_refused(self) -> None:
        blob = build_state(
            build_section(b"NDCS", build_cart_body(b"\x00" * 512)), major=10
        )
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        self.assertIn("major version", str(caught.exception))

    def test_a_truncated_file_is_refused_by_the_length_field(self) -> None:
        blob = a_state_holding(PLATINUM)[:-1024]
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        self.assertIn("truncated", str(caught.exception))

    def test_a_cart_with_no_save_is_refused_clearly(self) -> None:
        blob = build_state(build_section(b"NDCS", build_cart_body(b"")))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        self.assertIn("no battery save", str(caught.exception))

    def test_an_impossible_save_size_is_refused_rather_than_copied(self) -> None:
        """The length field is trusted only after it is checked against the sizes
        the hardware can actually have. Otherwise a damaged state would have us
        copying an arbitrary number of bytes from a guessed place."""
        body = struct.pack("<IIII", 0, 0, 0, 1234) + b"\x00" * 2048
        blob = build_state(build_section(b"NDCS", body))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        self.assertIn("not a size the DS can produce", str(caught.exception))

    def test_a_save_that_does_not_fit_its_section_is_refused(self) -> None:
        body = struct.pack("<IIII", 0, 0, 0, 524288) + b"\x00" * 64
        blob = build_state(build_section(b"NDCS", body))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(blob)
        self.assertIn("does not fit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
