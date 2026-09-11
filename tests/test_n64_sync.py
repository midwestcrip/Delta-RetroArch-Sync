"""The N64 conversion as the sync pass actually reaches it.

Kept apart from ``test_n64.py``, which covers the byte layout in isolation. What
matters here is the wiring: that a pull *merges* instead of copying, that a push
extracts only this cartridge's bytes, and that the Controller Pak notice is said
once and never again.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import inspect as inspect_module  # noqa: E402
from delta_retroarch_synchronizer import manifest as manifest_module  # noqa: E402
from delta_retroarch_synchronizer import n64, sync, systems  # noqa: E402

EEPROM_4K = 0x200
SRAM = 0x8000


def entry_for(
    key: str, name: str, save_path: Path | None
) -> inspect_module.GameEntry:
    system = systems.SYSTEMS[key]
    return inspect_module.GameEntry(
        identifier=key * 8,
        name=name,
        delta_type=system.delta_type,
        system=system,
        rom_path=None,
        save_path=save_path,
        extra_paths={},
    )


class ConversionWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta_save = self.root / "delta-save"
        self.target = self.root / "saves" / "Ocarina of Time.srm"
        self.target.parent.mkdir(parents=True)
        self.entry = entry_for("n64", "Ocarina of Time", self.delta_save)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_pull_merges_rather_than_copying(self) -> None:
        self.delta_save.write_bytes(bytes([0x77]) * SRAM)

        note = sync.write_to_retroarch(self.entry, self.delta_save, self.target)

        written = self.target.read_bytes()
        self.assertEqual(len(written), n64.SRM_SIZE)
        self.assertEqual(written[n64.SRAM.start : n64.SRAM.end], bytes([0x77]) * SRAM)
        self.assertIn("SRAM", note)

    def test_a_pull_keeps_controller_pak_data_already_on_this_pc(self) -> None:
        """The whole reason a pull cannot be a file copy for N64.

        Copying Delta's 32 KB over RetroArch's 296,960-byte file would not just
        lose the Controller Paks, it would leave a file no core could read.
        """
        srm = bytearray(n64.blank_srm())
        srm[n64.PAKS_START + 800 : n64.PAKS_START + 809] = b"GHOSTDATA"
        self.target.write_bytes(bytes(srm))
        self.delta_save.write_bytes(bytes([0x01]) * SRAM)

        sync.write_to_retroarch(self.entry, self.delta_save, self.target)

        self.assertIn(b"GHOSTDATA", self.target.read_bytes())

    def test_a_first_pull_formats_the_paks_rather_than_padding_them(self) -> None:
        self.delta_save.write_bytes(bytes([0x01]) * SRAM)

        note = sync.write_to_retroarch(self.entry, self.delta_save, self.target)

        self.assertIn("formatted", note)
        for pak in n64.controller_paks(self.target.read_bytes()):
            self.assertTrue(n64.controller_pak_is_empty(pak))
            self.assertEqual(pak[0], 0x81)

    def test_a_push_extracts_only_this_cartridges_bytes(self) -> None:
        self.delta_save.write_bytes(bytes(SRAM))
        srm = bytearray(n64.blank_srm())
        srm[n64.SRAM.start : n64.SRAM.end] = bytes([0x99]) * SRAM
        self.target.write_bytes(bytes(srm))

        staged, temporary = sync.stage_for_delta(self.entry, self.target, self.root)

        self.assertTrue(temporary)
        self.assertEqual(staged.read_bytes(), bytes([0x99]) * SRAM)
        # Exactly the size Delta expects, which is what lets the push size guard
        # pass without a per-system exemption.
        self.assertEqual(staged.stat().st_size, self.delta_save.stat().st_size)

    def test_a_push_refuses_when_delta_cannot_say_how_big_the_save_is(self) -> None:
        """The size comes from Delta's record, never from inspecting the .srm.

        Without it there is no way to tell a 4 Kbit EEPROM save from a 16 Kbit
        one, and guessing would send the wrong number of bytes home.
        """
        self.target.write_bytes(n64.blank_srm())
        with self.assertRaises(ValueError):
            sync.stage_for_delta(self.entry, self.target, self.root)

    def test_a_plain_copy_system_is_untouched_by_any_of_this(self) -> None:
        entry = entry_for("snes", "Super Mario World", self.delta_save)
        self.delta_save.write_bytes(bytes([0x42]) * 2048)
        target = self.root / "saves" / "Super Mario World.srm"

        sync.write_to_retroarch(entry, self.delta_save, target)
        self.assertEqual(target.read_bytes(), bytes([0x42]) * 2048)

        staged, temporary = sync.stage_for_delta(entry, target, self.root)
        self.assertFalse(temporary)
        self.assertEqual(staged, target)


class ControllerPakNoticeTests(unittest.TestCase):
    """Said once, and from evidence rather than from a list of titles.

    A hardcoded list of "games that use the Controller Pak" would be a guess
    presented as a fact. A pack with data in it is not a guess.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "Mario Kart 64.srm"
        self.delta_save = self.root / "delta-save"
        self.state = manifest_module.Manifest(self.root / "manifest.json")
        self.entry = entry_for("n64", "Mario Kart 64", self.delta_save)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def used_pak_srm(self) -> bytes:
        """A combined save whose first Controller Pak has an allocated note."""
        srm = bytearray(n64.blank_srm())
        table = n64.PAKS_START + 256
        srm[table + 10 : table + 12] = (5).to_bytes(2, "big")
        return bytes(srm)

    def test_nothing_is_said_when_no_pak_holds_anything(self) -> None:
        self.target.write_bytes(n64.blank_srm())
        self.assertIsNone(
            sync.controller_pak_notice(self.entry, self.target, self.state)
        )

    def test_it_is_said_when_a_pak_holds_something(self) -> None:
        self.target.write_bytes(self.used_pak_srm())
        self.delta_save.write_bytes(bytes(EEPROM_4K))

        outcome = sync.controller_pak_notice(self.entry, self.target, self.state)

        assert outcome is not None
        self.assertEqual(outcome.game, "Mario Kart 64")
        self.assertIn("Controller Pak", outcome.detail)
        # Not a failure: the cartridge save still synced perfectly.
        self.assertFalse(outcome.failed)

    def test_it_is_said_exactly_once(self) -> None:
        """A message that repeats every sync is one people learn to scroll past,
        and this log has already lost a real confirmation that way."""
        self.target.write_bytes(self.used_pak_srm())
        self.delta_save.write_bytes(bytes(EEPROM_4K))

        first = sync.controller_pak_notice(self.entry, self.target, self.state)
        second = sync.controller_pak_notice(self.entry, self.target, self.state)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_game_with_no_cartridge_save_gets_the_stronger_wording(self) -> None:
        """If Delta holds no save at all, everything this game keeps is on the
        pak, so nothing whatsoever will travel -- worth saying plainly."""
        self.target.write_bytes(self.used_pak_srm())

        outcome = sync.controller_pak_notice(self.entry, self.target, self.state)

        assert outcome is not None
        self.assertIn("Nothing will travel", outcome.detail)

    def test_other_systems_never_get_the_notice(self) -> None:
        entry = entry_for("snes", "Super Mario World", self.delta_save)
        self.target.write_bytes(self.used_pak_srm())
        self.assertIsNone(
            sync.controller_pak_notice(entry, self.target, self.state)
        )

    def test_a_save_that_is_not_a_combined_file_is_ignored(self) -> None:
        self.target.write_bytes(b"not an srm")
        self.assertIsNone(
            sync.controller_pak_notice(self.entry, self.target, self.state)
        )

    def test_the_notice_is_remembered_across_runs(self) -> None:
        """Or "once" would mean once per sync, which is not once."""
        self.target.write_bytes(self.used_pak_srm())
        sync.controller_pak_notice(self.entry, self.target, self.state)
        self.state.save()

        reloaded = manifest_module.Manifest.load(self.root / "manifest.json")

        self.assertIsNone(
            sync.controller_pak_notice(self.entry, self.target, reloaded)
        )

    def test_an_older_manifest_without_notices_still_loads(self) -> None:
        path = self.root / "old-manifest.json"
        path.write_text('{"version": 1, "games": {}}', encoding="utf-8")

        loaded = manifest_module.Manifest.load(path)

        self.assertEqual(loaded.notices, set())
        self.assertFalse(loaded.already_said("anything"))


