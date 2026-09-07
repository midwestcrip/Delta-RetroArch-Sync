"""Tests for the N64 save conversion.

The sizes here are the four real ones captured from Delta on 2026-09-07: 512 B
(Super Mario 64), 2,048 B (Yoshi's Story, Donkey Kong 64), 32,768 B (Ocarina of
Time) and 131,072 B (Majora's Mask, Paper Mario). Every cartridge storage type
N64 has is represented, which is what made the conversion writable at all.

The offsets are from ra_mp64_srm_convert. They are asserted here as literals
rather than computed from the module, so a change to the layout has to be a
deliberate edit to a stated number and not a silent shift.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import n64  # noqa: E402

EEPROM_4K = 0x200
EEPROM_16K = 0x800
SRAM = 0x8000
FLASHRAM = 0x20000


class LayoutTests(unittest.TestCase):
    def test_the_combined_save_is_the_size_mupen64plus_next_writes(self) -> None:
        self.assertEqual(n64.SRM_SIZE, 0x48800)
        self.assertEqual(n64.SRM_SIZE, 296_960)

    def test_the_regions_sit_where_the_reference_puts_them(self) -> None:
        self.assertEqual((n64.EEPROM.start, n64.EEPROM.end), (0x00000, 0x00800))
        self.assertEqual((n64.PAKS_START, n64.PAKS_END), (0x00800, 0x20800))
        self.assertEqual((n64.SRAM.start, n64.SRAM.end), (0x20800, 0x28800))
        self.assertEqual((n64.FLASHRAM.start, n64.FLASHRAM.end), (0x28800, 0x48800))

    def test_the_regions_tile_the_whole_file_without_gaps(self) -> None:
        """A gap or an overlap here is corruption that would look like a working
        conversion, so it is worth checking arithmetically rather than by eye."""
        self.assertEqual(n64.EEPROM.end, n64.PAKS_START)
        self.assertEqual(n64.PAKS_START + n64.PAK_COUNT * n64.PAK_SIZE, n64.PAKS_END)
        self.assertEqual(n64.PAKS_END, n64.SRAM.start)
        self.assertEqual(n64.SRAM.end, n64.FLASHRAM.start)
        self.assertEqual(n64.FLASHRAM.end, n64.SRM_SIZE)

    def test_each_real_save_size_maps_to_a_storage(self) -> None:
        self.assertIs(n64.region_for(EEPROM_4K), n64.EEPROM)
        self.assertIs(n64.region_for(EEPROM_16K), n64.EEPROM)
        self.assertIs(n64.region_for(SRAM), n64.SRAM)
        self.assertIs(n64.region_for(FLASHRAM), n64.FLASHRAM)

    def test_an_unrecognised_size_is_refused_with_the_sizes_it_wanted(self) -> None:
        with self.assertRaises(n64.ConversionError) as caught:
            n64.region_for(1234)
        message = str(caught.exception)
        self.assertIn("1,234", message)
        self.assertIn("512", message)
        self.assertIn("131,072", message)


class ControllerPakTests(unittest.TestCase):
    def test_a_freshly_formatted_pak_reads_as_empty(self) -> None:
        """The reference's own invariant, used as a check on this implementation.

        `init` and `is_empty` were transcribed from Rust separately; if the index
        table were written even slightly wrong, this would fail.
        """
        self.assertTrue(n64.controller_pak_is_empty(n64.format_controller_pak()))

    def test_a_formatted_pak_is_not_just_padding(self) -> None:
        """Filling the region with 0xFF instead would make the game ask the
        player to format the pack before it would save."""
        pak = n64.format_controller_pak()
        self.assertEqual(pak[0], 0x81)
        self.assertNotEqual(set(pak), {n64.EMPTY})

    def test_a_used_pak_is_detected(self) -> None:
        pak = bytearray(n64.format_controller_pak())
        # Allocate one entry in the index table: no longer free space.
        pak[256 + 10 : 256 + 12] = (5).to_bytes(2, "big")
        self.assertFalse(n64.controller_pak_is_empty(bytes(pak)))

    def test_a_blank_combined_save_has_four_formatted_empty_paks(self) -> None:
        srm = n64.blank_srm()
        self.assertEqual(len(srm), n64.SRM_SIZE)
        paks = n64.controller_paks(srm)
        self.assertEqual(len(paks), 4)
        for pak in paks:
            self.assertTrue(n64.controller_pak_is_empty(pak))
            self.assertEqual(pak[0], 0x81)
        self.assertFalse(n64.has_controller_pak_data(srm))


class RoundTripTests(unittest.TestCase):
    def test_every_storage_type_survives_out_and_back(self) -> None:
        for size in (EEPROM_4K, EEPROM_16K, SRAM, FLASHRAM):
            with self.subTest(size=size):
                save = bytes((i * 7 + 1) % 251 for i in range(size))
                srm = n64.to_retroarch(save)
                self.assertEqual(len(srm), n64.SRM_SIZE)
                self.assertEqual(n64.to_delta(srm, size), save)

    def test_a_save_lands_at_its_own_offset(self) -> None:
        save = b"\x11" * SRAM
        srm = n64.to_retroarch(save)
        self.assertEqual(srm[n64.SRAM.start : n64.SRAM.end], save)
        # And nowhere else.
        self.assertEqual(srm[n64.EEPROM.start : n64.EEPROM.end], bytes([n64.EMPTY]) * 0x800)
        self.assertEqual(
            srm[n64.FLASHRAM.start : n64.FLASHRAM.end], bytes([n64.EMPTY]) * 0x20000
        )

    def test_a_4k_eeprom_save_leaves_the_rest_of_its_region_empty(self) -> None:
        """How the reference tells 4 Kbit from 16 Kbit: everything past 0x200
        reads as 0xFF."""
        srm = n64.to_retroarch(b"\xab" * EEPROM_4K)
        self.assertEqual(srm[:EEPROM_4K], b"\xab" * EEPROM_4K)
        self.assertEqual(srm[EEPROM_4K:EEPROM_16K], bytes([n64.EMPTY]) * 0x600)


class PreservationTests(unittest.TestCase):
    """The rule the whole module exists for: never touch what is not ours."""

    def test_controller_pak_data_survives_a_pull(self) -> None:
        srm = bytearray(n64.blank_srm())
        marker = b"GHOSTDATA" + b"\x5a" * 100
        srm[n64.PAKS_START + 1000 : n64.PAKS_START + 1000 + len(marker)] = marker
        # Mark the pack as used, so it is genuinely "data" and not padding.
        srm[256 + n64.PAKS_START + 10 : 256 + n64.PAKS_START + 12] = (5).to_bytes(2, "big")
        before = bytes(srm[n64.PAKS_START : n64.PAKS_END])

        merged = n64.to_retroarch(b"\x01" * SRAM, bytes(srm))

        self.assertEqual(merged[n64.PAKS_START : n64.PAKS_END], before)
        self.assertIn(marker, merged)

    def test_the_other_cartridge_regions_survive_too(self) -> None:
        """A game does not have two storages, but the file has room for all of
        them, and clobbering a region we are not writing would still be wrong."""
        srm = bytearray(n64.blank_srm())
        srm[n64.FLASHRAM.start : n64.FLASHRAM.start + 4] = b"KEEP"

        merged = n64.to_retroarch(b"\x02" * EEPROM_16K, bytes(srm))

        self.assertEqual(merged[n64.FLASHRAM.start : n64.FLASHRAM.start + 4], b"KEEP")

    def test_a_smaller_save_does_not_leave_the_previous_one_trailing(self) -> None:
        """A 16 Kbit save replaced by a 4 Kbit one must not leave the tail of the
        old save behind, or the region would read as 16 Kbit and the game would
        load somebody else's data."""
        srm = n64.to_retroarch(b"\xcc" * EEPROM_16K)
        srm = n64.to_retroarch(b"\xdd" * EEPROM_4K, srm)

        self.assertEqual(srm[:EEPROM_4K], b"\xdd" * EEPROM_4K)
        self.assertEqual(srm[EEPROM_4K:EEPROM_16K], bytes([n64.EMPTY]) * 0x600)


class RefusalTests(unittest.TestCase):
    def test_a_combined_save_of_the_wrong_size_is_refused(self) -> None:
        """Rather than edited at offsets that may mean nothing in that file."""
        with self.assertRaises(n64.ConversionError):
            n64.to_retroarch(b"\x00" * SRAM, b"\xff" * 1024)
        with self.assertRaises(n64.ConversionError):
            n64.to_delta(b"\xff" * 1024, SRAM)

    def test_an_unrecognised_delta_save_is_refused(self) -> None:
        with self.assertRaises(n64.ConversionError):
            n64.to_retroarch(b"\x00" * 999)


if __name__ == "__main__":
    unittest.main()
