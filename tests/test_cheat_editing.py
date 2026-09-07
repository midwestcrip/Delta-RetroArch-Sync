"""Tests for editing a cheat that already exists in Delta.

The fixture is a real ``Cheat`` record captured from a live Delta sync on
2026-09-05, verbatim. Two things about it are load-bearing and neither was
guessed:

- ``files`` is empty. That is what makes a cheat push simpler than a save push:
  no attached file means no Dropbox revision to read back, so no API call and no
  authorisation.
- ``code`` is stored *formatted*, with the core's display spacing and a newline
  between lines. Writing back a bare or differently grouped code would rewrite
  every cheat's formatting on the phone, so ``to_delta_code`` has to be an exact
  inverse of the conversion out. The round-trip test below pins that against
  this real string.

``type`` is a base64 NSKeyedArchiver plist holding "ActionReplay". It is never
rewritten, and one test exists purely to prove it survives a push untouched.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import cheats, delta_writer, sync  # noqa: E402
from delta_retroarch_synchronizer import inspect as inspect_module  # noqa: E402
from delta_retroarch_synchronizer import manifest as manifest_module  # noqa: E402
from delta_retroarch_synchronizer import systems  # noqa: E402

GAME_SHA1 = "dd5945db9b930750cb39d00c84da8571feebf417"
CHEAT_UUID = "2946F7C3-1C4C-46D6-932F-E3F199A7ED0C"

#: The archived GameType plist, kept verbatim so the "never rewritten" test is
#: checking a real value rather than a placeholder.
ARCHIVED_TYPE = (
    "YnBsaXN0MDDUAQIDBAUGBwpYJHZlcnNpb25ZJGFyY2hpdmVyVCR0b3BYJG9iamVjdHMSAAGGoF8QD05T"
    "S2V5ZWRBcmNoaXZlctEICVRyb290gAGiCwxVJG51bGxcQWN0aW9uUmVwbGF5CBEaJCkyN0lMUVNWXAAA"
    "AAAAAAEBAAAAAAAAAA0AAAAAAAAAAAAAAAAAAABp"
)

#: Verbatim from C:\Data\Dropbox\Delta Emulator, 2026-09-05.
REAL_CHEAT_RECORD = {
    "relationships": {"game": {"type": "Game", "identifier": GAME_SHA1}},
    "files": [],
    "record": {
        "creationDate": 810357820.687162,
        "code": "00000000 18002C02\n0000E01A 00000000",
        "modifiedDate": 810357820.687162,
        "type": ARCHIVED_TYPE,
        "name": "Faster Text Display",
    },
    "identifier": CHEAT_UUID,
    "type": "Cheat",
    "sha1Hash": "14c58bb85d7e84bf677316e95369dd40d6ee35c1",
}

REAL_CODE = REAL_CHEAT_RECORD["record"]["code"]
REAL_RETROARCH_CODE = "00000000+18002C02+0000E01A+00000000"


def seed_cheat(folder: Path, code: str = REAL_CODE) -> Path:
    """Write the real cheat record into a folder, optionally with a new code."""
    folder.mkdir(parents=True, exist_ok=True)
    raw = json.loads(json.dumps(REAL_CHEAT_RECORD))
    raw["record"]["code"] = code
    path = folder / f"Cheat-{CHEAT_UUID}"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class CodeConversionTests(unittest.TestCase):
    def test_the_real_code_survives_a_round_trip_exactly(self) -> None:
        """The one test that justifies writing Delta's formatting back at all.

        Out to RetroArch's shape and back must reproduce the original string
        byte for byte, including the newline. Anything less and a first sync
        would silently reformat cheats on the phone.
        """
        out = cheats.to_retroarch_code(REAL_CODE, "ActionReplay")
        self.assertEqual(out, REAL_RETROARCH_CODE)
        self.assertEqual(cheats.to_delta_code(out, "ActionReplay"), REAL_CODE)

    def test_canonical_form_ignores_formatting(self) -> None:
        self.assertEqual(
            cheats.canonical_code(REAL_CODE),
            cheats.canonical_code(REAL_RETROARCH_CODE),
        )

    def test_canonical_form_is_independent_of_cheat_type(self) -> None:
        """Grouping must not enter the comparison.

        A CodeBreaker code groups as 8+4 and an Action Replay one as 8+8. If the
        canonical form were the grouped string, the same digits read under two
        different assumptions would compare unequal and every sync would report
        a phantom edit.
        """
        digits = "042257BC000F423F"
        self.assertEqual(
            cheats.canonical_code(cheats.to_retroarch_code(digits, "CodeBreaker")),
            cheats.canonical_code(cheats.to_retroarch_code(digits, "ActionReplay")),
        )

    def test_a_single_word_type_puts_one_word_per_line(self) -> None:
        self.assertEqual(
            cheats.to_delta_code("AAAAAAAA+BBBBBBBB", "GameGenie"),
            "AAAAAAAA\nBBBBBBBB",
        )


class DescriptionParsingTests(unittest.TestCase):
    def test_the_rendered_type_suffix_is_stripped(self) -> None:
        self.assertEqual(
            cheats.strip_type_suffix("Faster Text Display (ActionReplay)"),
            "Faster Text Display",
        )

    def test_brackets_that_are_not_a_cheat_type_are_kept(self) -> None:
        """A cheat may legitimately be named with brackets.

        Stripping any trailing bracket would rename "Infinite HP (Japan)" to
        "Infinite HP", which then matches no Delta cheat and reports as an
        uncreatable RetroArch-only cheat -- a confusing failure with no cause
        the user could see.
        """
        self.assertEqual(
            cheats.strip_type_suffix("Infinite HP (Japan)"), "Infinite HP (Japan)"
        )

    def test_render_and_parse_are_inverses(self) -> None:
        original = [
            cheats.Cheat("Faster Text Display", REAL_CODE, "ActionReplay"),
            cheats.Cheat("Walk Through Walls", "509197D3 542975F4", "ActionReplay"),
        ]
        parsed = cheats.parse_cheat_file(cheats.render(original))
        self.assertEqual([c.name for c in parsed], [c.name for c in original])
        self.assertEqual(
            [cheats.canonical_code(c.code) for c in parsed],
            [cheats.canonical_code(c.code) for c in original],
        )

    def test_a_file_retroarch_wrote_itself_still_parses(self) -> None:
        """RetroArch's own cheat editor does not promise this tool's layout."""
        text = (
            'cheats = 1\n'
            'cheat0_desc = "Some Cheat"\n'
            'cheat0_code = "AAAAAAAA+BBBBBBBB"\n'
            'cheat0_enable = false\n'
        )
        parsed = cheats.parse_cheat_file(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "Some Cheat")
        self.assertEqual(parsed[0].code, "AAAAAAAA+BBBBBBBB")


class DecisionTests(unittest.TestCase):
    def test_agreement_needs_no_history(self) -> None:
        """The case that would otherwise break the very first sync.

        Nothing has been recorded yet, so there is no agreed code -- but both
        sides already hold the same one, and calling that a conflict would flag
        every cheat the first time this ran.
        """
        action, _ = sync.decide_cheat(None, "ABCD", "ABCD")
        self.assertIs(action, sync.Action.NOTHING)

    def test_a_difference_with_no_history_is_a_conflict(self) -> None:
        action, _ = sync.decide_cheat(None, "ABCD", "EF01")
        self.assertIs(action, sync.Action.CONFLICT)

    def test_only_retroarch_changed_pushes(self) -> None:
        action, _ = sync.decide_cheat("ABCD", "ABCD", "EF01")
        self.assertIs(action, sync.Action.PUSH)

    def test_only_delta_changed_pulls(self) -> None:
        action, _ = sync.decide_cheat("ABCD", "EF01", "ABCD")
        self.assertIs(action, sync.Action.PULL)

    def test_both_changed_is_a_conflict(self) -> None:
        action, _ = sync.decide_cheat("ABCD", "EF01", "2345")
        self.assertIs(action, sync.Action.CONFLICT)


class PushCheatTests(unittest.TestCase):
    def test_the_code_is_written_and_the_record_hash_preserved(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)

            delta_writer.push_cheat(
                delta, CHEAT_UUID, "11111111 22222222", root / "backups"
            )

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["record"]["code"], "11111111 22222222")
            self.assertEqual(
                written["sha1Hash"], REAL_CHEAT_RECORD["sha1Hash"]
            )

    def test_the_archived_type_and_the_name_are_untouched(self) -> None:
        """Only the code changes.

        ``type`` is an NSKeyedArchiver plist we decode but never re-encode, and
        ``name`` is the key a `.cht` entry is matched by -- rewriting either
        would be a change nobody asked for, and rewriting the name would break
        matching for every subsequent sync.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)

            delta_writer.push_cheat(delta, CHEAT_UUID, "ABCDEF01", root / "backups")

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["record"]["type"], ARCHIVED_TYPE)
            self.assertEqual(written["record"]["name"], "Faster Text Display")

    def test_the_modified_date_moves_forward(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)
            before = REAL_CHEAT_RECORD["record"]["modifiedDate"]

            delta_writer.push_cheat(delta, CHEAT_UUID, "ABCDEF01", root / "backups")

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreater(written["record"]["modifiedDate"], before)

    def test_the_previous_record_is_backed_up(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            seed_cheat(delta)
            backups = root / "backups"

            delta_writer.push_cheat(delta, CHEAT_UUID, "ABCDEF01", backups)

            saved = list(backups.glob(f"Cheat-{CHEAT_UUID}.*.bak"))
            self.assertEqual(len(saved), 1)
            restored = json.loads(saved[0].read_text(encoding="utf-8"))
            self.assertEqual(restored["record"]["code"], REAL_CODE)

    def test_the_file_object_is_kept_rather_than_replaced(self) -> None:
        """The rule that cost days: a rename loses Dropbox property groups.

        Harmony needs those to see a record at all and only Delta can write
        them, so a record replaced by rename becomes permanently invisible. On
        Windows the identity that survives is the creation time, which a
        temp-file-and-rename would reset.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            path = seed_cheat(delta)
            before = path.stat().st_ctime_ns

            delta_writer.push_cheat(delta, CHEAT_UUID, "ABCDEF01", root / "backups")

            self.assertEqual(path.stat().st_ctime_ns, before)

    def test_a_cheat_that_does_not_exist_is_refused(self) -> None:
        """Creating a cheat is impossible, so it must fail loudly rather than
        write a file Harmony would silently ignore."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            delta.mkdir()
            with self.assertRaises(FileNotFoundError):
                delta_writer.push_cheat(
                    delta, "NOT-A-REAL-UUID", "ABCDEF01", root / "backups"
                )
            self.assertEqual(list(delta.iterdir()), [])

    def test_a_record_with_attached_files_is_refused(self) -> None:
        """A cheat with files is not the shape this was written against."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            delta.mkdir(parents=True)
            raw = json.loads(json.dumps(REAL_CHEAT_RECORD))
            raw["files"] = [{"identifier": "something", "sha1Hash": "x"}]
            (delta / f"Cheat-{CHEAT_UUID}").write_text(
                json.dumps(raw), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                delta_writer.push_cheat(
                    delta, CHEAT_UUID, "ABCDEF01", root / "backups"
                )

    def test_a_record_with_no_hash_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = root / "Delta Emulator"
            delta.mkdir(parents=True)
            raw = json.loads(json.dumps(REAL_CHEAT_RECORD))
            del raw["sha1Hash"]
            (delta / f"Cheat-{CHEAT_UUID}").write_text(
                json.dumps(raw), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                delta_writer.push_cheat(
                    delta, CHEAT_UUID, "ABCDEF01", root / "backups"
                )


def gba_entry() -> inspect_module.GameEntry:
    return inspect_module.GameEntry(
        identifier=GAME_SHA1,
        name="Pokemon Fire Red",
        delta_type="com.rileytestut.delta.game.gba",
        system=systems.SYSTEMS["gba"],
        rom_path=None,
        save_path=None,
        extra_paths={},
    )


def delta_payload(code: str = REAL_CODE) -> list[dict[str, str]]:
    return [
        {
            "identifier": CHEAT_UUID,
            "name": "Faster Text Display",
            "code": code,
            "type": "ActionReplay",
        }
    ]


class CheatSyncTests(unittest.TestCase):
    """The whole pass, against a synthetic Delta folder and cheat directory."""

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

    def cht_path(self) -> Path:
        return cheats.cheat_file_path(
            self.cheat_dir,
            "Nintendo - Game Boy Advance",
            self.entry.name,
        )

    def current_payload(self) -> list[dict[str, str]]:
        """What ``collect_cheats`` would return for the folder as it stands.

        Read from disk rather than hardcoded, because a pass that just pushed
        has changed the record -- handing the next pass the old code would be
        feeding it input the real caller could never produce.
        """
        raw = json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )
        return delta_payload(raw["record"]["code"])

    def run_pass(self, *, allow_push: bool = True, dry_run: bool = False):
        return sync.sync_cheats(
            self.entry,
            self.current_payload(),
            self.cheat_dir,
            delta_folder=self.delta,
            backup_dir=self.backups,
            state=self.state,
            allow_push=allow_push,
            dry_run=dry_run,
        )

    def test_first_pass_writes_the_file_and_records_agreement(self) -> None:
        outcomes = self.run_pass()
        self.assertTrue(any(o.applied for o in outcomes))
        self.assertIn(REAL_RETROARCH_CODE, self.cht_path().read_text(encoding="utf-8"))
        self.assertEqual(
            self.state.cheat_code(CHEAT_UUID), cheats.canonical_code(REAL_CODE)
        )

    def test_a_second_pass_does_nothing(self) -> None:
        self.run_pass()
        self.assertEqual(self.run_pass(), [])

    def test_an_edit_in_retroarch_reaches_delta(self) -> None:
        """The feature. Change the `.cht`, and Delta's record follows."""
        self.run_pass()
        edited = self.cht_path().read_text(encoding="utf-8").replace(
            REAL_RETROARCH_CODE, "11111111+22222222"
        )
        self.cht_path().write_text(edited, encoding="utf-8")

        outcomes = self.run_pass()

        self.assertTrue(
            any(o.action is sync.Action.PUSH and o.applied for o in outcomes),
            outcomes,
        )
        written = json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )
        # Written back in Delta's own layout, not RetroArch's.
        self.assertEqual(written["record"]["code"], "11111111 22222222")
        # And now agreed, so a further pass is quiet.
        self.assertEqual(self.run_pass(), [])

    def test_an_edit_is_not_pushed_without_permission(self) -> None:
        self.run_pass()
        edited = self.cht_path().read_text(encoding="utf-8").replace(
            REAL_RETROARCH_CODE, "11111111+22222222"
        )
        self.cht_path().write_text(edited, encoding="utf-8")

        outcomes = self.run_pass(allow_push=False)

        self.assertTrue(any("not pushed" in o.detail for o in outcomes), outcomes)
        written = json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )
        self.assertEqual(written["record"]["code"], REAL_CODE)

    def test_a_dry_run_writes_nothing(self) -> None:
        self.run_pass()
        edited = self.cht_path().read_text(encoding="utf-8").replace(
            REAL_RETROARCH_CODE, "11111111+22222222"
        )
        self.cht_path().write_text(edited, encoding="utf-8")

        self.run_pass(dry_run=True)

        written = json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )
        self.assertEqual(written["record"]["code"], REAL_CODE)
        self.assertEqual(self.cht_path().read_text(encoding="utf-8"), edited)

    def test_both_sides_changed_leaves_both_alone(self) -> None:
        """The case the whole manifest exists for.

        A cheat edited on the phone and in RetroArch since the last sync must
        not be resolved by guessing -- and crucially the `.cht` must not be
        rewritten from Delta, which would destroy the RetroArch edit before the
        user ever saw the conflict.
        """
        self.run_pass()
        edited = self.cht_path().read_text(encoding="utf-8").replace(
            REAL_RETROARCH_CODE, "11111111+22222222"
        )
        self.cht_path().write_text(edited, encoding="utf-8")
        seed_cheat(self.delta, "33333333 44444444")

        outcomes = sync.sync_cheats(
            self.entry,
            delta_payload("33333333 44444444"),
            self.cheat_dir,
            delta_folder=self.delta,
            backup_dir=self.backups,
            state=self.state,
            allow_push=True,
        )

        self.assertTrue(
            any(o.action is sync.Action.CONFLICT for o in outcomes), outcomes
        )
        self.assertEqual(self.cht_path().read_text(encoding="utf-8"), edited)
        written = json.loads(
            (self.delta / f"Cheat-{CHEAT_UUID}").read_text(encoding="utf-8")
        )
        self.assertEqual(written["record"]["code"], "33333333 44444444")

    def test_an_edit_in_delta_still_reaches_retroarch(self) -> None:
        self.run_pass()
        seed_cheat(self.delta, "33333333 44444444")

        sync.sync_cheats(
            self.entry,
            delta_payload("33333333 44444444"),
            self.cheat_dir,
            delta_folder=self.delta,
            backup_dir=self.backups,
            state=self.state,
            allow_push=True,
        )

        self.assertIn(
            "33333333+44444444", self.cht_path().read_text(encoding="utf-8")
        )

    def test_a_cheat_only_in_retroarch_is_reported_as_uncreatable(self) -> None:
        """Not an error, and not silence either.

        Silently ignoring it is exactly what makes someone believe a cheat they
        made in RetroArch synced to their phone.
        """
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
            any(
                o.action is sync.Action.SKIPPED and "cannot be created" in o.detail
                for o in outcomes
            ),
            outcomes,
        )


if __name__ == "__main__":
    unittest.main()
