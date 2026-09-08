"""Lifting a battery save out of a snes9x save state.

Read from snes9x's own ``snapshot.cpp``, then measured against a real Super
Mario World state synced from Delta on 2026-09-08. The ``SRA`` block was 131,072
bytes; the first 2,048 were byte-for-byte Delta's save and the remaining 129,024
were all ``0x60`` filler.

**That is the whole difficulty of this format.** ``FreezeBlock(stream, "SRA",
Memory.SRAM, Memory.SRAM_SIZE)`` writes snes9x's fixed SRAM buffer --
``SRAM_SIZE`` is a compile-time constant, 0x20000 in Delta's build and 0x80000
in snes9x today -- and the cartridge's real size comes from the ROM header at
load time, never from the state. So the save is a prefix of the block and the
state does not say how long it is.

The length therefore comes from Delta's record, the same rule ``n64.py`` follows
for extraction. Without it, extraction refuses. Guessing from the filler would
work on this file and silently truncate a save that legitimately ends in a run
of one byte, and truncating a save is the failure this project exists to avoid.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

#: The header snes9x writes: ``"%s:%04d\n"``.
HEADER = b"#!s9xsnp:0009\n"
#: Filler seen in the tail of the real file's SRA block.
FILLER = 0x60


def block(name: bytes, payload: bytes) -> bytes:
    """One block, framed as ``FreezeBlock`` frames it: ``NAME:%06d:`` + payload."""
    return name + b":%06d:" % len(payload) + payload


def packed_block(name: bytes, payload: bytes) -> bytes:
    """The oversize form: ``NAME:------:`` with the length packed big-endian.

    ``FreezeBlock`` switches to this above 999999 bytes, writing the length into
    bytes 6..9 of the same 11-byte header. Rare, but a state containing one
    would otherwise be walked into nonsense.
    """
    header = bytearray(name + b":------:")
    header[6:10] = len(payload).to_bytes(4, "big")
    return bytes(header) + payload


def a_state(sram_block: bytes, *, extra: bytes = b"") -> bytes:
    return (
        HEADER
        + block(b"NAM", b"Removed\x00")
        + block(b"CPU", b"\x01" * 48)
        + block(b"VRA", b"\x02" * 512)
        + block(b"RAM", b"\x03" * 1024)
        + sram_block
        + extra
    )


def sra(save: bytes, buffer_size: int = 0x20000) -> bytes:
    """An ``SRA`` block: the save, then filler out to the fixed buffer size."""
    return block(b"SRA", save + bytes([FILLER]) * (buffer_size - len(save)))


SMW = bytes((i * 31) % 256 for i in range(2048))


class FramingTests(unittest.TestCase):
    def test_the_header_is_fourteen_bytes(self) -> None:
        self.assertEqual(savestate.SNES9X_HEADER_SIZE, 14)
        self.assertEqual(len(HEADER), 14)

    def test_a_block_header_is_eleven_bytes(self) -> None:
        self.assertEqual(savestate.SNES9X_BLOCK_HEADER_SIZE, 11)
        self.assertEqual(len(block(b"SRA", b"")[:11]), 11)

    def test_block_length_excludes_the_header(self) -> None:
        """The opposite of melonDS, where the length includes it. Conflating the
        two is 11 or 16 bytes of drift that still parses."""
        blocks = savestate.read_snes9x_blocks(a_state(sra(SMW)))
        by_name = {b.name: b for b in blocks}
        self.assertEqual(by_name[b"RAM"].length, 1024)
        self.assertEqual(
            by_name[b"RAM"].payload_start, by_name[b"RAM"].start + 11
        )

    def test_every_block_is_found_in_order(self) -> None:
        names = [b.name for b in savestate.read_snes9x_blocks(a_state(sra(SMW)))]
        self.assertEqual(names, [b"NAM", b"CPU", b"VRA", b"RAM", b"SRA"])

    def test_the_packed_length_form_is_walked_correctly(self) -> None:
        state = (
            HEADER
            + packed_block(b"BIG", b"\x07" * 1_000_005)
            + sra(SMW)
        )
        names = [b.name for b in savestate.read_snes9x_blocks(state)]
        self.assertEqual(names, [b"BIG", b"SRA"])
        extracted = savestate.extract_battery_save(state, expected_size=2048)
        self.assertEqual(extracted.data, SMW)

    def test_a_block_running_past_the_end_is_an_error(self) -> None:
        state = a_state(sra(SMW))[:-64]
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.read_snes9x_blocks(state)
        self.assertIn("truncated", str(caught.exception))


class ExtractionTests(unittest.TestCase):
    def test_the_save_is_the_prefix_of_the_SRA_block(self) -> None:
        extracted = savestate.extract_battery_save(
            a_state(sra(SMW)), expected_size=2048
        )
        self.assertEqual(extracted.data, SMW)
        self.assertEqual(extracted.core, "snes9x")
        self.assertEqual(extracted.version, (9, 0))

    def test_the_filler_after_the_save_is_never_returned(self) -> None:
        """The real block was 128 KB for a 2 KB save. Returning the buffer would
        be a confidently wrong answer shaped exactly like a save."""
        extracted = savestate.extract_battery_save(
            a_state(sra(SMW)), expected_size=2048
        )
        self.assertEqual(len(extracted.data), 2048)
        self.assertNotIn(FILLER, set(extracted.data[-16:]))

    def test_every_common_SNES_save_size_works(self) -> None:
        for size in (2048, 8192, 32768, 65536, 131072):
            with self.subTest(size=size):
                save = bytes((i * 7) % 256 for i in range(size))
                state = a_state(sra(save))
                got = savestate.extract_battery_save(state, expected_size=size)
                self.assertEqual(got.data, save)

    def test_the_length_is_reported_as_coming_from_the_record(self) -> None:
        """The CLI says so, because it is the one input the file did not supply."""
        extracted = savestate.extract_battery_save(
            a_state(sra(SMW)), expected_size=2048
        )
        self.assertTrue(extracted.size_from_record)

    def test_a_buffer_of_a_different_fixed_size_still_works(self) -> None:
        """snes9x's SRAM_SIZE is 0x20000 in Delta's build and 0x80000 today, so
        the block size must not be assumed."""
        state = a_state(sra(SMW, buffer_size=0x80000))
        got = savestate.extract_battery_save(state, expected_size=2048)
        self.assertEqual(got.data, SMW)


class RefusalTests(unittest.TestCase):
    def test_without_a_length_it_refuses_rather_than_returning_the_buffer(
        self,
    ) -> None:
        """The single most important behaviour here."""
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(a_state(sra(SMW)))
        message = str(caught.exception)
        self.assertIn("does not record how large", message)
        self.assertIn("Delta's record", message)

    def test_a_length_larger_than_the_block_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(
                a_state(sra(SMW, buffer_size=4096)), expected_size=65536
            )
        self.assertIn("does not fit", str(caught.exception))

    def test_a_zero_length_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(a_state(sra(SMW)), expected_size=0)
        self.assertIn("does not fit", str(caught.exception))

    def test_a_state_with_no_SRA_block_is_refused(self) -> None:
        state = HEADER + block(b"NAM", b"Removed\x00") + block(b"RAM", b"\x03" * 64)
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(state, expected_size=2048)
        self.assertIn("no SRA block", str(caught.exception))

    def test_an_unreadable_version_is_refused(self) -> None:
        state = b"#!s9xsnp:abcd\n" + sra(SMW)
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(state, expected_size=2048)
        self.assertIn("version", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
