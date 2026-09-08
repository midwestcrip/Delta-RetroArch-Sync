"""Lifting a GBA battery save out of a visualboyadvance-m save state.

The hardest of the six, and the only one where the offset is *computed* rather
than read out of the file. A VBA-M state has no structure: ``CPUWriteState``
calls ``utilGzWrite`` down a list of raw buffers, nothing names or sizes any of
them, and the save is reached only by adding up everything written first.

**Searching for it instead is not an option, and that was measured.** The flash
buffer is preceded by ``flashSaveData3``, four ints; scanning the real 2 MB Fire
Red state for a plausible one gives **144 matches**, exactly one of which is
right. A pattern search would be wrong 143 times out of 144.

The sizes come from the revision Delta actually ships -- ``GBADeltaCore`` pins
``visualboyadvance-m`` at submodule commit 453fa0de, whose ``CPUWriteState``
differs from current upstream (no DMA variables, ``pix`` fixed at 4*241*162).
Computing against upstream master lands about 500 bytes off, which is precisely
the near-miss that would return plausible garbage.

Verified 2026-09-08: the computed flash descriptor offset is 594,444, which is
exactly where the real state's is, and the recovered 131,072 bytes matched
Delta's own Fire Red save.

Because it is arithmetic against one revision, it is checked before it is
trusted -- the version must be 10 and the four ints at the computed offset must
be a credible descriptor. ``RefusalTests`` is the important class in this file.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

FIRE_RED = bytes((i * 11) % 256 for i in range(131072))
TITLE = b"POKEMON FIREBPRE"


def a_state(
    save: bytes,
    *,
    version: int = 10,
    flash_size: int = 131072,
    bank: int = 0,
    eeprom: bytes | None = None,
) -> bytes:
    """A state laid out exactly as ``CPUWriteState`` lays one out.

    Decompressed -- the gzip wrapper is the dispatcher's business, not this
    reader's.
    """
    eeprom_data, flash_desc, flash_data = savestate._vbam_offsets()

    out = bytearray(flash_data + max(len(save), flash_size))
    out[0:4] = struct.pack("<i", version)
    out[4:20] = TITLE
    # Everything between the header and the EEPROM block is registers, the
    # saveGameStruct and the RAM dumps. Its *size* is what matters here, so it
    # is left as filler rather than modelled.
    struct.pack_into("<iiii", out, flash_desc, 0, 0, flash_size, bank)
    if eeprom is not None:
        out[eeprom_data : eeprom_data + len(eeprom)] = eeprom
    else:
        out[flash_data : flash_data + len(save)] = save
    return bytes(out)


class OffsetTests(unittest.TestCase):
    def test_the_computed_offsets_are_the_measured_ones(self) -> None:
        """594,444 is where the real Fire Red state's flash descriptor is. If
        the arithmetic ever drifts, it drifts away from a number that was
        checked against a real file."""
        eeprom_data, flash_desc, flash_data = savestate._vbam_offsets()
        self.assertEqual(flash_desc, 594_444)
        self.assertEqual(flash_data, 594_460)
        self.assertEqual(eeprom_data, 586_252)

    def test_the_chain_adds_up_the_way_CPUWriteState_writes_it(self) -> None:
        self.assertEqual(savestate.VBAM_PREFIX_SIZE, 4 + 16 + 4 + 180 + 267 + 4 + 4)
        self.assertEqual(savestate.VBAM_PREFIX_SIZE, 479)
        self.assertEqual(savestate.VBAM_RAM_BLOCKS, 585_224)

    def test_only_version_ten_is_accepted(self) -> None:
        self.assertEqual(savestate.VBAM_SAVE_GAME_VERSION, 10)


class ExtractionTests(unittest.TestCase):
    def test_a_flash_save_comes_back_byte_for_byte(self) -> None:
        got = savestate.extract_vbam(a_state(FIRE_RED), expected_size=131072)
        self.assertEqual(got.data, FIRE_RED)
        self.assertEqual(got.core, "visualboyadvance-m")

    def test_the_game_title_is_reported(self) -> None:
        """Read straight out of &rom[0xa0], so it is a free sanity check that
        the offsets are anchored where they should be."""
        got = savestate.extract_vbam(a_state(FIRE_RED), expected_size=131072)
        self.assertIn("POKEMON FIRE", got.save_type)

    def test_a_smaller_SRAM_save_is_a_prefix_of_the_flash_buffer(self) -> None:
        """VBA-M keeps SRAM in flashSaveMemory too, so a 32 KB save is the
        first 32 KB of the same buffer."""
        sram = bytes((i * 3) % 256 for i in range(32768))
        state = a_state(sram, flash_size=65536)
        got = savestate.extract_vbam(state, expected_size=32768)
        self.assertEqual(got.data, sram)

    def test_an_EEPROM_save_comes_from_the_EEPROM_block(self) -> None:
        """A different buffer entirely, and the size is what tells them apart."""
        for size in savestate.VBAM_EEPROM_SAVE_SIZES:
            with self.subTest(size=size):
                data = bytes((i * 9) % 256 for i in range(size))
                state = a_state(b"", eeprom=data)
                got = savestate.extract_vbam(state, expected_size=size)
                self.assertEqual(got.data, data)
                self.assertIn("EEPROM", got.save_type)

    def test_the_length_is_reported_as_coming_from_the_record(self) -> None:
        got = savestate.extract_vbam(a_state(FIRE_RED), expected_size=131072)
        self.assertTrue(got.size_from_record)


class RefusalTests(unittest.TestCase):
    """The class that matters. A wrong offset here returns 128 KB of somebody
    else's RAM shaped exactly like a save."""

    def test_a_different_save_game_version_is_refused(self) -> None:
        """The layout is the sum of every field before the save, so another
        version moves all of it."""
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(
                a_state(FIRE_RED, version=11), expected_size=131072
            )
        self.assertIn("version 11", str(caught.exception))

    def test_a_computed_offset_that_is_not_a_descriptor_is_refused(self) -> None:
        """The check that catches a different VBA-M revision. Without it, the
        arithmetic would be trusted blindly."""
        state = bytearray(a_state(FIRE_RED))
        _eeprom, flash_desc, _flash = savestate._vbam_offsets()
        struct.pack_into("<iiii", state, flash_desc, 0, 0, 12345, 7)
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(bytes(state), expected_size=131072)
        message = str(caught.exception)
        self.assertIn("not a flash descriptor", message)
        self.assertIn("not the VBA-M revision Delta ships", message)

    def test_an_implausible_flash_bank_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(
                a_state(FIRE_RED, bank=9), expected_size=131072
            )
        self.assertIn("not a flash descriptor", str(caught.exception))

    def test_a_save_larger_than_the_chip_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(
                a_state(FIRE_RED, flash_size=65536), expected_size=131072
            )
        self.assertIn("says the chip is", str(caught.exception))

    def test_a_truncated_state_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(
                a_state(FIRE_RED)[:600_000], expected_size=131072
            )
        self.assertIn("does not fit at the computed offset", str(caught.exception))

    def test_without_a_length_it_refuses(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(a_state(FIRE_RED), expected_size=None)
        self.assertIn("does not record how large", str(caught.exception))

    def test_a_stub_too_short_to_be_a_state_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_vbam(b"\x0a\x00\x00\x00", expected_size=131072)
        self.assertIn("too short", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
