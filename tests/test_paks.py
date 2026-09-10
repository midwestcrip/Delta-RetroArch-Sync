"""Controller Pak files on disk, and RetroArch's combined save.

This is the half of the Controller Pak feature that needs no cable, no
``pymobiledevice3`` and no phone: given a folder of ``.mpk`` files -- filled by
the add-on, or dragged off in Explorer by hand, it makes no difference -- put
them into RetroArch's combined save and take them back out.

Two rules carry the file:

- **A blank pak never overwrites one with data.** A phone with an empty
  Controller Pak 2 and a desktop with a season of Mario Kart 64 ghosts in slot 2
  is the ordinary case. A straight copy erases them, so ``plan_install`` sorts
  each slot into write / keep / skip and says which.
- **Only the pak regions are touched.** ``n64.to_retroarch`` writes the
  cartridge save and refuses to touch the paks; this writes the paks and must
  leave the cartridge save, the EEPROM and the FlashRAM exactly as they were.
  ``RegionOwnershipTests`` is the one that would catch the two halves eating
  each other.

Shapes are told apart by **size**, never by name -- mupen64plus builds disagree
about whether the four paks live in one file or four, and about what to call
them, but a pak is 32,768 bytes on every one of them.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import n64, paks, restore  # noqa: E402


def a_pak(marker: int) -> bytes:
    """A formatted pak with one page allocated, so it counts as used.

    The index table lives at 256, and an entry reading ``0x0003`` means that
    page is free -- which is what ``controller_pak_is_empty`` checks, entry by
    entry from offset 10. Anything else is a page in use, so writing one
    non-free entry is the smallest honest way to say "this pack has notes on
    it". ``marker`` just keeps four paks distinguishable.
    """
    data = bytearray(n64.format_controller_pak())
    data[256 + 10] = 0x00
    data[256 + 11] = 0x04 + marker
    return bytes(data)


class ReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_one_file_holding_all_four_is_split_by_size(self) -> None:
        bundle = b"".join(a_pak(i + 1) for i in range(4))
        (self.folder / "PaperMario.mpk").write_bytes(bundle)

        found = paks.read_folder(self.folder)
        self.assertEqual(found.slots, (0, 1, 2, 3))
        self.assertEqual(found.paks[2].data, a_pak(3))

    def test_four_separate_files_are_read_by_their_numbers(self) -> None:
        for index in range(1, 5):
            (self.folder / f"PaperMario.mpk{index}").write_bytes(a_pak(index))
        found = paks.read_folder(self.folder)
        self.assertEqual(found.slots, (0, 1, 2, 3))
        self.assertEqual(found.paks[0].data, a_pak(1))

    def test_the_slot_number_is_read_where_it_cannot_be_part_of_a_title(
        self,
    ) -> None:
        """Two places a digit is certainly a slot: the extension, and a stem
        that is nothing but a pak word and a number."""
        for name, slot in (
            ("mempak3.mpk", 2),
            ("MemPak 2.mpk", 1),
            ("controller-pak4.mpk", 3),
            ("pak1.mpk", 0),
            ("whatever.mpk4", 3),
        ):
            with self.subTest(name=name):
                folder = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, folder, True)
                (folder / name).write_bytes(a_pak(1))
                self.assertEqual(paks.read_folder(folder).slots, (slot,))

    def test_a_digit_in_the_game_title_is_not_a_slot_number(self) -> None:
        """The bug this replaced, and it was not hypothetical.

        mupen64plus names its files after the ROM, so ``Mario Kart 64.mpk`` was
        read as Controller Pak **4** and ``Doom 64.mpk`` the same -- both games
        whose *only* storage is the Controller Pak, so data in the wrong
        controller is the entire save gone. Every name below is a real N64
        title that the old trailing-digit rule mis-slotted.
        """
        for name in (
            "Mario Kart 64.mpk",
            "Doom 64.mpk",
            "Turok 2.mpk",
            "Extreme-G XG2.mpk",
            "Top Gear Rally 2.mpk",
        ):
            with self.subTest(name=name):
                self.assertIsNone(paks._slot_from_name(Path(name)))

    def test_the_extension_still_decides_for_a_title_full_of_digits(
        self,
    ) -> None:
        """``.mpk3`` is unambiguous however the game is called, so the fix must
        not have thrown the working case away with the broken one."""
        self.assertEqual(paks._slot_from_name(Path("Mario Kart 64.mpk3")), 2)

    def test_one_file_named_after_its_game_is_still_read(self) -> None:
        """Refusing to guess the slot must not become refusing the file. A
        single pak in a folder is unambiguous whatever it is called."""
        (self.folder / "Mario Kart 64.mpk").write_bytes(a_pak(1))
        self.assertEqual(paks.read_folder(self.folder).slots, (0,))

    def test_several_files_named_after_their_games_are_refused(self) -> None:
        """And when it is genuinely ambiguous, it asks rather than scattering
        them across controllers by whatever digits the titles happen to end
        in."""
        (self.folder / "Mario Kart 64.mpk").write_bytes(a_pak(1))
        (self.folder / "Doom 64.mpk").write_bytes(a_pak(2))
        with self.assertRaises(paks.PakError) as caught:
            paks.read_folder(self.folder)
        self.assertIn("does not say which controller", str(caught.exception))

    def test_a_lone_unnumbered_pak_is_taken_as_controller_one(self) -> None:
        """One file in a folder is unambiguous whatever it is called."""
        (self.folder / "mempak.mpk").write_bytes(a_pak(1))
        self.assertEqual(paks.read_folder(self.folder).slots, (0,))

    def test_several_unnumbered_paks_are_refused_rather_than_ordered(
        self,
    ) -> None:
        """Guessing the order here puts someone's Mario Kart ghosts into the
        wrong controller, which looks like data loss and is."""
        (self.folder / "one.mpk").write_bytes(a_pak(1))
        (self.folder / "two.mpk").write_bytes(a_pak(2))
        with self.assertRaises(paks.PakError) as caught:
            paks.read_folder(self.folder)
        self.assertIn("does not say which controller", str(caught.exception))

    def test_two_files_claiming_the_same_slot_are_refused(self) -> None:
        """Both of these name slot 1 unambiguously -- one in its extension, one
        as its whole stem -- so there is a genuine conflict to refuse."""
        (self.folder / "a.mpk1").write_bytes(a_pak(1))
        (self.folder / "mempak1.mpk").write_bytes(a_pak(2))
        with self.assertRaises(paks.PakError) as caught:
            paks.read_folder(self.folder)
        self.assertIn("both claim Controller Pak 1", str(caught.exception))

    def test_a_file_of_the_wrong_size_is_refused_with_its_size(self) -> None:
        (self.folder / "truncated.mpk").write_bytes(b"\x00" * 1234)
        with self.assertRaises(paks.PakError) as caught:
            paks.read_folder(self.folder)
        message = str(caught.exception)
        self.assertIn("1,234 bytes", message)
        self.assertIn("Refusing to guess", message)

    def test_an_empty_folder_says_what_it_was_looking_for(self) -> None:
        with self.assertRaises(paks.PakError) as caught:
            paks.read_folder(self.folder)
        self.assertIn(".mpk1 to .mpk4", str(caught.exception))

    def test_files_that_are_not_paks_are_ignored(self) -> None:
        (self.folder / "PaperMario.mpk1").write_bytes(a_pak(1))
        (self.folder / "PaperMario.eep").write_bytes(b"\x00" * 2048)
        (self.folder / "notes.txt").write_text("hello")
        self.assertEqual(paks.read_folder(self.folder).slots, (0,))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.target = self.folder / "Paper Mario.srm"

    def a_set(self, *specs: tuple[int, bytes]) -> paks.PakSet:
        return paks.PakSet(
            tuple(
                paks.Pak(slot=slot, data=data, source=f"pak{slot + 1}.mpk")
                for slot, data in specs
            )
        )

    def test_a_pak_with_notes_is_written(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        plan = paks.plan_install(self.a_set((0, a_pak(1))), self.target)
        self.assertEqual(plan.writing[0].slot, 0)
        self.assertTrue(plan.changes_anything)

    def test_a_blank_pak_never_overwrites_one_with_data(self) -> None:
        """The rule the whole module is built around."""
        srm = n64.with_controller_paks(n64.blank_srm(), {1: a_pak(9)})
        self.target.write_bytes(srm)

        plan = paks.plan_install(
            self.a_set((1, n64.format_controller_pak())), self.target
        )
        self.assertEqual(plan.writing, ())
        self.assertEqual(plan.protecting[0].slot, 1)
        self.assertIn("the phone's is blank", plan.describe())

    def test_blank_on_both_sides_is_a_quiet_skip(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        plan = paks.plan_install(
            self.a_set((0, n64.format_controller_pak())), self.target
        )
        self.assertEqual(plan.already_blank[0].slot, 0)
        self.assertIn("blank on both sides", plan.describe())

    def test_a_missing_save_is_created_from_a_blank_one(self) -> None:
        plan = paks.plan_install(self.a_set((0, a_pak(1))), self.target)
        self.assertTrue(plan.creating)
        self.assertIn("Creating", plan.describe())

    def test_a_save_of_the_wrong_size_is_refused(self) -> None:
        self.target.write_bytes(b"\x00" * 4096)
        with self.assertRaises(paks.PakError) as caught:
            paks.plan_install(self.a_set((0, a_pak(1))), self.target)
        self.assertIn("not a Mupen64Plus-Next save", str(caught.exception))

    def test_nothing_to_install_is_refused(self) -> None:
        with self.assertRaises(paks.PakError):
            paks.plan_install(paks.PakSet(()), self.target)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.target = self.root / "saves" / "Paper Mario.srm"
        self.target.parent.mkdir()
        self.backup_dir = self.root / "backups"

    def plan_for(self, *specs: tuple[int, bytes]) -> paks.PakInstallPlan:
        pakset = paks.PakSet(
            tuple(
                paks.Pak(slot=slot, data=data, source=f"pak{slot + 1}.mpk")
                for slot, data in specs
            )
        )
        return paks.plan_install(pakset, self.target)

    def test_the_pak_lands_in_its_own_slot(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        paks.install(self.plan_for((2, a_pak(7))), self.backup_dir)
        self.assertEqual(
            n64.controller_paks(self.target.read_bytes())[2], a_pak(7)
        )

    def test_the_previous_save_is_backed_up_and_restorable(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        original = self.target.read_bytes()

        paks.install(self.plan_for((0, a_pak(1))), self.backup_dir)
        self.assertNotEqual(self.target.read_bytes(), original)

        points = restore.restore_points(restore.scan(self.backup_dir), {})
        self.assertEqual(len(points), 1)
        restore.restore_retroarch(points[0], self.target.parent, self.backup_dir)
        self.assertEqual(self.target.read_bytes(), original)

    def test_installing_where_there_was_no_save_creates_a_valid_one(self) -> None:
        paks.install(self.plan_for((0, a_pak(1))), self.backup_dir)
        written = self.target.read_bytes()
        self.assertEqual(len(written), n64.SRM_SIZE)
        self.assertEqual(n64.controller_paks(written)[0], a_pak(1))

    def test_a_plan_that_writes_nothing_is_refused_not_silently_done(
        self,
    ) -> None:
        self.target.write_bytes(n64.blank_srm())
        plan = self.plan_for((0, n64.format_controller_pak()))
        with self.assertRaises(paks.PakError) as caught:
            paks.install(plan, self.backup_dir)
        self.assertIn("Nothing was changed", str(caught.exception))

    def test_no_partial_file_survives(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        paks.install(self.plan_for((0, a_pak(1))), self.backup_dir)
        self.assertEqual(list(self.target.parent.glob("*.partial")), [])


class RegionOwnershipTests(unittest.TestCase):
    """The property that keeps the two halves from eating each other.

    ``n64.to_retroarch`` owns the cartridge save and must not touch the paks.
    This module owns the paks and must not touch anything else. Between them
    every byte of the 296,960 has exactly one owner.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.target = self.root / "Paper Mario.srm"

    def test_installing_paks_leaves_the_cartridge_save_untouched(self) -> None:
        cartridge = bytes((i * 31) % 256 for i in range(0x20000))
        srm = n64.to_retroarch(cartridge, n64.blank_srm())
        self.target.write_bytes(srm)

        plan = paks.plan_install(
            paks.PakSet((paks.Pak(0, a_pak(5), "one.mpk"),)), self.target
        )
        paks.install(plan, self.root / "backups")

        after = self.target.read_bytes()
        self.assertEqual(after[n64.FLASHRAM.start : n64.FLASHRAM.end], cartridge)
        self.assertEqual(after[: n64.PAKS_START], srm[: n64.PAKS_START])
        self.assertEqual(after[n64.PAKS_END :], srm[n64.PAKS_END :])

    def test_writing_a_cartridge_save_leaves_the_paks_untouched(self) -> None:
        """The existing rule, restated from this side so both directions are
        asserted in one place."""
        with_paks = n64.with_controller_paks(n64.blank_srm(), {3: a_pak(6)})
        after = n64.to_retroarch(bytes(2048), with_paks)
        self.assertEqual(n64.controller_paks(after)[3], a_pak(6))

    def test_a_save_of_the_wrong_size_cannot_be_written_into(self) -> None:
        with self.assertRaises(ValueError) as caught:
            n64.with_controller_paks(b"\x00" * 100, {0: a_pak(1)})
        self.assertIn("296960 bytes", str(caught.exception).replace(",", ""))

    def test_a_pak_of_the_wrong_size_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            n64.with_controller_paks(n64.blank_srm(), {0: b"\x00" * 10})
        self.assertIn("Controller Pak 1 is 10 bytes", str(caught.exception))

    def test_there_is_no_fifth_controller(self) -> None:
        with self.assertRaises(ValueError) as caught:
            n64.with_controller_paks(n64.blank_srm(), {4: a_pak(1)})
        self.assertIn("no Controller Pak 5", str(caught.exception))


