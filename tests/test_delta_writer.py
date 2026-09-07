"""Tests for writing back into Delta's folder.

The fixture below is a real record captured from a live Delta sync on
2026-09-05, reduced to the fields the format actually uses. Its stored
``sha1Hash`` is the ground truth: if ``record_hash`` reproduces it, our model of
Harmony's hashing is right, and if it ever stops reproducing it, the format
changed and pushing must stop until it is re-derived.

It contains nothing private -- content hashes and a commercial game's title.
"""

from __future__ import annotations

import hashlib
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


def seed_consistent_record(record_path: Path, save_path: Path) -> str:
    """Leave a record and its save exactly as Delta would: in agreement.

    The captured fixture names the hash of a real save whose bytes we do not
    have, so the save is synthesised and the record adjusted to describe it. The
    top-level hash is then recomputed, which is what makes this a faithful
    "Delta wrote this last" starting point rather than an already-pushed one --
    the distinction the preservation behaviour turns on.

    Returns the top-level hash, which a push must leave untouched.
    """
    payload = bytes([0xFF]) * 131072
    save_path.write_bytes(payload)
    save_hash = hashlib.sha1(payload).hexdigest()

    record = json.loads(json.dumps(REAL_GAMESAVE_RECORD))
    record["record"]["sha1"] = save_hash
    record["files"][0]["sha1Hash"] = save_hash
    record["sha1Hash"] = delta_writer.record_hash(record)
    record_path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    return record["sha1Hash"]


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
        self.original_hash = seed_consistent_record(self.record_path, self.save_path)

        self.source = self.root / "new.srm"
        self.source.write_bytes(bytes(131072))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    NEW_REVISION = "0123456789abcdef01234"

    def push(self, revision: str | None = NEW_REVISION) -> str:
        return delta_writer.push_save(
            self.delta, GAME_SHA1, self.source, self.backups, revision=revision
        )

    def test_push_describes_the_new_save(self) -> None:
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        entry = raw["files"][0]
        expected = hashlib.sha1(self.source.read_bytes()).hexdigest()
        self.assertEqual(entry["sha1Hash"], expected)
        self.assertEqual(raw["record"]["sha1"], expected)
        self.assertEqual(entry["size"], 131072)
        self.assertEqual(self.save_path.read_bytes(), self.source.read_bytes())

    def test_push_preserves_the_top_level_hash(self) -> None:
        """The single field that stops Delta flagging a conflict.

        Harmony keeps this hash in the record JSON and in a Dropbox property
        group and compares the two. Only Delta's own app may write the property
        group, so recomputing the JSON copy updates one half of a pair and
        guarantees the mismatch that makes Delta ask the user to resolve.
        """
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["sha1Hash"], self.original_hash)

    def test_the_preserved_hash_no_longer_describes_the_record(self) -> None:
        """Deliberate, and the reason doctor stopped failing on it."""
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertFalse(delta_writer.verify_record_hash(raw))

    def test_pushing_twice_still_preserves_the_original_hash(self) -> None:
        """The second push must not preserve its own previous output.

        By then the stored hash no longer reproduces, so any check treating that
        as corruption would refuse, and any code that recomputed would put the
        mismatch straight back.
        """
        self.push()
        self.source.write_bytes(bytes([0x01]) * 131072)
        self.push()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["sha1Hash"], self.original_hash)
        self.assertEqual(
            raw["record"]["sha1"], hashlib.sha1(bytes([0x01]) * 131072).hexdigest()
        )

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

    def test_refuses_when_the_record_disagrees_with_the_save_beside_it(self) -> None:
        # The preflight that replaced the hash check. Reproducing the stored
        # hash stopped being a valid precondition once it began being preserved.
        # What is still an invariant is that a record describes the save next to
        # it; if it does not, something failed halfway and a push would build on
        # a broken state.
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        raw["record"]["sha1"] = "0" * 40
        self.record_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.push()
        # And nothing was touched.
        self.assertEqual(self.save_path.read_bytes(), b"\xff" * 131072)
        self.assertFalse(self.backups.exists())

    def test_refuses_a_save_that_changed_size(self) -> None:
        """A cartridge's SRAM is fixed, so a size change is a format change.

        The live case is Game Boy Color: Delta keeps the clock in a separate
        4-byte file, while some libretro cores append RTC state to the .srm.
        Pushing that verbatim would hand Delta a save it cannot read, in the one
        direction that can damage the phone's copy.
        """
        self.source.write_bytes(bytes(131072) + b"RTC!")

        with self.assertRaises(ValueError) as caught:
            self.push()
        self.assertIn("131,076", str(caught.exception))
        self.assertIn("131,072", str(caught.exception))

        # Refused before anything was touched, backups included.
        self.assertEqual(self.save_path.read_bytes(), b"\xff" * 131072)
        self.assertFalse(self.backups.exists())

    def test_refuses_a_record_with_no_hash_to_preserve(self) -> None:
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        del raw["sha1Hash"]
        self.record_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.push()
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


