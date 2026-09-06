"""Tests for writing back into Delta's folder.

The fixture below is a real record captured from a live Delta sync on
2026-09-05, reduced to the fields the format actually uses. Its stored
``sha1Hash`` is the ground truth: if ``record_hash`` reproduces it, our model of
Harmony's hashing is right, and if it ever stops reproducing it, the format
changed and pushing must stop until it is re-derived.

It contains nothing private -- content hashes and a commercial game's title.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import delta_writer  # noqa: E402

GAME_SHA1 = "dd5945db9b930750cb39d00c84da8571feebf417"
SAVE_SHA1 = "22afa41d1347ca65f58c279747ea9a9bdbd7f901"

#: Verbatim from C:\Data\Dropbox\Delta Emulator, 2026-09-05.
REAL_GAMESAVE_RECORD = {
    "files": [
        {
            "versionIdentifier": "65ac6bfd57295cb175c93",
            "remoteIdentifier": f"/delta emulator/gamesave-{GAME_SHA1}-gamesave",
            "size": 131072,
            "identifier": "gameSave",
            "sha1Hash": SAVE_SHA1,
        }
    ],
    "type": "GameSave",
    "record": {"sha1": SAVE_SHA1, "modifiedDate": 810352323.12742},
    "sha1Hash": "e52196d7adb2c452e6b6c90c36972fef9d6d3f30",
    "relationships": {"game": {"type": "Game", "identifier": GAME_SHA1}},
    "identifier": GAME_SHA1,
}

#: Also verbatim: a Game record, whose `record` holds base64-archived values.
REAL_GAME_RECORD_HASH = "eb150f91638e56e8d94925f03386039a49f02584"


class RecordHashTests(unittest.TestCase):
    def test_reproduces_a_real_records_stored_hash(self) -> None:
        self.assertEqual(
            delta_writer.record_hash(REAL_GAMESAVE_RECORD),
            REAL_GAMESAVE_RECORD["sha1Hash"],
        )

    def test_verify_accepts_the_real_record(self) -> None:
        self.assertTrue(delta_writer.verify_record_hash(REAL_GAMESAVE_RECORD))

    def test_verify_rejects_a_tampered_record(self) -> None:
        tampered = json.loads(json.dumps(REAL_GAMESAVE_RECORD))
        tampered["record"]["sha1"] = "0" * 40
        self.assertFalse(delta_writer.verify_record_hash(tampered))

    def test_hashing_form_drops_sha1Hash_and_flattens_files(self) -> None:
        form = delta_writer.hashing_form(REAL_GAMESAVE_RECORD)
        self.assertNotIn("sha1Hash", form)
        self.assertEqual(form["files"], {"gameSave": SAVE_SHA1})

    def test_encoding_is_compact_and_sorted(self) -> None:
        encoded = delta_writer.encode_for_hashing({"b": 1, "a": 2})
        self.assertEqual(encoded, b'{"a":2,"b":1}')

    def test_encoding_escapes_forward_slashes(self) -> None:
        encoded = delta_writer.encode_for_hashing({"p": "a/b"})
        self.assertEqual(encoded, b'{"p":"a\\/b"}')


class PushTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "Delta Emulator"
        self.delta.mkdir()
        self.backups = self.root / "backups"

        self.record_path = self.delta / f"GameSave-{GAME_SHA1}"
        self.save_path = self.delta / f"GameSave-{GAME_SHA1}-gameSave"
        self.record_path.write_text(
            json.dumps(REAL_GAMESAVE_RECORD, separators=(",", ":")), encoding="utf-8"
        )
        self.save_path.write_bytes(b"\xff" * 131072)

        self.source = self.root / "new.srm"
        self.source.write_bytes(b"\x00" * 131072)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    NEW_REVISION = "0123456789abcdef01234"

    def push(self, revision: str | None = NEW_REVISION) -> str:
        return delta_writer.push_save(
            self.delta, GAME_SHA1, self.source, self.backups, revision=revision
        )

    def test_push_updates_save_record_and_hash_consistently(self) -> None:
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        # The written record must itself verify, or Delta sees it as corrupt.
        self.assertTrue(delta_writer.verify_record_hash(raw))

        entry = raw["files"][0]
        self.assertEqual(entry["sha1Hash"], raw["record"]["sha1"])
        self.assertEqual(entry["size"], 131072)
        self.assertEqual(self.save_path.read_bytes(), self.source.read_bytes())

    def test_version_identifier_becomes_the_real_revision(self) -> None:
        # Leaving the old revision makes Delta re-download the previous save,
        # silently undoing the push. A bogus one makes the sync fail outright --
        # both were observed. It has to be the actual new revision.
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["files"][0]["versionIdentifier"], self.NEW_REVISION)

    def test_no_revision_leaves_the_existing_one_untouched(self) -> None:
        self.push(revision=None)
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["files"][0]["versionIdentifier"], "65ac6bfd57295cb175c93"
        )

    def test_both_pieces_are_backed_up_before_writing(self) -> None:
        original_save = self.save_path.read_bytes()
        self.push()
        record_backups = list(self.backups.glob(f"GameSave-{GAME_SHA1}.*.bak"))
        save_backups = list(self.backups.glob(f"GameSave-{GAME_SHA1}-gameSave.*.bak"))
        self.assertEqual(len(record_backups), 1)
        self.assertEqual(len(save_backups), 1)
        self.assertEqual(save_backups[0].read_bytes(), original_save)

    def test_refuses_a_record_whose_hash_cannot_be_reproduced(self) -> None:
        # If the format ever changes, the preflight must stop the write rather
        # than produce a record Delta cannot trust.
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        raw["sha1Hash"] = "deadbeef" * 5
        self.record_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.push()
        # And nothing was touched.
        self.assertEqual(self.save_path.read_bytes(), b"\xff" * 131072)
        self.assertFalse(self.backups.exists())

    def test_missing_record_raises_before_writing(self) -> None:
        self.record_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.push()

    def test_apple_epoch_round_trips(self) -> None:
        from delta_retroarch_synchronizer import manifest

        unix = 1757000000.0
        apple = delta_writer.unix_to_apple(unix)
        self.assertAlmostEqual(manifest.apple_timestamp_to_unix(apple), unix, places=6)


if __name__ == "__main__":
    unittest.main()
