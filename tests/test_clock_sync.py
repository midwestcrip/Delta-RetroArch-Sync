"""Where the clock is allowed to move, and where it must not.

The conversion itself is pinned in test_clock.py. What is tested here is the
part that can lose something: which core we are willing to write a clock file
for, and the rule that the clock only ever travels with the save it belongs to.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import clock, inspect as inspect_module  # noqa: E402
from delta_retroarch_synchronizer import sync, systems  # noqa: E402

REAL_DELTA_CLOCK = bytes.fromhex("6a9dcb79")
REAL_GAMBATTE_CLOCK = bytes.fromhex("a60d9e6a00000000")


class ClockSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.delta_folder = root / "delta"
        self.delta_folder.mkdir()
        self.save_dir = root / "saves"
        self.save_dir.mkdir()
        self.backups = root / "backups"

        self.identifier = "abc123"
        self.delta_save = self.delta_folder / f"GameSave-{self.identifier}-gameSave"
        self.delta_save.write_bytes(b"\x00" * 32768)
        self.delta_clock = (
            self.delta_folder / f"GameSave-{self.identifier}-gameTimeSave"
        )
        self.delta_clock.write_bytes(REAL_DELTA_CLOCK)

        self.retro_save = self.save_dir / "Crystal.srm"
        self.retro_clock = self.save_dir / "Crystal.rtc"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def entry(self, system_key: str = "gbc") -> inspect_module.GameEntry:
        return inspect_module.GameEntry(
            identifier=self.identifier,
            name="Pokemon Crystal",
            delta_type=systems.SYSTEMS[system_key].delta_type,
            system=systems.SYSTEMS[system_key],
            rom_path=None,
            save_path=self.delta_save,
            extra_paths={},
        )

    def pull(self, core: str = "Gambatte", **kwargs):
        return sync.pull_clock(
            self.entry(), self.retro_save, core, self.backups, **kwargs
        )

    # -- the direction that only touches RetroArch -------------------------

    def test_pulling_writes_gambattes_format(self) -> None:
        outcome = self.pull()

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertTrue(outcome.applied)
        self.assertEqual(
            self.retro_clock.read_bytes(), clock.to_retroarch(REAL_DELTA_CLOCK)
        )

    def test_pulling_an_unchanged_clock_does_nothing(self) -> None:
        """A no-op must report nothing, not a write that changed no bytes."""
        self.retro_clock.write_bytes(clock.to_retroarch(REAL_DELTA_CLOCK))

        self.assertIsNone(self.pull())

    def test_pulling_backs_up_the_clock_it_replaces(self) -> None:
        self.retro_clock.write_bytes(REAL_GAMBATTE_CLOCK)

        self.pull()

        self.assertTrue(list(self.backups.glob("Crystal.rtc.*.bak")))

    def test_a_dry_run_writes_nothing(self) -> None:
        outcome = self.pull(dry_run=True)

        self.assertIsNotNone(outcome)
        self.assertFalse(self.retro_clock.exists())

    # -- the gate that stops a clock being written in the wrong format -----

    def test_an_unverified_core_is_refused_and_says_why(self) -> None:
        """mGBA's .rtc is a 48-byte struct; eight bytes over it is corruption."""
        outcome = self.pull(core="mGBA")

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertIs(outcome.action, sync.Action.SKIPPED)
        self.assertIn("mGBA", outcome.detail)
        self.assertIn("Gambatte", outcome.detail)
        self.assertFalse(self.retro_clock.exists())

    def test_the_refusal_says_the_save_is_fine(self) -> None:
        """Otherwise it reads as though the sync half-failed."""
        outcome = self.pull(core="mGBA")

        assert outcome is not None
        self.assertIn("save is unaffected", outcome.detail.lower())

    def test_a_system_with_no_clock_produces_no_outcome_at_all(self) -> None:
        """SNES has no clock file, so this must be silent, not "skipped"."""
        entry = self.entry("snes")

        self.assertIsNone(
            sync.pull_clock(entry, self.retro_save, "Snes9x", self.backups)
        )

    def test_a_game_whose_delta_clock_is_absent_is_silent(self) -> None:
        """Plenty of Game Boy games have no clock. That is not a problem."""
        self.delta_clock.unlink()

        self.assertIsNone(self.pull())

    def test_a_corrupt_delta_clock_is_reported_not_written(self) -> None:
        self.delta_clock.write_bytes(b"\x00" * 7)

        outcome = self.pull()

        assert outcome is not None
        self.assertIs(outcome.action, sync.Action.SKIPPED)
        self.assertFalse(self.retro_clock.exists())

    # -- the direction that writes into Delta ------------------------------

    def push(self, core: str = "Gambatte", dropbox=None, **kwargs):
        paths = sync.Paths(
            delta_folder=self.delta_folder,
            retroarch_config=Path("retroarch.cfg"),
            save_dir=self.save_dir,
            state_dir=Path(self._tmp.name) / "state",
        )
        return sync.push_clock(
            paths, self.entry(), self.retro_save, core, dropbox, **kwargs
        )

    def test_pushing_without_dropbox_explains_rather_than_guessing(self) -> None:
        """The revision cannot be invented, so this must not write."""
        self.retro_clock.write_bytes(REAL_GAMBATTE_CLOCK)

        outcome = self.push()

        assert outcome is not None
        self.assertFalse(outcome.applied)
        self.assertEqual(self.delta_clock.read_bytes(), REAL_DELTA_CLOCK)

    def test_pushing_an_unchanged_clock_does_nothing(self) -> None:
        self.retro_clock.write_bytes(clock.to_retroarch(REAL_DELTA_CLOCK))

        self.assertIsNone(self.push())

    def test_pushing_without_a_retroarch_clock_is_silent(self) -> None:
        self.assertIsNone(self.push())

    def test_pushing_from_an_unverified_core_is_refused(self) -> None:
        self.retro_clock.write_bytes(REAL_GAMBATTE_CLOCK)

        outcome = self.push(core="mGBA")

        assert outcome is not None
        self.assertIs(outcome.action, sync.Action.SKIPPED)
        self.assertEqual(self.delta_clock.read_bytes(), REAL_DELTA_CLOCK)


class ClockCoreGateTests(unittest.TestCase):
    """The gate itself, independent of any files."""

    def test_only_gambatte_is_cleared_for_game_boy_clocks(self) -> None:
        """Widening this is a decision that needs a real file behind it."""
        self.assertEqual(systems.SYSTEMS["gbc"].clock_cores, ("Gambatte",))

    def test_every_core_cleared_for_clocks_can_also_run_the_system(self) -> None:
        for system in systems.SYSTEMS.values():
            for core in system.clock_cores:
                self.assertIn(core, system.retroarch_cores, system.key)

    def test_a_system_declaring_a_clock_declares_both_halves(self) -> None:
        """One without the other cannot describe a conversion."""
        for system in systems.SYSTEMS.values():
            self.assertEqual(
                bool(system.delta_clock_id),
                bool(system.retroarch_clock_ext),
                system.key,
            )

    def test_no_system_clears_a_core_without_declaring_a_clock(self) -> None:
        for system in systems.SYSTEMS.values():
            if system.clock_cores:
                self.assertTrue(system.delta_clock_id, system.key)


if __name__ == "__main__":
    unittest.main()
