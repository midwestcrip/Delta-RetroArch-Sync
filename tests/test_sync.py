"""Tests for the reconcile decision and the write path.

The decision table is the safety-critical part of this project: every way a save
can be lost runs through it. So each of the four outcomes is pinned, including
the ones that must refuse to act.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import manifest, sync  # noqa: E402

DELTA_BYTES = b"delta save" + b"\x00" * 1000
RETRO_BYTES = b"retro save" + b"\x00" * 1000


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.delta = root / "GameSave-abc-gameSave"
        self.retro = root / "Game.srm"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, path: Path, data: bytes) -> None:
        path.write_bytes(data)

    def agreed(self) -> manifest.Entry:
        """A manifest entry recording both files exactly as they are now."""
        return manifest.Entry(
            delta=manifest.FileState.of(self.delta),
            retroarch=manifest.FileState.of(self.retro),
        )

    def test_neither_side_has_a_save(self) -> None:
        action, _ = sync.decide(manifest.Entry(), self.delta, self.retro)
        self.assertIs(action, sync.Action.MISSING)

    def test_first_sync_pulls_when_only_delta_has_one(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        action, _ = sync.decide(manifest.Entry(), self.delta, self.retro)
        self.assertIs(action, sync.Action.PULL)

    def test_first_sync_pushes_when_only_retroarch_has_one(self) -> None:
        self.write(self.retro, RETRO_BYTES)
        action, _ = sync.decide(manifest.Entry(), self.delta, self.retro)
        self.assertIs(action, sync.Action.PUSH)

    def test_first_sync_with_identical_saves_is_a_no_op(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, DELTA_BYTES)
        action, _ = sync.decide(manifest.Entry(), self.delta, self.retro)
        self.assertIs(action, sync.Action.NOTHING)

    def test_first_sync_with_two_different_saves_is_a_conflict(self) -> None:
        # No agreed history and two different saves: picking either one would
        # discard real progress, so it must refuse.
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        action, _ = sync.decide(manifest.Entry(), self.delta, self.retro)
        self.assertIs(action, sync.Action.CONFLICT)

    def test_unchanged_on_both_sides_does_nothing(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        action, _ = sync.decide(self.agreed(), self.delta, self.retro)
        self.assertIs(action, sync.Action.NOTHING)

    def test_only_delta_changed_pulls(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        entry = self.agreed()
        self.write(self.delta, DELTA_BYTES + b"progress")
        action, _ = sync.decide(entry, self.delta, self.retro)
        self.assertIs(action, sync.Action.PULL)

    def test_only_retroarch_changed_pushes(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        entry = self.agreed()
        self.write(self.retro, RETRO_BYTES + b"progress")
        action, _ = sync.decide(entry, self.delta, self.retro)
        self.assertIs(action, sync.Action.PUSH)

    def test_both_changed_is_a_conflict(self) -> None:
        # The crash-resilience case: played on desktop, RetroArch died before
        # the post-close push, then played on the phone too.
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        entry = self.agreed()
        self.write(self.delta, DELTA_BYTES + b"phone")
        self.write(self.retro, RETRO_BYTES + b"desktop")
        action, _ = sync.decide(entry, self.delta, self.retro)
        self.assertIs(action, sync.Action.CONFLICT)

    def test_a_deleted_save_counts_as_a_change(self) -> None:
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        entry = self.agreed()
        self.retro.unlink()
        action, _ = sync.decide(entry, self.delta, self.retro)
        self.assertIs(action, sync.Action.PULL)

    def test_same_size_different_content_is_still_a_change(self) -> None:
        # Size is only a fast path; the hash has to be what decides.
        self.write(self.delta, DELTA_BYTES)
        self.write(self.retro, RETRO_BYTES)
        entry = self.agreed()
        self.write(self.delta, b"DELTA save" + b"\x00" * 1000)
        self.assertEqual(len(DELTA_BYTES), self.delta.stat().st_size)
        action, _ = sync.decide(entry, self.delta, self.retro)
        self.assertIs(action, sync.Action.PULL)


class WritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_copy_is_byte_exact_and_leaves_no_partial_file(self) -> None:
        source = self.root / "src.bin"
        source.write_bytes(DELTA_BYTES)
        destination = self.root / "nested" / "dst.srm"
        sync.copy_atomically(source, destination)
        self.assertEqual(destination.read_bytes(), DELTA_BYTES)
        self.assertEqual(list(destination.parent.glob("*.partial")), [])

    def test_backup_is_made_before_overwrite_and_pruned(self) -> None:
        target = self.root / "Game.srm"
        backups = self.root / "backups"
        for index in range(15):
            target.write_bytes(b"version %d" % index)
            sync.backup(target, backups, keep=10)
        kept = sorted(backups.glob("Game.srm.*.bak"))
        self.assertEqual(len(kept), 10)
        # The most recent backup holds the most recent pre-overwrite content.
        self.assertEqual(kept[-1].read_bytes(), b"version 14")

    def test_a_rom_name_with_brackets_in_it_is_still_pruned(self) -> None:
        """Region tags are ordinary in ROM names, and they are glob syntax.

        A save is named after its ROM, so "Zelda [U].srm" is the normal case
        rather than the exotic one. Interpolated straight into a glob pattern,
        "[U]" is a character class matching the single letter U: the pattern
        never matches this file's own backups, so pruning silently stopped and
        they grew without bound.
        """
        target = self.root / "Zelda [U].srm"
        backups = self.root / "backups"
        for index in range(15):
            target.write_bytes(b"version %d" % index)
            sync.backup(target, backups, keep=10)
        kept = sorted(path for path in backups.iterdir() if path.suffix == ".bak")
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[-1].read_bytes(), b"version 14")

    def test_backup_of_a_missing_file_is_a_no_op(self) -> None:
        self.assertIsNone(sync.backup(self.root / "nope.srm", self.root / "backups"))


class ManifestTests(unittest.TestCase):
    def test_round_trips_through_disk(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            save = root / "a.srm"
            save.write_bytes(DELTA_BYTES)

            state = manifest.Manifest(root / "manifest.json")
            state.record("sha1-key", save, save)
            state.save()

            reloaded = manifest.Manifest.load(root / "manifest.json")
            entry = reloaded.get("sha1-key")
            self.assertIsNotNone(entry.delta)
            assert entry.delta is not None
            self.assertTrue(entry.delta.matches(save))

    def test_corrupt_manifest_reads_as_empty_rather_than_raising(self) -> None:
        # An unreadable manifest must degrade to "no agreed state", which shows
        # up as a conflict to resolve -- never as a silent overwrite.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("{ this is not json", encoding="utf-8")
            self.assertEqual(manifest.Manifest.load(path).entries, {})

    def test_apple_reference_date_converts_to_unix(self) -> None:
        # Delta's modifiedDate is seconds since 2001-01-01, not 1970-01-01.
        self.assertEqual(manifest.apple_timestamp_to_unix(0.0), 978307200.0)


class BaselineTests(unittest.TestCase):
    """Two identical files are an agreement, and it has to be written down.

    `decide` returns NOTHING for two reasons. "Unchanged on both sides" is read
    *from* the agreed state; "both sides already identical" is reached because
    there is none -- and nothing used to record one, so the sync confirmed the
    two sides matched and then forgot. The next genuine one-sided change then
    took the first-sync branch again, found two files that no longer matched,
    and reported a conflict with nothing conflicting in it.

    This applies to RetroArch exactly as it does to a standalone emulator; the
    branch is in the shared `decide`.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "GameSave-abc-gameSave"
        self.retro = self.root / "Game.srm"
        self.delta.write_bytes(DELTA_BYTES)
        self.retro.write_bytes(DELTA_BYTES)
        self.state = manifest.Manifest(self.root / "manifest.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def entry(self):
        from delta_retroarch_synchronizer import inspect as inspect_module

        return inspect_module.GameEntry(
            identifier="abc",
            name="Game",
            delta_type="",
            system=None,
            rom_path=None,
            save_path=self.delta,
            extra_paths={},
        )

    def test_identical_sides_are_recorded_as_agreed(self) -> None:
        entry = self.entry()
        self.assertTrue(self.state.get("abc").agreement().empty)

        sync.baseline_if_agreed(self.state, entry, sync.Action.NOTHING, self.retro)

        agreed = self.state.get("abc").agreement()
        self.assertFalse(agreed.empty)
        self.assertTrue(agreed.delta.matches(self.delta))
        self.assertTrue(agreed.desktop.matches(self.retro))

    def test_the_next_one_sided_change_is_a_pull_not_a_conflict(self) -> None:
        entry = self.entry()
        sync.baseline_if_agreed(self.state, entry, sync.Action.NOTHING, self.retro)

        self.delta.write_bytes(RETRO_BYTES)
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)

        self.assertIs(action, sync.Action.PULL)

    def test_nothing_is_recorded_for_an_action_that_still_has_to_happen(self) -> None:
        """Only NOTHING means the two sides already agree.

        Baselining a PULL or a CONFLICT here would write an agreement over a
        difference that has not been resolved -- which is the silent overwrite
        this whole design exists to avoid, arriving one run later.
        """
        entry = self.entry()
        for action in (sync.Action.PULL, sync.Action.PUSH, sync.Action.CONFLICT):
            with self.subTest(action=action):
                sync.baseline_if_agreed(self.state, entry, action, self.retro)
                self.assertTrue(self.state.get("abc").agreement().empty)

    def test_re_recording_an_unchanged_pair_writes_the_same_facts(self) -> None:
        """NOTHING means both files still match what was agreed, so this is a no-op.

        It is done on every NOTHING rather than only the empty case because that
        is what rewrites a manifest entry left in an older shape. Safe for the
        reason stated: the action being NOTHING is itself the proof that neither
        side has moved away from what is recorded.
        """
        entry = self.entry()
        self.state.record("abc", self.delta, self.retro)
        before = self.state.get("abc").agreement()

        sync.baseline_if_agreed(self.state, entry, sync.Action.NOTHING, self.retro)

        self.assertEqual(self.state.get("abc").agreement(), before)


if __name__ == "__main__":
    unittest.main()
