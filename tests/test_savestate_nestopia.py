"""Lifting a NES battery save out of a nestopia save state.

Read from nestopia's ``NstState.cpp`` and ``board/NstBoard.cpp``, then measured
against a real Kirby's Adventure state synced from Delta on 2026-09-08: 13,165
bytes, ``WRM`` chunk of 8,193, flag byte 0, and 8,192 bytes matching Delta's own
save exactly.

Three things about this format are easy to get wrong, and each has tests below.

**``NST\\x1a`` is a chunk id, not a magic followed by a version.**
``Machine::SaveState`` opens with ``saver.Begin( AsciiId<'N','S','T'>::V | 0x1A
<< 24 )``, so the whole file is one outer chunk and the four bytes after the id
are its payload length. Reading them as a version was the first mistake here;
they are a length, and checking it against the file size is free.

**Chunks nest, and nothing marks a container.** nestopia's loader knows by
context. Containers are found here by parsing a payload as a chunk sequence and
requiring it to tile the payload exactly.

**``Compress`` writes a flag byte before the data**, 0 stored or 1 zlib. Delta's
build wrote 0 on every measured file, so the compressed branch is exercised only
by these fixtures -- which is exactly why they exist.
"""

from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

KIRBY = bytes((i * 13) % 256 for i in range(8192))


def chunk(ident: bytes, payload: bytes) -> bytes:
    """``Saver::Begin`` writes the id and a length; ``End`` fills the length in."""
    assert len(ident) == 4
    return ident + len(payload).to_bytes(4, "little") + payload


def stored(data: bytes) -> bytes:
    """``Compress`` with ``NO_COMPRESSION``."""
    return bytes([savestate.NESTOPIA_STORED]) + data


def compressed(data: bytes) -> bytes:
    """``Compress`` with ``ZLIB_COMPRESSION``.

    nestopia's ``Zlib::Compress`` calls ``compress2``, so this is zlib-wrapped
    rather than raw deflate.
    """
    return bytes([savestate.NESTOPIA_ZLIB]) + zlib.compress(data, 9)


def a_state(wrm_payload: bytes, *, nest: bool = True) -> bytes:
    """A state shaped like the real one: NFO, CPU, PPU, then IMG > MPR > WRM."""
    inner = chunk(b"WRM\x00", wrm_payload) + chunk(b"PRG\x00", b"\x01" * 16)
    body = (
        chunk(b"NFO\x00", b"\x00" * 8)
        + chunk(b"CPU\x00", chunk(b"REG\x00", b"\x02" * 7))
        + chunk(b"PPU\x00", chunk(b"PAL\x00", b"\x03" * 33))
    )
    if nest:
        body += chunk(b"IMG\x00", chunk(b"MPR\x00", inner))
    else:
        body += inner
    return savestate.NESTOPIA_MAGIC + len(body).to_bytes(4, "little") + body


class FramingTests(unittest.TestCase):
    def test_the_outer_length_is_checked_against_the_file(self) -> None:
        """Those four bytes are a length, not a version. Truncation shows up
        here before anything else is parsed."""
        state = bytearray(a_state(stored(KIRBY)))
        state[4:8] = (len(state) * 2).to_bytes(4, "little")
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(bytes(state), expected_size=8192)
        self.assertIn("truncated or damaged", str(caught.exception))

    def test_a_chunk_sequence_must_tile_its_buffer_exactly(self) -> None:
        """The test for "is this a container?". Anything looser would descend
        into arbitrary payload bytes and find chunks that are not there."""
        good = chunk(b"AAA\x00", b"\x01" * 4) + chunk(b"BBB\x00", b"\x02" * 4)
        self.assertIsNotNone(savestate._nestopia_chunks(good))
        self.assertIsNone(savestate._nestopia_chunks(good + b"\x00"))

    def test_the_save_is_found_however_deeply_it_is_nested(self) -> None:
        for nest in (True, False):
            with self.subTest(nested=nest):
                got = savestate.extract_battery_save(
                    a_state(stored(KIRBY), nest=nest), expected_size=8192
                )
                self.assertEqual(got.data, KIRBY)

    def test_nestopia_states_report_no_version(self) -> None:
        """There is no version field in the format. Reporting one would be
        inventing it -- this said 13157 until the header was read properly."""
        got = savestate.extract_battery_save(
            a_state(stored(KIRBY)), expected_size=8192
        )
        self.assertIsNone(got.version)
        self.assertEqual(got.core, "nestopia")


class CompressionTests(unittest.TestCase):
    def test_a_stored_chunk_is_read_directly(self) -> None:
        got = savestate.extract_battery_save(
            a_state(stored(KIRBY)), expected_size=8192
        )
        self.assertEqual(got.data, KIRBY)

    def test_a_zlib_chunk_is_decompressed(self) -> None:
        """The branch Delta's build never took. It is live whenever nestopia is
        built with zlib, so a state from anywhere else could arrive this way."""
        state = a_state(compressed(KIRBY))
        self.assertLess(len(state), len(KIRBY))  # it really is compressed
        got = savestate.extract_battery_save(state, expected_size=8192)
        self.assertEqual(got.data, KIRBY)

    def test_a_corrupt_zlib_chunk_is_refused_not_guessed_at(self) -> None:
        broken = bytes([savestate.NESTOPIA_ZLIB]) + b"\x78\x9c" + b"\x00" * 64
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(a_state(broken), expected_size=8192)
        self.assertIn("will not decompress", str(caught.exception))

    def test_an_unknown_compression_flag_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(
                a_state(b"\x07" + KIRBY), expected_size=8192
            )
        self.assertIn("neither stored (0) nor zlib (1)", str(caught.exception))


class ExtractionTests(unittest.TestCase):
    def test_the_save_is_the_prefix_of_the_work_RAM(self) -> None:
        """WRM holds GetWram(); the battery-backed part is GetSavableWram(),
        which nestopia enumerates first and so is the prefix."""
        wram = KIRBY + b"\xEE" * 8192
        got = savestate.extract_battery_save(
            a_state(stored(wram)), expected_size=8192
        )
        self.assertEqual(got.data, KIRBY)

    def test_common_NES_save_sizes_work(self) -> None:
        for size in (2048, 8192, 32768):
            with self.subTest(size=size):
                save = bytes((i * 5) % 256 for i in range(size))
                got = savestate.extract_battery_save(
                    a_state(stored(save)), expected_size=size
                )
                self.assertEqual(got.data, save)


class RefusalTests(unittest.TestCase):
    def test_without_a_length_it_refuses(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(a_state(stored(KIRBY)))
        self.assertIn("does not record how much", str(caught.exception))

    def test_a_length_larger_than_the_chunk_is_refused(self) -> None:
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(
                a_state(stored(KIRBY)), expected_size=65536
            )
        self.assertIn("Refusing", str(caught.exception))

    def test_a_cartridge_with_no_work_RAM_says_so(self) -> None:
        """No WRM chunk means the board has no work RAM, so there is no battery
        save in there -- a different thing from a damaged state."""
        body = chunk(b"NFO\x00", b"\x00" * 8) + chunk(b"CPU\x00", b"\x01" * 16)
        state = savestate.NESTOPIA_MAGIC + len(body).to_bytes(4, "little") + body
        with self.assertRaises(savestate.SaveStateError) as caught:
            savestate.extract_battery_save(state, expected_size=8192)
        self.assertIn("no battery save to recover", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
