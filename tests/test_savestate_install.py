"""Installing a recovered save into RetroArch, and backing up what it replaces.

Recovery used to end at a file in ``recovered/``, with the last step -- copying
it over RetroArch's own save -- left to Explorer. That step had no backup behind
it and no undo, and it lands on the file holding the progress being rescued.

So the two things this file is really about are both refusals and one round
trip:

- **The target is found, not guessed**, whenever RetroArch already has a save.
  Guessing means knowing which core RetroArch picked and whether it sorts saves
  into folders, and writing into the wrong one of two same-named files looks
  exactly like success while changing nothing.
- **A converted system is refused outright.** N64 keeps the cartridge save and
  four Controller Paks in one combined ``.srm``; a bare cartridge save written
  over it erases the paks. Unreachable today -- no N64 state holds a save to
  recover -- and asserted anyway, because the day it becomes reachable the
  damage is silent.
- **What was replaced comes back.** The backup goes through ``sync.backup``, so
  it lands in the same rolling folder as every other write and the existing
  Backups tab restores it with no new code at all. ``RoundTripTests`` proves
  that end to end rather than trusting the naming.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import (  # noqa: E402
    inspect as inspect_module,
    naming,
    restore,
    savestate,
    sync,
    systems,
)

SNES = systems.SYSTEMS["snes"]
N64 = systems.SYSTEMS["n64"]
GAME = "Super Mario World"
SAVE = bytes((i * 7) % 256 for i in range(2048))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.save_dir = self.root / "saves"
        self.save_dir.mkdir()
        self.backup_dir = self.root / "backups"

    def existing_save(self, data: bytes, *, core: str = "") -> Path:
        folder = self.save_dir / core if core else self.save_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / naming.save_filename(GAME, SNES.retroarch_save_ext)
        path.write_bytes(data)
        return path

    def plan(self, *, core: str = "Snes9x", sorted_by_core: bool = False):
        return savestate.plan_install(
            self.save_dir, GAME, SNES, core_name=core, sorted_by_core=sorted_by_core
        )


class PlanTests(Base):
    def test_an_existing_save_is_found_rather_than_reconstructed(self) -> None:
        target = self.existing_save(b"old" * 100, core="Snes9x")
        plan = self.plan(sorted_by_core=True)
        self.assertEqual(plan.target, target)
        self.assertTrue(plan.found)
        self.assertEqual(plan.existing_size, 300)
        self.assertTrue(plan.replaces_a_save)

    def test_a_save_is_found_even_where_the_sort_setting_says_it_should_not_be(
        self,
    ) -> None:
        """The setting can have been changed since the file was written. The
        file is the fact; the setting is a guess about it."""
        target = self.existing_save(b"old", core="Snes9x")
        plan = self.plan(sorted_by_core=False)
        self.assertEqual(plan.target, target)
        self.assertTrue(plan.found)

    def test_with_nothing_there_the_path_is_constructed_and_says_so(self) -> None:
        plan = self.plan(sorted_by_core=True)
        self.assertFalse(plan.found)
        self.assertIsNone(plan.existing_size)
        self.assertFalse(plan.replaces_a_save)
        self.assertEqual(plan.target.parent, self.save_dir / "Snes9x")

    def test_an_unsorted_setup_puts_it_loose_in_the_save_folder(self) -> None:
        plan = self.plan(sorted_by_core=False)
        self.assertEqual(plan.target.parent, self.save_dir)

    def test_the_constructed_path_is_the_one_the_sync_would_use(self) -> None:
        """The only two places in the program that work out where a save goes.
        They agree here, and this fails the moment either drifts."""
        entry = inspect_module.GameEntry(
            identifier="0" * 40,
            name=GAME,
            delta_type=SNES.delta_type,
            system=SNES,
            rom_path=None,
            save_path=None,
            extra_paths={},
        )
        for sorted_by_core in (False, True):
            with self.subTest(sorted_by_core=sorted_by_core):
                self.assertEqual(
                    self.plan(sorted_by_core=sorted_by_core).target,
                    sync.retroarch_save_path(
                        self.save_dir, entry, "Snes9x", sorted_by_core
                    ),
                )

    def test_a_missing_save_folder_is_not_an_error(self) -> None:
        """Nothing to search is the same answer as nothing found: construct it
        and let the write create the folder."""
        shutil.rmtree(self.save_dir)
        self.assertFalse(self.plan().found)


class AmbiguityTests(Base):
    """Two files with one name, and only one of them is read."""

    def test_two_candidates_are_refused_with_both_paths_named(self) -> None:
        loose = self.existing_save(b"a" * 10)
        sorted_one = self.existing_save(b"b" * 20, core="Snes9x")
        with self.assertRaises(savestate.InstallError) as caught:
            self.plan(sorted_by_core=True)
        message = str(caught.exception)
        self.assertIn(str(loose), message)
        self.assertIn(str(sorted_one), message)
        self.assertIn("only one of them is the file it loads", message)

    def test_neither_file_is_touched_when_it_refuses(self) -> None:
        self.existing_save(b"a" * 10)
        self.existing_save(b"b" * 20, core="Snes9x")
        with self.assertRaises(savestate.InstallError):
            self.plan(sorted_by_core=True)
        self.assertEqual(
            sorted(p.stat().st_size for p in self.save_dir.rglob("*") if p.is_file()),
            [10, 20],
        )


class RefusalTests(Base):
    def test_a_converted_system_is_refused_because_of_the_Controller_Paks(
        self,
    ) -> None:
        with self.assertRaises(savestate.InstallError) as caught:
            savestate.plan_install(self.save_dir, "Paper Mario", N64)
        message = str(caught.exception)
        self.assertIn("Controller Paks", message)
        self.assertIn("erase", message)

    def test_a_game_with_no_known_system_is_refused(self) -> None:
        with self.assertRaises(savestate.InstallError) as caught:
            savestate.plan_install(self.save_dir, GAME, None)
        self.assertIn("what system", str(caught.exception))

    def test_an_empty_save_is_refused_before_anything_is_written(self) -> None:
        target = self.existing_save(b"keep me")
        with self.assertRaises(savestate.InstallError) as caught:
            savestate.install_save(b"", self.plan(), self.backup_dir)
        self.assertIn("nothing to install", str(caught.exception))
        self.assertEqual(target.read_bytes(), b"keep me")

    def test_a_target_folder_that_cannot_be_made_leaves_everything_alone(
        self,
    ) -> None:
        """A file where the core's folder should be. The write must fail as a
        refusal, not as a traceback, and must not have started."""
        (self.save_dir / "Snes9x").write_bytes(b"not a folder")
        plan = self.plan(sorted_by_core=True)
        with self.assertRaises(savestate.InstallError) as caught:
            savestate.install_save(SAVE, plan, self.backup_dir)
        self.assertIn("cannot create", str(caught.exception))
        self.assertEqual((self.save_dir / "Snes9x").read_bytes(), b"not a folder")


class InstallTests(Base):
    def test_the_save_lands_byte_for_byte(self) -> None:
        target = self.existing_save(b"old" * 100)
        result = savestate.install_save(SAVE, self.plan(), self.backup_dir)
        self.assertEqual(target.read_bytes(), SAVE)
        self.assertEqual(result.target, target)
        self.assertEqual(result.size, len(SAVE))

    def test_what_it_replaced_is_backed_up_first(self) -> None:
        self.existing_save(b"old" * 100)
        result = savestate.install_save(SAVE, self.plan(), self.backup_dir)
        self.assertIsNotNone(result.backup)
        assert result.backup is not None
        self.assertEqual(result.backup.read_bytes(), b"old" * 100)
        self.assertIn("backed up as", result.describe())

    def test_installing_where_there_was_nothing_takes_no_backup(self) -> None:
        result = savestate.install_save(
            SAVE, self.plan(sorted_by_core=True), self.backup_dir
        )
        self.assertIsNone(result.backup)
        self.assertIn("nothing was there", result.describe())
        self.assertEqual(result.target.read_bytes(), SAVE)

    def test_the_core_folder_is_created_when_it_has_to_be(self) -> None:
        plan = self.plan(sorted_by_core=True)
        self.assertFalse(plan.target.parent.exists())
        savestate.install_save(SAVE, plan, self.backup_dir)
        self.assertTrue(plan.target.is_file())

    def test_no_partial_file_is_left_behind(self) -> None:
        """It is written beside the target and moved into place, so a crash
        cannot leave a truncated save. The staging file must not survive."""
        self.existing_save(b"old")
        savestate.install_save(SAVE, self.plan(), self.backup_dir)
        self.assertEqual(list(self.save_dir.rglob("*.partial")), [])

    def test_a_size_that_does_not_match_is_reported_not_refused(self) -> None:
        """A core that pads its .srm is legitimate; a name that collided with
        another game is not. Only the person can tell, and there is a backup."""
        self.existing_save(b"x" * 8192)
        plan = self.plan()
        self.assertFalse(plan.size_matches(len(SAVE)))
        savestate.install_save(SAVE, plan, self.backup_dir)
        self.assertEqual(plan.target.read_bytes(), SAVE)

    def test_a_matching_size_is_the_ordinary_case(self) -> None:
        self.existing_save(b"x" * len(SAVE))
        self.assertTrue(self.plan().size_matches(len(SAVE)))

    def test_a_constructed_target_has_no_size_to_disagree_with(self) -> None:
        self.assertTrue(self.plan().size_matches(999))


class RoundTripTests(Base):
    """Install, change your mind, put it back -- through the shipped Backups
    tab, with nothing written for this feature."""

    def test_the_replaced_save_is_restorable_from_the_backups_list(self) -> None:
        target = self.existing_save(b"the original save" * 10)
        original = target.read_bytes()

        savestate.install_save(SAVE, self.plan(), self.backup_dir)
        self.assertEqual(target.read_bytes(), SAVE)

        points = restore.restore_points(restore.scan(self.backup_dir), {})
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.backup.side, restore.RETROARCH)
        self.assertEqual(point.backup.kind, "save")
        self.assertEqual(point.label, GAME)

        restore.restore_retroarch(point, self.save_dir, self.backup_dir)
        self.assertEqual(target.read_bytes(), original)

    def test_the_restore_is_itself_undoable(self) -> None:
        """Restoring backs up what it replaces, so the installed save is still
        reachable after changing your mind twice."""
        self.existing_save(b"original")
        savestate.install_save(SAVE, self.plan(), self.backup_dir)
        first = restore.restore_points(restore.scan(self.backup_dir), {})[0]
        restore.restore_retroarch(first, self.save_dir, self.backup_dir)

        points = restore.restore_points(restore.scan(self.backup_dir), {})
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].backup.path.read_bytes(), SAVE)


if __name__ == "__main__":
    unittest.main()
