"""Tests for Delta -> RetroArch cheat conversion.

The codes below are verbatim from libretro-database's own
`Pokemon - FireRed Version (USA, Europe) (Rev 1).cht`. Checking our output
against the format libretro actually ships is the point: a `.cht` file that
merely looks plausible will load without complaint and do nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import cheats  # noqa: E402

#: (description, delta code as typed, delta cheat type, expected .cht code)
REAL_CODES = [
    (
        "Faster Text Display",
        "00000000 18002C02\n0000E01A 00000000",
        "ActionReplay",
        "00000000+18002C02+0000E01A+00000000",
    ),
    (
        "Ride Bike Over Water",
        "72036E3C 0100\n32036E43 0000",
        "CodeBreaker",
        "72036E3C+0100+32036E43+0000",
    ),
    (
        "Infinite Money",
        "042257BC 000F423F",
        "ActionReplay",
        "042257BC+000F423F",
    ),
    (
        "Wild PKMN Easily Caught",
        "72023D74 9A84\n82023D74 9AC3",
        "CodeBreaker",
        "72023D74+9A84+82023D74+9AC3",
    ),
]


class CodeConversionTests(unittest.TestCase):
    def test_real_codes_convert_to_libretros_own_format(self) -> None:
        for description, delta_code, kind, expected in REAL_CODES:
            with self.subTest(description):
                self.assertEqual(
                    cheats.to_retroarch_code(delta_code, kind), expected
                )

    def test_word_sizes_differ_between_action_replay_and_code_breaker(self) -> None:
        # The same digits regroup differently: 8+8 vs 8+4. Getting this wrong
        # produces a well-formed file full of meaningless codes.
        digits = "042257BC000F423F"
        self.assertEqual(
            cheats.code_words(digits, "ActionReplay"), ["042257BC", "000F423F"]
        )
        self.assertEqual(
            cheats.code_words(digits, "CodeBreaker"),
            ["042257BC", "000F", "423F"],
        )

    def test_unformatted_and_formatted_codes_agree(self) -> None:
        # Delta may store the code with its display formatting or without; the
        # word sizes are what define the boundaries, not the whitespace.
        spaced = "00000000 18002C02 0000E01A 00000000"
        bare = "000000001 8002C020 000E01A0 0000000".replace(" ", "")
        self.assertEqual(
            cheats.to_retroarch_code(spaced, "ActionReplay"),
            cheats.to_retroarch_code(bare, "ActionReplay"),
        )

    def test_lowercase_hex_is_normalised(self) -> None:
        self.assertEqual(
            cheats.to_retroarch_code("042257bc 000f423f", "ActionReplay"),
            "042257BC+000F423F",
        )

    def test_unknown_cheat_type_falls_back_to_eight_digit_words(self) -> None:
        self.assertEqual(
            cheats.to_retroarch_code("042257BC 000F423F", "SomethingNew"),
            "042257BC+000F423F",
        )


class RenderTests(unittest.TestCase):
    def render_one(self) -> str:
        return cheats.render(
            [
                cheats.Cheat("Faster Text Display", "00000000 18002C02", "ActionReplay"),
                cheats.Cheat("Ride Bike Over Water", "72036E3C 0100", "CodeBreaker"),
            ]
        )

    def test_header_counts_the_cheats(self) -> None:
        self.assertTrue(self.render_one().startswith("cheats = 2"))

    def test_each_cheat_has_desc_code_and_enable(self) -> None:
        text = self.render_one()
        for index in (0, 1):
            self.assertIn(f"cheat{index}_desc = ", text)
            self.assertIn(f"cheat{index}_code = ", text)
            self.assertIn(f"cheat{index}_enable = false", text)

    def test_cheats_are_written_disabled(self) -> None:
        # Enabling a cheat changes what a save becomes. A sync must not make
        # that decision on the user's behalf.
        self.assertNotIn("= true", self.render_one())

    def test_quotes_in_a_name_cannot_break_the_parser(self) -> None:
        text = cheats.render([cheats.Cheat('He said "hi"', "042257BC", "ActionReplay")])
        body = [line for line in text.splitlines() if "_desc" in line][0]
        self.assertEqual(body.count('"'), 2)

    def test_newlines_in_a_name_are_flattened(self) -> None:
        text = cheats.render([cheats.Cheat("two\nlines", "042257BC", "ActionReplay")])
        self.assertIn('cheat0_desc = "two lines (ActionReplay)"', text)

    def test_empty_name_still_produces_a_label(self) -> None:
        text = cheats.render([cheats.Cheat("", "042257BC", "ActionReplay")])
        self.assertIn("Cheat 1", text)


class FileTests(unittest.TestCase):
    def test_path_follows_libretro_database_layout(self) -> None:
        path = cheats.cheat_file_path(
            Path("C:/RetroArch/cheats"),
            "Nintendo - Game Boy Advance",
            "Pokémon: Fire Red Version",
        )
        self.assertEqual(path.parent.name, "Nintendo - Game Boy Advance")
        # The colon is illegal in a Windows path and must not survive.
        self.assertNotIn(":", path.name)
        self.assertEqual(path.name, "Pokémon - Fire Red Version.cht")

    def test_write_reports_change_then_no_change(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "Game.cht"
            entries = [cheats.Cheat("A", "042257BC 000F423F", "ActionReplay")]
            self.assertTrue(cheats.write_cheat_file(path, entries))
            # Rewriting an identical file would churn the mtime of something
            # RetroArch reads, for no reason.
            self.assertFalse(cheats.write_cheat_file(path, entries))

    def test_write_leaves_no_partial_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "Game.cht"
            cheats.write_cheat_file(path, [cheats.Cheat("A", "042257BC", "ActionReplay")])
            self.assertEqual(list(path.parent.glob("*.partial")), [])


class DryRunAccuracyTests(unittest.TestCase):
    """A dry run must predict the real run, or it teaches you to ignore it."""

    def test_dry_run_agrees_with_a_real_run_on_an_up_to_date_file(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from delta_retroarch_synchronizer import inspect, sync, systems

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = inspect.GameEntry(
                identifier="a" * 40,
                name="Test Game",
                delta_type="com.rileytestut.delta.game.gba",
                system=systems.SYSTEMS["gba"],
                rom_path=None,
                save_path=None,
                extra_paths={},
            )
            payload = [{"name": "A", "code": "042257BC 000F423F", "type": "ActionReplay"}]

            first = sync.sync_cheats(entry, payload, root)
            assert first is not None
            self.assertTrue(first.applied)

            # Now the file is current: both the dry run and the real run must
            # agree that there is nothing to do.
            dry = sync.sync_cheats(entry, payload, root, dry_run=True)
            real = sync.sync_cheats(entry, payload, root)
            assert dry is not None and real is not None
            self.assertIs(dry.action, sync.Action.NOTHING)
            self.assertIs(real.action, sync.Action.NOTHING)

    def test_dry_run_writes_nothing(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from delta_retroarch_synchronizer import inspect, sync, systems

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = inspect.GameEntry(
                identifier="a" * 40,
                name="Test Game",
                delta_type="com.rileytestut.delta.game.gba",
                system=systems.SYSTEMS["gba"],
                rom_path=None,
                save_path=None,
                extra_paths={},
            )
            payload = [{"name": "A", "code": "042257BC 000F423F", "type": "ActionReplay"}]
            sync.sync_cheats(entry, payload, root, dry_run=True)
            self.assertEqual(list(root.rglob("*.cht")), [])


if __name__ == "__main__":
    unittest.main()