class ExportTests(unittest.TestCase):
    """The way back: four files to put on the phone."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.srm = self.root / "Paper Mario.srm"

    def test_four_files_come_out_one_per_controller(self) -> None:
        srm = n64.with_controller_paks(
            n64.blank_srm(), {index: a_pak(index + 1) for index in range(4)}
        )
        self.srm.write_bytes(srm)

        written = paks.export(self.srm, self.root / "out")
        self.assertEqual(len(written), 4)
        self.assertEqual(written[0].read_bytes(), a_pak(1))
        self.assertEqual(written[3].read_bytes(), a_pak(4))

    def test_what_comes_out_reads_back_in(self) -> None:
        """The round trip, which is the only reason export exists."""
        original = {index: a_pak(index + 1) for index in range(4)}
        self.srm.write_bytes(n64.with_controller_paks(n64.blank_srm(), original))

        out = self.root / "out"
        paks.export(self.srm, out)
        read_back = paks.read_folder(out)

        self.assertEqual(read_back.slots, (0, 1, 2, 3))
        self.assertEqual(read_back.as_mapping(), original)

    def test_a_save_of_the_wrong_size_cannot_be_exported(self) -> None:
        self.srm.write_bytes(b"\x00" * 64)
        with self.assertRaises(paks.PakError):
            paks.export(self.srm, self.root / "out")


if __name__ == "__main__":
    unittest.main()
