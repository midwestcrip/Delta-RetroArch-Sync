"""Tests for reading the rolling backups and putting one back.

The backup names here are the shapes that actually occur in the folder on this
machine, including the two that are easy to get wrong: a RetroArch save whose
game name contains dots and spaces, and a Delta clock file, whose name differs
from a Delta save only by its trailing file identifier.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import manifest as manifest_module  # noqa: E402
from delta_retroarch_synchronizer import restore, sync  # noqa: E402

GAME_SHA1 = "6b47bb75d16514b6a476aa0c73a683a2a4c18765"
CHEAT_UUID = "2946F7C3-1C4C-46D6-932F-E3F199A7ED0C"
STAMP = "20260906T210845793346"
EARLIER = "20260906T200845793346"


def write_backup(folder: Path, original: str, stamp: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{original}.{stamp}.bak"
    path.write_bytes(data)
    return path


class NameParsingTests(unittest.TestCase):
    def test_a_delta_save_backup(self) -> None:
        parsed = restore.parse_backup_name(
            f"GameSave-{GAME_SHA1}-gameSave.{STAMP}.bak"
        )
        assert parsed is not None
        original, when = parsed
        self.assertEqual(original, f"GameSave-{GAME_SHA1}-gameSave")
        self.assertEqual(when.tzinfo, timezone.utc)
        self.assertEqual(when.year, 2026)

    def test_a_name_containing_dots_still_splits(self) -> None:
        """The reason the stamp's shape decides the split, not a dot count.

        RetroArch names saves after the game, and plenty of games have a dot in
        the title -- "Super Mario Bros." among the ones on this machine.
        """
        parsed = restore.parse_backup_name(f"Super Mario Bros..srm.{STAMP}.bak")
        assert parsed is not None
        self.assertEqual(parsed[0], "Super Mario Bros..srm")

    def test_something_that_is_not_a_backup(self) -> None:
        self.assertIsNone(restore.parse_backup_name("notes.txt"))
        self.assertIsNone(restore.parse_backup_name("thing.20260906.bak"))


class ClassificationTests(unittest.TestCase):
    def make(self, original: str) -> restore.Backup:
        parsed = restore.parse_backup_name(f"{original}.{STAMP}.bak")
        assert parsed is not None
        return restore.Backup(Path(original), parsed[0], parsed[1], 0)

    def test_a_delta_save(self) -> None:
        backup = self.make(f"GameSave-{GAME_SHA1}-gameSave")
        self.assertEqual(backup.side, restore.DELTA)
        self.assertEqual(backup.kind, "save")
        self.assertEqual(backup.identifier, GAME_SHA1)
        self.assertTrue(backup.restorable)

    def test_a_delta_clock(self) -> None:
        """Differs from a save only by the trailing identifier."""
        backup = self.make(f"GameSave-{GAME_SHA1}-gameTimeSave")
        self.assertEqual(backup.kind, "clock")
        self.assertEqual(backup.identifier, GAME_SHA1)

    def test_a_delta_record_is_not_restorable_alone(self) -> None:
        backup = self.make(f"GameSave-{GAME_SHA1}")
        self.assertEqual(backup.kind, "record")
        self.assertFalse(backup.restorable)

    def test_a_lowercased_record_name_is_still_recognised(self) -> None:
        """Delta re-uploads records under Dropbox's lowercased path.

        A record Delta has touched is named `gamesave-<sha1>` on disk, so a
        case-sensitive check here would classify half the folder as RetroArch
        files and offer to restore them into the wrong place.
        """
        backup = self.make(f"gamesave-{GAME_SHA1}-gameSave")
        self.assertEqual(backup.side, restore.DELTA)
        self.assertEqual(backup.identifier, GAME_SHA1)

    def test_a_cheat_record(self) -> None:
        backup = self.make(f"Cheat-{CHEAT_UUID}")
        self.assertEqual(backup.side, restore.DELTA)
        self.assertEqual(backup.kind, "cheat")
        # The UUID has dashes of its own, so it must be rejoined rather than
        # taken as a single split field.
        self.assertEqual(backup.identifier, CHEAT_UUID)
        self.assertTrue(backup.restorable)

    def test_a_retroarch_save_and_clock(self) -> None:
        save = self.make("Super Mario World.srm")
        self.assertEqual(save.side, restore.RETROARCH)
        self.assertEqual(save.kind, "save")
        self.assertIsNone(save.identifier)

        clock = self.make("Pokemon - Crystal Version.rtc")
        self.assertEqual(clock.kind, "clock")


class ScanTests(unittest.TestCase):
    def test_newest_first_and_junk_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_backup(folder, "A.srm", EARLIER, b"old")
            write_backup(folder, "B.srm", STAMP, b"new")
            (folder / "stray.txt").write_text("not a backup", encoding="utf-8")

            found = restore.scan(folder)

            self.assertEqual([b.original_name for b in found], ["B.srm", "A.srm"])

    def test_a_missing_folder_is_empty_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(restore.scan(Path(tmp) / "nope"), [])


class LabellingTests(unittest.TestCase):
    def test_a_delta_backup_gets_the_game_name(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_backup(folder, f"GameSave-{GAME_SHA1}-gameSave", STAMP, b"x")
            points = restore.restore_points(
                restore.scan(folder), {GAME_SHA1: "Super Mario World"}
            )
            self.assertEqual(points[0].label, "Super Mario World")

    def test_a_game_delta_no_longer_has_still_lists(self) -> None:
        """Exactly when someone needs to restore it."""
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_backup(folder, f"GameSave-{GAME_SHA1}-gameSave", STAMP, b"x")
            points = restore.restore_points(restore.scan(folder), {})
            self.assertEqual(points[0].label, f"GameSave-{GAME_SHA1}-gameSave")

    def test_record_backups_are_left_out_of_the_list(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_backup(folder, f"GameSave-{GAME_SHA1}", STAMP, b"{}")
            write_backup(folder, f"GameSave-{GAME_SHA1}-gameSave", STAMP, b"x")
            points = restore.restore_points(restore.scan(folder), {})
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0].backup.kind, "save")


class RetroArchRestoreTests(unittest.TestCase):
    def test_the_file_comes_back_and_the_current_one_is_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            saves = root / "saves" / "Snes9x"
            saves.mkdir(parents=True)
            target = saves / "Super Mario World.srm"
            target.write_bytes(b"current")

            backups = root / "backups"
            write_backup(backups, "Super Mario World.srm", STAMP, b"older")
            point = restore.restore_points(restore.scan(backups), {})[0]

            restore.restore_retroarch(point, root / "saves", backups)

            self.assertEqual(target.read_bytes(), b"older")
            # And the version that was just replaced is itself now a backup, so
            # the restore can be undone.
            kept = [
                path
                for path in backups.iterdir()
                if path.read_bytes() == b"current"
            ]
            self.assertEqual(len(kept), 1)

    def test_a_save_in_a_sorted_subfolder_is_found(self) -> None:
        """RetroArch sorts saves into per-core folders on this machine."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "saves" / "mGBA"
            nested.mkdir(parents=True)
            (nested / "Game.srm").write_bytes(b"current")
            found = restore.find_retroarch_target(root / "saves", "Game.srm")
            self.assertEqual(found, nested / "Game.srm")

    def test_restoring_a_game_retroarch_no_longer_has_fails_clearly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "saves").mkdir()
            backups = root / "backups"
            write_backup(backups, "Gone.srm", STAMP, b"older")
            point = restore.restore_points(restore.scan(backups), {})[0]

            with self.assertRaises(FileNotFoundError):
                restore.restore_retroarch(point, root / "saves", backups)

    def test_a_restore_is_a_change_the_next_sync_propagates(self) -> None:
        """The reason the manifest is deliberately not updated.

        Recording the restored file as agreed would leave the two sides holding
        different saves while the manifest claimed they matched, and nothing
        would ever reconcile them again. Leaving it alone means the restore
        propagates like any other edit.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            saves = root / "saves"
            saves.mkdir()
            target = saves / "Game.srm"
            target.write_bytes(b"current")
            delta_save = root / "delta-save"
            delta_save.write_bytes(b"current")

            state = manifest_module.Manifest(root / "manifest.json")
            state.record(GAME_SHA1, delta_save, target)

            backups = root / "backups"
            write_backup(backups, "Game.srm", STAMP, b"older")
            point = restore.restore_points(restore.scan(backups), {})[0]
            restore.restore_retroarch(point, saves, backups)

            action, _ = sync.decide(state.get(GAME_SHA1), delta_save, target)
            self.assertIs(action, sync.Action.PUSH)


class CheatRestoreTests(unittest.TestCase):
    def test_the_old_code_goes_back(self) -> None:
        from test_cheat_editing import CHEAT_UUID as UUID
        from test_cheat_editing import REAL_CHEAT_RECORD, seed_cheat

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            record = seed_cheat(delta, "11111111 22222222")

            backups = root / "backups"
            write_backup(
                backups,
                f"Cheat-{UUID}",
                STAMP,
                json.dumps(REAL_CHEAT_RECORD).encode("utf-8"),
            )
            point = restore.restore_points(restore.scan(backups), {})[0]

            restore.restore_delta_cheat(point, delta, backups)

            written = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(
                written["record"]["code"], REAL_CHEAT_RECORD["record"]["code"]
            )

    def test_only_the_code_is_taken_from_the_backup(self) -> None:
        """The rest of the backed-up record belongs to whatever Delta has done
        since, so it must not be written back wholesale."""
        from test_cheat_editing import CHEAT_UUID as UUID
        from test_cheat_editing import REAL_CHEAT_RECORD, seed_cheat

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            record = seed_cheat(delta, "11111111 22222222")
            current = json.loads(record.read_text(encoding="utf-8"))
            current["sha1Hash"] = "adifferenthashwrittenbydeltasince000000"
            record.write_text(json.dumps(current), encoding="utf-8")

            backups = root / "backups"
            write_backup(
                backups,
                f"Cheat-{UUID}",
                STAMP,
                json.dumps(REAL_CHEAT_RECORD).encode("utf-8"),
            )
            point = restore.restore_points(restore.scan(backups), {})[0]

            restore.restore_delta_cheat(point, delta, backups)

            written = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(
                written["sha1Hash"], "adifferenthashwrittenbydeltasince000000"
            )


if __name__ == "__main__":
    unittest.main()