class ConvertedComparisonTests(unittest.TestCase):
    """A converted save has to be *compared* converted, not just pushed that way.

    RetroArch's combined 296,960-byte .srm is never byte-equal to Delta's single
    storage even when the two hold exactly the same save. So comparing them
    whole answers a question nobody asked, and one branch in particular could
    never be reached: "both sides already identical" is what establishes a first
    agreement, and for N64 it could not fire. A game with no manifest history
    had no route back to one -- every run took the first-sync branch, saw two
    files that differ, and reported a conflict with nothing conflicting in it.

    Seven real games on the development machine were stuck exactly that way.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "GameSave-abc-gameSave"
        self.target = self.root / "Game.srm"
        self.state = manifest_module.Manifest(self.root / "manifest.json")

        self.save = bytes(range(256)) * (SRAM // 256)
        self.delta.write_bytes(self.save)
        self.target.write_bytes(n64.to_retroarch(self.save))
        self.entry = entry_for("n64", "Game", self.delta)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_same_save_in_both_shapes_reads_as_identical(self) -> None:
        body = sync.converted_body(self.entry)

        action, detail = sync.decide(
            self.state.get("abc"), self.delta, self.target, desktop_body=body
        )

        self.assertIs(action, sync.Action.NOTHING)
        self.assertEqual(detail, "both sides already identical")

    def test_without_the_conversion_it_is_a_phantom_conflict(self) -> None:
        """What the same two files used to say, which is why this exists."""
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.target)

        self.assertIs(action, sync.Action.CONFLICT)

    def test_a_genuinely_different_save_is_still_a_conflict(self) -> None:
        """Narrowing the comparison must not narrow it to nothing."""
        self.target.write_bytes(n64.to_retroarch(b"\x5a" * SRAM))
        body = sync.converted_body(self.entry)

        action, _ = sync.decide(
            self.state.get("abc"), self.delta, self.target, desktop_body=body
        )

        self.assertIs(action, sync.Action.CONFLICT)

    def test_an_unconverted_system_gets_no_narrowing(self) -> None:
        """Every other system's file already is the save; nothing to extract."""
        self.assertIsNone(sync.converted_body(entry_for("gba", "Game", self.delta)))

    def test_a_save_the_conversion_cannot_read_falls_back(self) -> None:
        """An .srm of the wrong length must not raise out of the decision.

        `to_delta` refuses a file that is not the size mupen64plus-next writes.
        Falling back to the whole file means such a save compares exactly as it
        did before this existed -- no better, and importantly no worse.
        """
        body = sync.converted_body(self.entry)
        assert body is not None

        self.assertEqual(body(b"too short"), b"too short")


if __name__ == "__main__":
    unittest.main()
