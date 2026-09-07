"""Renaming a cheat from either side.

A `.cht` file carries no UUID, so the cheat's *name* is the only thing linking
an entry back to its Delta record. That makes renaming the awkward case: the
link breaks, and before this the cheat silently reappeared as an uncreatable
"only exists in RetroArch" one while its Delta twin was rewritten alongside it.

The matcher therefore falls back, strongest signal first: exact name, then the
last agreed name (Delta was renamed), then the code (RetroArch was renamed), and
finally elimination -- but only where exactly one cheat is unpaired on each side,
which is the case where both fields were edited at once and the pairing is forced
rather than chosen. With two or more unpaired apiece it would be a choice, so
they stay unpaired and get reported.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import cheats, delta_writer, sync  # noqa: E402
from delta_retroarch_synchronizer import manifest as manifest_module  # noqa: E402

from test_cheat_editing import (  # noqa: E402
    CHEAT_UUID,
    GAME_SHA1,
    REAL_CODE,
    REAL_RETROARCH_CODE,
    gba_entry,
    seed_cheat,
)

ORIGINAL_NAME = "Faster Text Display"
NEW_NAME = "Much Faster Text"


class RenamePairingTests(unittest.TestCase):
    """The matcher, in isolation."""

    def cheats_and_sources(self, delta_name: str, code: str = REAL_CODE):
        cheat = cheats.Cheat(delta_name, code, "ActionReplay")
        source = {
            "identifier": CHEAT_UUID,
            "name": delta_name,
            "code": code,
            "type": "ActionReplay",
        }
        return [cheat], [source]

    def state_with(self, name: str, code: str = REAL_CODE):
        state = manifest_module.Manifest(Path("unused"))
        state.record_cheat(CHEAT_UUID, cheats.canonical_code(code), name)
        return state

    def test_an_unchanged_name_matches_directly(self) -> None:
        delta, sources = self.cheats_and_sources(ORIGINAL_NAME)
        existing = [cheats.Cheat(ORIGINAL_NAME, REAL_RETROARCH_CODE, "")]

        matched, orphans = sync.match_cheats(
            delta, sources, existing, self.state_with(ORIGINAL_NAME)
        )

        self.assertIs(matched[0], existing[0])
        self.assertEqual(orphans, [])

    def test_a_delta_rename_matches_on_the_last_agreed_name(self) -> None:
        """Delta now says "Much Faster Text"; the `.cht` still says the old one."""
        delta, sources = self.cheats_and_sources(NEW_NAME)
        existing = [cheats.Cheat(ORIGINAL_NAME, REAL_RETROARCH_CODE, "")]

        matched, orphans = sync.match_cheats(
            delta, sources, existing, self.state_with(ORIGINAL_NAME)
        )

        self.assertIs(matched[0], existing[0])
        self.assertEqual(orphans, [])

    def test_a_retroarch_rename_matches_on_the_code(self) -> None:
        """The `.cht` name is new and unknown, but the code is the agreed one."""
        delta, sources = self.cheats_and_sources(ORIGINAL_NAME)
        existing = [cheats.Cheat(NEW_NAME, REAL_RETROARCH_CODE, "")]

        matched, orphans = sync.match_cheats(
            delta, sources, existing, self.state_with(ORIGINAL_NAME)
        )

        self.assertIs(matched[0], existing[0])
        self.assertEqual(orphans, [])

    def test_a_genuinely_new_cheat_stays_unmatched(self) -> None:
        """Still reported as uncreatable, which is the honest answer."""
        delta, sources = self.cheats_and_sources(ORIGINAL_NAME)
        existing = [
            cheats.Cheat(ORIGINAL_NAME, REAL_RETROARCH_CODE, ""),
            cheats.Cheat("Made In RetroArch", "99999999+88888888", ""),
        ]

        matched, orphans = sync.match_cheats(
            delta, sources, existing, self.state_with(ORIGINAL_NAME)
        )

        self.assertIs(matched[0], existing[0])
        self.assertEqual([c.name for c in orphans], ["Made In RetroArch"])

    def test_a_rename_and_a_recode_at_once_pairs_by_elimination(self) -> None:
        """Both fields edited in RetroArch's cheat editor -- an ordinary thing.

        No shared field survives, but with one cheat unpaired on each side the
        pairing is forced rather than chosen, so it is made.
        """
        delta, sources = self.cheats_and_sources(ORIGINAL_NAME)
        existing = [cheats.Cheat(NEW_NAME, "11111111+22222222", "")]

        matched, orphans = sync.match_cheats(
            delta, sources, existing, self.state_with(ORIGINAL_NAME)
        )

        self.assertIs(matched[0], existing[0])
        self.assertEqual(orphans, [])

    def test_elimination_is_refused_when_it_would_be_a_choice(self) -> None:
        """Two unpaired apiece: which goes with which is a guess, so neither."""
        delta = [
            cheats.Cheat("A", "AAAAAAAA AAAAAAAA", "ActionReplay"),
            cheats.Cheat("B", "BBBBBBBB BBBBBBBB", "ActionReplay"),
        ]
        sources = [
            {"identifier": "id-a", "name": "A", "code": delta[0].code, "type": "ActionReplay"},
            {"identifier": "id-b", "name": "B", "code": delta[1].code, "type": "ActionReplay"},
        ]
        existing = [
            cheats.Cheat("Renamed One", "11111111+11111111", ""),
            cheats.Cheat("Renamed Two", "22222222+22222222", ""),
        ]

        matched, orphans = sync.match_cheats(delta, sources, existing, None)

        self.assertEqual(matched, [None, None])
        self.assertEqual(len(orphans), 2)

    def test_one_entry_is_never_claimed_by_two_cheats(self) -> None:
        first = cheats.Cheat("A", "AAAAAAAA AAAAAAAA", "ActionReplay")
        second = cheats.Cheat("B", "AAAAAAAA AAAAAAAA", "ActionReplay")
        sources = [
            {"identifier": "id-a", "name": "A", "code": first.code, "type": "ActionReplay"},
            {"identifier": "id-b", "name": "B", "code": second.code, "type": "ActionReplay"},
        ]
        # Only one `.cht` entry, and both Delta cheats share its code.
        existing = [cheats.Cheat("A", "AAAAAAAA+AAAAAAAA", "")]

        matched, orphans = sync.match_cheats([first, second], sources, existing, None)

        self.assertIs(matched[0], existing[0])
        self.assertIsNone(matched[1])
        self.assertEqual(orphans, [])


class MergedDecisionTests(unittest.TestCase):
    """Combining the code decision with the name decision."""

    def test_agreement_on_both_is_nothing(self) -> None:
        action, _ = sync.merge_cheat_actions(
            (sync.Action.NOTHING, "unchanged on both sides"),
            (sync.Action.NOTHING, "unchanged on both sides"),
        )
        self.assertIs(action, sync.Action.NOTHING)

    def test_a_rename_alone_carries_the_action(self) -> None:
        action, detail = sync.merge_cheat_actions(
            (sync.Action.NOTHING, "unchanged on both sides"),
            (sync.Action.PUSH, "changed in RetroArch"),
        )
        self.assertIs(action, sync.Action.PUSH)
        self.assertIn("renamed", detail)

    def test_both_moving_the_same_way_is_one_action(self) -> None:
        action, detail = sync.merge_cheat_actions(
            (sync.Action.PUSH, "changed in RetroArch"),
            (sync.Action.PUSH, "changed in RetroArch"),
        )
        self.assertIs(action, sync.Action.PUSH)
        self.assertEqual(detail, "changed in RetroArch")

    def test_opposite_directions_are_a_conflict(self) -> None:
        """The code edited on the phone while the name was edited on the desktop.

        There is no resolution that does not discard one of them.
        """
        action, detail = sync.merge_cheat_actions(
            (sync.Action.PULL, "changed in Delta"),
            (sync.Action.PUSH, "changed in RetroArch"),
        )
        self.assertIs(action, sync.Action.CONFLICT)
        self.assertIn("different directions", detail)


class RenameSyncTests(unittest.TestCase):
    """The whole pass, against a synthetic Delta folder."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "Delta Emulator"
        self.cheat_dir = self.root / "cheats"
        self.backups = self.root / "backups"
        seed_cheat(self.delta)
        self.state = manifest_module.Manifest(self.root / "manifest.json")
        self.entry = gba_entry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def record(self) -> dict:
        return json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )

    def cht_path(self) -> Path:
        return cheats.cheat_file_path(
            self.cheat_dir, "Nintendo - Game Boy Advance", self.entry.name
        )

    def payload(self) -> list[dict[str, str]]:
        fields = self.record()["record"]
        return [
            {
                "identifier": CHEAT_UUID,
                "name": fields["name"],
                "code": fields["code"],
                "type": "ActionReplay",
            }
        ]

    def run_pass(self, *, allow_push: bool = True):
        return sync.sync_cheats(
            self.entry,
            self.payload(),
            self.cheat_dir,
            delta_folder=self.delta,
            backup_dir=self.backups,
            state=self.state,
            allow_push=allow_push,
        )

    def rename_in_delta(self, name: str) -> None:
        raw = self.record()
        raw["record"]["name"] = name
        (self.delta / f"Cheat-{CHEAT_UUID}").write_text(
            json.dumps(raw), encoding="utf-8"
        )

    def rename_in_cht(self, name: str) -> None:
        text = self.cht_path().read_text(encoding="utf-8")
        self.cht_path().write_text(
            text.replace(f'"{ORIGINAL_NAME} (ActionReplay)"', f'"{name}"'),
            encoding="utf-8",
        )

    def test_a_rename_in_retroarch_reaches_delta(self) -> None:
        self.run_pass()
        self.rename_in_cht(NEW_NAME)

        outcomes = self.run_pass()

        self.assertTrue(
            any(o.action is sync.Action.PUSH and o.applied for o in outcomes), outcomes
        )
        self.assertEqual(self.record()["record"]["name"], NEW_NAME)
        # The code was not disturbed by a rename.
        self.assertEqual(self.record()["record"]["code"], REAL_CODE)
        # And it settles: a further pass is quiet.
        self.assertEqual(self.run_pass(), [])

    def test_a_rename_in_delta_reaches_retroarch(self) -> None:
        self.run_pass()
        self.rename_in_delta(NEW_NAME)

        self.run_pass()

        self.assertIn(NEW_NAME, self.cht_path().read_text(encoding="utf-8"))
        self.assertEqual(self.run_pass(), [])

    def test_a_renamed_cheat_is_not_reported_as_uncreatable(self) -> None:
        """The bug this feature exists to remove.

        Before, renaming in RetroArch unlinked the cheat: Delta's copy was left
        alone and the renamed entry was announced as one that could never be
        created in Delta -- which was true of the name and false of the cheat.
        """
        self.run_pass()
        self.rename_in_cht(NEW_NAME)

        outcomes = self.run_pass()

        self.assertFalse(
            any("cannot be created" in o.detail for o in outcomes), outcomes
        )

    def test_a_rename_on_both_sides_is_a_conflict(self) -> None:
        self.run_pass()
        self.rename_in_cht("Desktop Name")
        self.rename_in_delta("Phone Name")

        outcomes = self.run_pass()

        self.assertTrue(
            any(o.action is sync.Action.CONFLICT for o in outcomes), outcomes
        )
        # Neither side touched.
        self.assertEqual(self.record()["record"]["name"], "Phone Name")
        self.assertIn("Desktop Name", self.cht_path().read_text(encoding="utf-8"))

    def test_a_rename_and_a_code_edit_together_both_travel(self) -> None:
        self.run_pass()
        text = self.cht_path().read_text(encoding="utf-8")
        text = text.replace(f'"{ORIGINAL_NAME} (ActionReplay)"', f'"{NEW_NAME}"')
        text = text.replace(REAL_RETROARCH_CODE, "11111111+22222222")
        self.cht_path().write_text(text, encoding="utf-8")

        outcomes = self.run_pass()

        self.assertTrue(any(o.applied for o in outcomes), outcomes)
        fields = self.record()["record"]
        self.assertEqual(fields["name"], NEW_NAME)
        self.assertEqual(fields["code"], "11111111 22222222")

    def test_a_new_cheat_in_retroarch_is_still_reported(self) -> None:
        self.run_pass()
        text = self.cht_path().read_text(encoding="utf-8")
        text += (
            'cheat1_desc = "Made In RetroArch"\n'
            'cheat1_code = "99999999+88888888"\n'
            'cheat1_enable = false\n'
        )
        self.cht_path().write_text(text, encoding="utf-8")

        outcomes = self.run_pass()

        self.assertTrue(
            any("cannot be created" in o.detail for o in outcomes), outcomes
        )


