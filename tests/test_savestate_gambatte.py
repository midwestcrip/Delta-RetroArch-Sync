"""Lifting a Game Boy battery save out of a gambatte state, and the N64 finding.

Read from gambatte's ``statesaver.cpp``, then measured against a real Pokemon
Crystal state synced from Delta on 2026-09-08: 84,010 bytes, 114 fields tiling
the file exactly, and an ``sram`` field of 32,768 bytes matching Delta's own
save.

gambatte is the tidiest format here and the only one besides melonDS that says
how big its save is, so it needs nothing from Delta's record.

It is also the only one with **no magic**. ``saveState`` writes two version
bytes, a screenshot as a 24-bit big-endian length plus that many bytes, then a
flat run of fields -- each its label including the trailing null, a 24-bit
big-endian size, and the data. So the parse is the identification: fields that
tile the file exactly is not something another format does by accident, and
``read_gambatte_fields`` insists on it.

The N64 case is in here too, because it is the same question with a different
answer: there is nothing in a mupen64plus state to recover.
"""

from __future__ import annotations

import gzip
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

CRYSTAL = bytes((i * 17) % 256 for i in range(32768))


def field(label: bytes, data: bytes) -> bytes:
    """``file.write(label, labelsize)`` then ``put24(size)`` then the data."""
    return label + b"\x00" + len(data).to_bytes(3, "big") + data


def a_state(*fields: bytes, snapshot: bytes = b"") -> bytes:
    return (
        savestate.GAMBATTE_VERSION
        + len(snapshot).to_bytes(3, "big")
        + snapshot
        + b"".join(fields)
    )


def a_crystal_state(save: bytes = CRYSTAL, **kwargs) -> bytes:
    """Fields in gambatte's own alphabetical order, as the real file has them."""
    return a_state(
        field(b"a", b"\x01"),
        field(b"bgp", b"\x02" * 64),
        field(b"spucntr", b"\x03" * 4),
        field(b"sram", save),
        field(b"zram", b"\x04" * 127),
        **kwargs,
    )


class FramingTests(unittest.TestCase):
    def test_the_fields_must_tile_the_file_exactly(self) -> None:
        """The identification, since there is no magic to check."""
        good = a_crystal_state()
        self.assertEqual(len(savestate.read_gambatte_fields(good)), 5)
        # A stray byte past the last field is not a field, so the walk refuses
        # rather than treating the tail as one.
        with self.assertRaises(savestate.SaveStateError):
            savestate.read_gambatte_fields(good + b"\x00")

    def test_an_unterminated_trailing_label_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_gambatte_fields(a_crystal_state() + b"abc")
        self.assertIn("not terminated", str(caught.exception))

    def test_a_screenshot_is_skipped_by_its_declared_length(self) -> None:
        """Zero on the measured file, but the field is there and must be
        honoured or every offset after it is wrong."""
        state = a_crystal_state(snapshot=b"\xAA" * 300)
        got = savestate.extract_battery_save(state)
        self.assertEqual(got.data, CRYSTAL)

    def test_a_screenshot_longer_than_the_file_is_refused(self) -> None:
        state = bytearray(a_crystal_state())
        state[2:5] = (len(state) * 4).to_bytes(3, "big")
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_gambatte_fields(bytes(state))
        self.assertIn("does not fit", str(caught.exception))

    def test_a_truncated_field_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_gambatte_fields(a_crystal_state()[:-100])
        self.assertIn("truncated", str(caught.exception))

    def test_labels_are_read_up_to_their_null(self) -> None:
        labels = [f.label for f in savestate.read_gambatte_fields(a_crystal_state())]
        self.assertEqual(labels, [b"a", b"bgp", b"spucntr", b"sram", b"zram"])


class ExtractionTests(unittest.TestCase):
    def test_the_save_comes_back_byte_for_byte(self) -> None:
        got = savestate.extract_battery_save(a_crystal_state())
        self.assertEqual(got.data, CRYSTAL)
        self.assertEqual(got.core, "gambatte")

    def test_the_length_comes_from_the_state_not_the_record(self) -> None:
        """gambatte and melonDS are the two that say. The others do not."""
        got = savestate.extract_battery_save(a_crystal_state())
        self.assertFalse(got.size_from_record)

    def test_common_Game_Boy_save_sizes_work(self) -> None:
        for size in (2048, 8192, 32768, 131072):
            with self.subTest(size=size):
                save = bytes((i * 3) % 256 for i in range(size))
                got = savestate.extract_battery_save(a_crystal_state(save))
                self.assertEqual(got.data, save)

    def test_a_cartridge_with_no_save_says_so(self) -> None:
        state = a_state(field(b"a", b"\x01"), field(b"sram", b""))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(state)
        self.assertIn("no battery save", str(caught.exception))

    def test_a_state_without_an_sram_field_says_so(self) -> None:
        state = a_state(field(b"a", b"\x01"), field(b"bgp", b"\x02" * 64))
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(state)
        self.assertIn("no 'sram' field", str(caught.exception))


class MupenSaveStatesHoldNoSaveTests(unittest.TestCase):
    """Not "unsupported" -- there is genuinely nothing in the file.

    mupen64plus-core's ``savestates.c`` writes the flashram controller's
    registers and none of the storage behind them; the words eeprom, mempak and
    sram do not occur in it at all. Confirmed against the real Paper Mario
    state, where Delta's 131,072-byte save appears nowhere in the 16,793,412
    decompressed bytes in either byte order.
    """

    def test_an_N64_state_is_refused_with_the_reason(self) -> None:
        inner = savestate.MUPEN64PLUS_MAGIC + b"\x00\x01\x05\x00" + b"A" * 32
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(gzip.compress(inner + b"\x00" * 4096))
        message = str(caught.exception)
        self.assertIn("no battery save inside it", message)
        self.assertIn(".eep/.sra/.fla/.mpk", message)

    def test_a_GBA_state_is_gzip_too_and_goes_to_the_VBA_M_reader(self) -> None:
        """Both formats are gzip on the outside, so the dispatcher has to look
        inside. An N64 state is refused for good; a GBA one is handed to the
        VBA-M reader, which refuses this stub for a different reason -- it is
        far too short to hold the blocks that precede the save."""
        inner = b"\x0a\x00\x00\x00" + b"POKEMON FIREBPRE"
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(
                gzip.compress(inner + b"\x00" * 512), expected_size=131072
            )
        message = str(caught.exception)
        self.assertNotIn("no battery save inside it", message)
        self.assertIn("does not fit", message)

    def test_a_corrupt_gzip_state_is_refused_cleanly(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(b"\x1f\x8b\x08\x00" + b"\xFF" * 256)
        self.assertIn("will not decompress", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
