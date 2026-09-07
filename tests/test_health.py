"""Tests for the push health checks.

Each case reproduces a state that actually occurred on 2026-09-05, when a push
failed with no error reported anywhere. A check that does not catch the thing it
was written for is worse than no check, so they are driven from real breakage
rather than from imagined failure modes.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import delta_writer, health  # noqa: E402

GAME_SHA1 = "dd5945db9b930750cb39d00c84da8571feebf417"
SAVE_BYTES = bytes([0xFF]) * 1024


def build_record(save_hash: str, revision: str = "65ac6bfd57295cb175c93") -> dict:
    record = {
        "type": "GameSave",
        "identifier": GAME_SHA1,
        "record": {"sha1": save_hash, "modifiedDate": 810352323.12742},
        "files": [
            {
                "identifier": "gameSave",
                "sha1Hash": save_hash,
                "size": len(SAVE_BYTES),
                "remoteIdentifier": f"/delta emulator/gamesave-{GAME_SHA1}-gamesave",
                "versionIdentifier": revision,
            }
        ],
        "relationships": {"game": {"type": "Game", "identifier": GAME_SHA1}},
    }
    record["sha1Hash"] = delta_writer.record_hash(record)
    return record


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        import hashlib

        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "Delta Emulator"
        self.folder.mkdir()

        self.save_hash = hashlib.sha1(SAVE_BYTES).hexdigest()
        self.record_path = self.folder / f"GameSave-{GAME_SHA1}"
        self.save_path = self.folder / f"GameSave-{GAME_SHA1}-gameSave"
        self.save_path.write_bytes(SAVE_BYTES)
        self._write(build_record(self.save_hash))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, record: dict) -> None:
        self.record_path.write_text(
            json.dumps(record, separators=(",", ":")), encoding="utf-8"
        )

    def named(self, report: health.Health, name: str) -> health.Check:
        return next(check for check in report.checks if check.name == name)

    def test_a_consistent_record_passes(self) -> None:
        report = health.check_game(self.folder, GAME_SHA1)
        self.assertTrue(report.ok, [c.detail for c in report.problems])

    def test_a_hash_that_no_longer_matches_is_reported_not_failed(self) -> None:
        """It used to fail here, and that was wrong.

        A push deliberately preserves the record's top-level hash so it stays
        equal to the Dropbox property group only Delta can write. The value
        therefore stops describing the record's contents on purpose, and failing
        on it would mark every successfully pushed save as corrupt.
        """
        record = build_record(self.save_hash)
        record["sha1Hash"] = "deadbeef" * 5
        self._write(record)

        report = health.check_game(self.folder, GAME_SHA1)
        self.assertTrue(report.ok)

        hash_check = next(c for c in report.checks if c.name == "record hash")
        self.assertIn("preserved", hash_check.detail)

    def test_a_delta_written_hash_is_reported_as_such(self) -> None:
        self._write(build_record(self.save_hash))

        report = health.check_game(self.folder, GAME_SHA1)

        hash_check = next(c for c in report.checks if c.name == "record hash")
        self.assertIn("as Delta last wrote it", hash_check.detail)

    def _age(self, seconds: int = 3600) -> None:
        """Backdate the record and save.

        A disagreement between two files written seconds ago is Dropbox still
        downloading them, and is reported as such. Making it a *real* fault
        means making it an old one.
        """
        import os
        import time

        when = time.time() - seconds
        for path in (self.record_path, self.save_path):
            os.utime(path, (when, when))

    def test_catches_a_record_pointing_at_different_content(self) -> None:
        # The state a half-applied push leaves behind: record and save disagree.
        self._write(build_record("0" * 40))
        self._age()

        report = health.check_game(self.folder, GAME_SHA1)
        self.assertFalse(report.ok)
        self.assertFalse(self.named(report, "record points at the save on disk").ok)

    def test_a_fresh_disagreement_is_reported_as_still_arriving(self) -> None:
        """The Kirby's Adventure case, 2026-09-07.

        Delta's record and its save are downloaded as two independent files and
        landed twelve seconds apart. A sync in that window saw a record
        describing a save that had not arrived yet and called it corruption,
        which is alarming and wrong -- it resolves itself.
        """
        self._write(build_record("0" * 40))  # left with a current mtime

        report = health.check_game(self.folder, GAME_SHA1)

        check = self.named(report, "record points at the save on disk")
        self.assertTrue(check.ok)
        self.assertIn("still arriving", check.detail)

    def test_catches_a_missing_record(self) -> None:
        # Exactly the state that had to be created by hand to unstick Delta.
        self.record_path.unlink()
        report = health.check_game(self.folder, GAME_SHA1)
        self.assertFalse(report.ok)
        self.assertFalse(self.named(report, "record present").ok)

    def test_finds_a_lowercase_record(self) -> None:
        # Delta renames records to Dropbox's lowercased path, so the checks must
        # not depend on the capitalisation the tool would construct.
        renamed = self.folder / f"gamesave-{GAME_SHA1}"
        self.record_path.rename(renamed)
        report = health.check_game(self.folder, GAME_SHA1)
        self.assertTrue(report.ok, [c.detail for c in report.problems])

    def test_unparseable_record_is_reported_not_raised(self) -> None:
        self.record_path.write_text("{ not json", encoding="utf-8")
        report = health.check_game(self.folder, GAME_SHA1)
        self.assertFalse(report.ok)
        self.assertFalse(self.named(report, "record parses").ok)


class WriteInPlaceTests(unittest.TestCase):
    """The write must keep the file itself, not replace it.

    A rename over the target loses the Dropbox property groups Delta needs to
    see the record at all, which is what broke pushing on 2026-09-05.
    """

    def test_contents_change_but_the_file_does_not(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            path.write_bytes(b"original contents, quite long")
            before = path.stat().st_ino if hasattr(path.stat(), "st_ino") else None

            delta_writer.write_in_place(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")
            if before:
                self.assertEqual(path.stat().st_ino, before)

    def test_shrinking_content_truncates(self) -> None:
        # Without truncate, the tail of the old record would survive and the
        # file would no longer be valid JSON.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            path.write_bytes(b'{"a": "a long original value"}')
            delta_writer.write_in_place(path, b'{"a": 1}')
            self.assertEqual(path.read_bytes(), b'{"a": 1}')

    def test_no_temporary_files_are_left_behind(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "record"
            path.write_bytes(b"x" * 100)
            delta_writer.write_in_place(path, b"y" * 50)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["record"])


if __name__ == "__main__":
    unittest.main()