class WriterTests(unittest.TestCase):
    def test_push_cheat_writes_a_name_when_given_one(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)

            note = delta_writer.push_cheat(
                delta, CHEAT_UUID, REAL_CODE, root / "backups", new_name=NEW_NAME
            )

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["record"]["name"], NEW_NAME)
            self.assertIn("name", note)

    def test_a_rename_alone_is_still_a_write(self) -> None:
        """The code is unchanged, so the early-out must not fire."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)

            delta_writer.push_cheat(
                delta, CHEAT_UUID, REAL_CODE, root / "backups", new_name=NEW_NAME
            )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["record"]["name"],
                NEW_NAME,
            )

    def test_the_record_hash_is_preserved_through_a_rename(self) -> None:
        from test_cheat_editing import REAL_CHEAT_RECORD

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)

            delta_writer.push_cheat(
                delta, CHEAT_UUID, REAL_CODE, root / "backups", new_name=NEW_NAME
            )

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["sha1Hash"], REAL_CHEAT_RECORD["sha1Hash"])


class ManifestCompatibilityTests(unittest.TestCase):
    def test_a_manifest_written_before_names_were_tracked_still_loads(self) -> None:
        """The old shape stored a bare code string per UUID."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(
                json.dumps(
                    {"version": 1, "games": {}, "cheats": {CHEAT_UUID: "ABCD1234"}}
                ),
                encoding="utf-8",
            )

            loaded = manifest_module.Manifest.load(path)

            self.assertEqual(loaded.cheat_code(CHEAT_UUID), "ABCD1234")
            # No name was ever recorded, which is different from an empty one:
            # a name difference cannot be attributed to either side yet.
            self.assertIsNone(loaded.cheat_name(CHEAT_UUID))

    def test_names_survive_a_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            state = manifest_module.Manifest(path)
            state.record_cheat(CHEAT_UUID, "ABCD1234", ORIGINAL_NAME)
            state.save()

            reloaded = manifest_module.Manifest.load(path)

            self.assertEqual(reloaded.cheat_code(CHEAT_UUID), "ABCD1234")
            self.assertEqual(reloaded.cheat_name(CHEAT_UUID), ORIGINAL_NAME)


if __name__ == "__main__":
    unittest.main()
