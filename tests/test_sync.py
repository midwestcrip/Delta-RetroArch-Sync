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


class AgreementInvariantTests(unittest.TestCase):
    """The one rule the whole agreement design now rests on.

        When both halves are present they describe the same bytes: the save
        Delta holds IS the save inside the desktop file.

    `sync.desktop_still_agreed` uses it directly -- it treats "the extracted
    save matches the Delta half" as proof the desktop save has not moved, which
    is how it sees past a Controller Pak or a clock block. If the invariant is
    ever false, that inference is false, and the sync calls a changed save
    unchanged. Nothing is reported and nothing is written: the two sides quietly
    diverge, which is the worst failure available here.

    Four rounds of review each found a reader or a writer that had drifted from
    it while every test still passed, so it is pinned here rather than left to
    be re-derived at each call site.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = manifest.Manifest(self.root / "manifest.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def file(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_a_pair_describing_two_different_saves_is_not_stored(self) -> None:
        """Refusing costs a conflict next run. Storing it costs the divergence.

        A pair like this can only mean the caller believes two different saves
        agree. Written down, every later run reads it as "unchanged on both
        sides" and the difference is never reported to anyone. Left alone, the
        next run sees a change it cannot explain and says so.
        """
        delta = self.file("delta", b"one" * 100)
        desktop = self.file("desktop", b"two" * 100)

        self.state.record("abc", delta, desktop)

        self.assertTrue(self.state.get("abc").agreement().empty)

    def test_an_earlier_agreement_survives_a_refused_write(self) -> None:
        """The fallback is the previous truth, not an empty entry."""
        delta = self.file("delta", b"one" * 100)
        desktop = self.file("desktop", b"one" * 100)
        self.state.record("abc", delta, desktop)
        before = self.state.get("abc").agreement()

        desktop.write_bytes(b"two" * 100)
        self.state.record("abc", delta, desktop)

        self.assertEqual(self.state.get("abc").agreement(), before)

    def test_one_half_alone_says_nothing_about_the_pair(self) -> None:
        """A target can legitimately know one side and not the other."""
        delta = self.file("delta", b"one" * 100)

        self.state.record("abc", delta, self.root / "not-there")

        agreed = self.state.get("abc").agreement()
        self.assertIsNotNone(agreed.delta)
        self.assertIsNone(agreed.desktop)

    def test_the_data_structure_itself_stays_free_of_the_policy(self) -> None:
        """Old manifests break the invariant, and must stay representable.

        Before the desktop half described the *save* rather than the whole file,
        every converted entry paired a 32 KB Delta save with a 296,960-byte
        .srm. Those are on disk now. Enforcing at the data structure would make
        the program unable to model its own history.
        """
        inconsistent = manifest.Agreement(
            delta=manifest.FileState(sha1="a" * 40, size=32768),
            desktop=manifest.FileState(sha1="b" * 40, size=296960),
        )
        self.assertFalse(inconsistent.consistent)

        stored = manifest.Entry().with_agreement(manifest.RETROARCH, inconsistent)

        self.assertEqual(stored.agreement(), inconsistent)

    def test_every_sync_writer_leaves_a_consistent_pair(self) -> None:
        """Driven through the real paths, not asserted about them.

        Each of these is a way an agreement comes into being. Reading the code
        and concluding they all hold is exactly what produced four incomplete
        fixes, so they are run.
        """
        from delta_retroarch_synchronizer import inspect as inspect_module
        from delta_retroarch_synchronizer import n64, systems

        cases = []

        # A plain-copy system, pulled: the desktop file becomes Delta's save.
        delta = self.file("gba-delta", b"\xa5" * 1024)
        desktop = self.file("gba-desktop", b"\xa5" * 1024)
        cases.append(("plain copy", delta, desktop, None))

        # N64, where the desktop file holds four Controller Paks besides.
        save = bytes(range(256)) * (0x8000 // 256)
        n64_delta = self.file("n64-delta", save)
        srm = bytearray(n64.to_retroarch(save))
        srm[n64.PAKS_START + 900 : n64.PAKS_START + 908] = b"PAKSAVE1"
        n64_desktop = self.file("n64-desktop", bytes(srm))
        n64_entry = inspect_module.GameEntry(
            identifier="n64",
            name="G",
            delta_type=systems.SYSTEMS["n64"].delta_type,
            system=systems.SYSTEMS["n64"],
            rom_path=None,
            save_path=n64_delta,
            extra_paths={},
        )
        cases.append(
            ("n64 with paks", n64_delta, n64_desktop, sync.converted_body(n64_entry))
        )

        # mGBA's Game Boy save, which carries a 48-byte clock block.
        from delta_retroarch_synchronizer import emulators

        layout = emulators.EMULATORS["mgba"].saves["gbc"]
        gb_save = b"\x5c" * 0x8000
        gb_delta = self.file("gb-delta", gb_save)
        gb_desktop = self.file("gb-desktop", gb_save + bytes(48))
        cases.append(
            (
                "mgba clock block",
                gb_delta,
                gb_desktop,
                lambda data: emulators.split_trailer(data, layout)[0],
            )
        )

        for label, delta_path, desktop_path, body in cases:
            with self.subTest(case=label):
                self.state.record(label, delta_path, desktop_path, desktop_body=body)
                agreed = self.state.get(label).agreement()
                self.assertFalse(
                    agreed.empty, f"{label}: refused, so the pair was inconsistent"
                )
                self.assertTrue(agreed.consistent)


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

    def test_a_save_landing_mid_run_is_not_agreed_away(self) -> None:
        """The gap between deciding and recording is a gap a save can land in.

        RetroArch is often running while this does, and the Delta folder is a
        Dropbox folder being synced from the phone throughout. Re-reading the
        file at record time and trusting the result would agree to whatever
        arrived in that gap: the incoming save is written down as *already
        agreed*, every later run reports "unchanged on both sides", and the
        change never arrives anywhere. Nothing is corrupted and nothing is
        reported -- it simply never syncs.
        """
        entry = self.entry()
        self.state.record("abc", self.delta, self.retro)
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)
        self.assertIs(action, sync.Action.NOTHING)

        # Dropbox delivers a save from the phone, right here.
        self.delta.write_bytes(RETRO_BYTES)
        sync.baseline_if_agreed(self.state, entry, action, self.retro)

        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)
        self.assertIs(action, sync.Action.PULL)

    def test_a_desktop_save_landing_mid_run_is_not_agreed_away(self) -> None:
        """The same gap, from the side the emulator writes."""
        entry = self.entry()
        self.state.record("abc", self.delta, self.retro)
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)

        self.retro.write_bytes(RETRO_BYTES)
        sync.baseline_if_agreed(self.state, entry, action, self.retro)

        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)
        self.assertIs(action, sync.Action.PUSH)

    def test_a_first_baseline_is_not_written_over_a_mid_run_change(self) -> None:
        """With no history the check is the one NOTHING actually meant.

        "Both sides already identical" is only worth writing down while it is
        still true. If it is not, these are two different saves with no agreed
        history, which is a conflict -- unhelpful, and the honest answer.
        """
        entry = self.entry()
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)
        self.assertIs(action, sync.Action.NOTHING)

        self.delta.write_bytes(RETRO_BYTES)
        sync.baseline_if_agreed(self.state, entry, action, self.retro)

        self.assertTrue(self.state.get("abc").agreement().empty)
        action, _ = sync.decide(self.state.get("abc"), self.delta, self.retro)
        self.assertIs(action, sync.Action.CONFLICT)

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