class FilenameCasingTests(unittest.TestCase):
    """Writing must not rename Delta's files.

    Delta re-uploads records to Dropbox's lowercased path, so a record it has
    touched is "gamesave-<sha1>" on disk. os.replace renames the target to
    whatever spelling it is given, so constructing the name silently renames
    Delta's file -- a real change to another app's storage.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "Delta Emulator"
        self.delta.mkdir()

        # Named the way Delta leaves it after re-uploading: all lowercase.
        self.record_path = self.delta / f"gamesave-{GAME_SHA1}"
        self.save_path = self.delta / f"gamesave-{GAME_SHA1}-gameSave"
        seed_consistent_record(self.record_path, self.save_path)

        self.source = self.root / "new.srm"
        self.source.write_bytes(bytes(131072))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lowercase_names_are_found_and_preserved(self) -> None:
        # The file *identifier* inside the record stays "gameSave" -- that is
        # Harmony's key, not a path. Only the on-disk spelling is lowercase.
        delta_writer.push_save(
            self.delta,
            GAME_SHA1,
            self.source,
            self.root / "backups",
            revision="0123456789abcdef01234",
        )
        names = sorted(p.name for p in self.delta.iterdir())
        self.assertIn(f"gamesave-{GAME_SHA1}", names)
        self.assertIn(f"gamesave-{GAME_SHA1}-gameSave", names)
        # No capitalised twin was created alongside.
        self.assertNotIn(f"GameSave-{GAME_SHA1}", names)

    def test_resolve_existing_returns_the_real_spelling(self) -> None:
        from delta_retroarch_synchronizer import harmony

        found = harmony.resolve_existing(self.delta, f"GameSave-{GAME_SHA1}")
        self.assertEqual(found.name, f"gamesave-{GAME_SHA1}")

    def test_resolve_existing_falls_back_to_the_requested_name(self) -> None:
        from delta_retroarch_synchronizer import harmony

        found = harmony.resolve_existing(self.delta, "Cheat-does-not-exist")
        self.assertEqual(found.name, "Cheat-does-not-exist")


if __name__ == "__main__":
    unittest.main()


class SecondaryFileTests(unittest.TestCase):
    """Pushing a file that is not the record's primary save.

    Game Boy Color records carry two files: the battery save and a four-byte
    real-time clock. They share a record but not a hash -- ``record.sha1``
    describes the save and nothing else, verified against a real Crystal record
    whose ``record.sha1`` equals its ``gameSave`` entry while ``gameTimeSave``
    carries its own.

    Before this was handled, pushing the clock would have set ``record.sha1`` to
    the clock's hash -- telling Delta the 32KB battery save had become four
    bytes. It failed closed rather than doing that, because the preflight
    compared ``record.sha1`` against whichever file was being written, so a
    clock push always looked like an inconsistent record.
    """

    CLOCK = bytes.fromhex("6a9dcb79")
    NEW_CLOCK = bytes.fromhex("6a9dcc00")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "Delta Emulator"
        self.delta.mkdir()
        self.backups = self.root / "backups"

        self.record_path = self.delta / f"GameSave-{GAME_SHA1}"
        self.save_path = self.delta / f"GameSave-{GAME_SHA1}-gameSave"
        self.save_hash = seed_consistent_record(self.record_path, self.save_path)

        # Add the clock as a second file, exactly as a real GBC record has it.
        self.clock_path = self.delta / f"GameSave-{GAME_SHA1}-gameTimeSave"
        self.clock_path.write_bytes(self.CLOCK)
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))
        raw["files"].append(
            {
                "versionIdentifier": "aaaaaaaaaaaaaaaaaaaaa",
                "remoteIdentifier": f"/delta emulator/gamesave-{GAME_SHA1}-gametimesave",
                "size": 4,
                "identifier": "gameTimeSave",
                "sha1Hash": hashlib.sha1(self.CLOCK).hexdigest(),
            }
        )
        self.record_path.write_text(
            json.dumps(raw, separators=(",", ":")), encoding="utf-8"
        )
        self.preserved = raw["sha1Hash"]
        self.primary_sha1 = raw["record"]["sha1"]

        self.source = self.root / "new.rtc"
        self.source.write_bytes(self.NEW_CLOCK)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def push_clock(self, revision: str | None = "bbbbbbbbbbbbbbbbbbbbb") -> str:
        return delta_writer.push_save(
            self.delta,
            GAME_SHA1,
            self.source,
            self.backups,
            file_identifier="gameTimeSave",
            revision=revision,
        )

    def test_pushing_the_clock_writes_only_the_clock(self) -> None:
        self.push_clock()

        self.assertEqual(self.clock_path.read_bytes(), self.NEW_CLOCK)
        self.assertEqual(len(self.save_path.read_bytes()), 131072)

    def test_the_records_save_hash_is_left_alone(self) -> None:
        """The battery save did not change, so record.sha1 must not either."""
        self.push_clock()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        self.assertEqual(raw["record"]["sha1"], self.primary_sha1)

    def test_the_clocks_own_entry_is_updated(self) -> None:
        self.push_clock()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        entry = next(f for f in raw["files"] if f["identifier"] == "gameTimeSave")
        self.assertEqual(entry["sha1Hash"], hashlib.sha1(self.NEW_CLOCK).hexdigest())
        self.assertEqual(entry["size"], 4)
        self.assertEqual(entry["versionIdentifier"], "bbbbbbbbbbbbbbbbbbbbb")

    def test_the_saves_entry_is_untouched(self) -> None:
        self.push_clock()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        entry = next(f for f in raw["files"] if f["identifier"] == "gameSave")
        self.assertEqual(entry["sha1Hash"], self.primary_sha1)
        self.assertEqual(entry["size"], 131072)

    def test_the_top_level_hash_is_still_preserved(self) -> None:
        """The rule that stops Delta conflicting applies to every file."""
        self.push_clock()
        raw = json.loads(self.record_path.read_text(encoding="utf-8"))

        self.assertEqual(raw["sha1Hash"], self.preserved)

    def test_the_modified_date_still_moves(self) -> None:
        before = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.push_clock()
        after = json.loads(self.record_path.read_text(encoding="utf-8"))

        self.assertGreater(
            after["record"]["modifiedDate"], before["record"]["modifiedDate"]
        )

    def test_a_clock_of_the_wrong_size_is_refused(self) -> None:
        """The size guard covers every file on the record, not just the save."""
        self.source.write_bytes(b"\x00" * 8)

        with self.assertRaises(ValueError) as caught:
            self.push_clock()

        self.assertIn("changed format", str(caught.exception))
        self.assertEqual(self.clock_path.read_bytes(), self.CLOCK)

    def test_a_record_inconsistent_with_its_save_still_blocks_a_clock_push(self) -> None:
        """The preflight must keep checking the save, not the file being written."""
        self.save_path.write_bytes(b"\x01" * 131072)

        with self.assertRaises(ValueError) as caught:
            self.push_clock()

        self.assertIn("refusing to push", str(caught.exception))
        self.assertEqual(self.clock_path.read_bytes(), self.CLOCK)
