"""Tests against a synthetic Delta folder.

The fixtures below were corrected against a real Delta sync on 2026-09-05, which
disagreed with Harmony's source in two ways that matter: `files` arrives as a
list of file objects rather than an {identifier: sha1} map, and non-JSON-native
Core Data attributes -- `type`, `artworkURL` -- are base64 NSKeyedArchiver
plists rather than plain strings.

Keep these shapes faithful to real data. They are the only place the wire format
is pinned down, and a fixture that drifts from reality tests nothing.
"""

from __future__ import annotations

import base64
import json
import plistlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_sync import harmony, inspect, naming, systems  # noqa: E402

FIRERED_SHA1 = "0" * 40
UNSUPPORTED_SHA1 = "1" * 40


def archived(value: str) -> str:
    """Encode a string the way Delta stores GameType: NSKeyedArchiver + base64.

    Matches the real payload byte-for-byte in structure: a $objects table whose
    slot 0 is "$null", with $top.root pointing at the real value.
    """
    plist = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", value],
    }
    blob = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    return base64.b64encode(blob).decode("ascii")


def game_files(sha1: str) -> list[dict]:
    """The list-of-objects `files` shape real records use."""
    return [
        {
            "identifier": "game",
            "sha1Hash": sha1,
            "size": 16777216,
            "remoteIdentifier": f"/delta emulator/game-{sha1}-game",
            "versionIdentifier": "65ac6a11de26ecb175c93",
        }
    ]


def build_delta_folder(root: Path) -> Path:
    folder = root / "Delta Emulator"
    folder.mkdir()

    def record(name: str, payload: dict) -> None:
        (folder / name).write_text(json.dumps(payload), encoding="utf-8")

    record(
        f"Game-{FIRERED_SHA1}",
        {
            "type": "Game",
            "identifier": FIRERED_SHA1,
            "record": {
                "name": "Pokemon FireRed",
                "filename": f"{FIRERED_SHA1}.gba",
                "type": archived("com.rileytestut.delta.game.gba"),
            },
            "files": game_files(FIRERED_SHA1),
            "relationships": {},
        },
    )
    record(
        f"GameSave-{FIRERED_SHA1}",
        {
            "type": "GameSave",
            "identifier": FIRERED_SHA1,
            "record": {"sha1": "abc"},
            "files": [{"identifier": "gameSave", "sha1Hash": "abc", "size": 131072}],
            "relationships": {"game": {"type": "Game", "identifier": FIRERED_SHA1}},
        },
    )
    record(
        "Cheat-11111111-2222-3333-4444-555555555555",
        {
            "type": "Cheat",
            "identifier": "11111111-2222-3333-4444-555555555555",
            "record": {
                "name": "Walk Through Walls",
                "code": "509197D3542975F4",
                "type": "actionReplay",
            },
            "relationships": {"game": {"type": "Game", "identifier": FIRERED_SHA1}},
        },
    )
    # An N64 game, to prove unsupported systems are reported but not enabled.
    record(
        f"Game-{UNSUPPORTED_SHA1}",
        {
            "type": "Game",
            "identifier": UNSUPPORTED_SHA1,
            "record": {
                "name": "Ocarina of Time",
                "filename": f"{UNSUPPORTED_SHA1}.z64",
                "type": archived("com.rileytestut.delta.game.n64"),
            },
            "files": game_files(UNSUPPORTED_SHA1),
            "relationships": {},
        },
    )
    # A melonDS BIOS pseudo-game, which must be skipped entirely.
    record(
        "Game-com.rileytestut.MelonDSDeltaCore.BIOS",
        {
            "type": "Game",
            "identifier": "com.rileytestut.MelonDSDeltaCore.BIOS",
            "record": {
                "name": "Home Screen",
                "type": archived("com.rileytestut.delta.game.ds"),
            },
            "files": [{"identifier": "bios7", "sha1Hash": "x"}],
            "relationships": {},
        },
    )

    # Attached data files sit alongside the records and are not JSON.
    (folder / f"Game-{FIRERED_SHA1}-game").write_bytes(b"ROMDATA")
    (folder / f"GameSave-{FIRERED_SHA1}-gameSave").write_bytes(b"S" * 131072)
    (folder / f"Game-{UNSUPPORTED_SHA1}-game").write_bytes(b"N64DATA")
    return folder


class HarmonyFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = build_delta_folder(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_non_json_attachments_are_not_parsed_as_records(self) -> None:
        types = sorted({r.type for r in harmony.iter_records(self.folder)})
        self.assertEqual(types, ["Cheat", "Game", "GameSave"])

    def test_bios_pseudo_games_are_skipped(self) -> None:
        names = {e.name for e in inspect.collect_games(self.folder)}
        self.assertNotIn("Home Screen", names)

    def test_save_is_joined_to_game_by_shared_sha1(self) -> None:
        entries = {e.name: e for e in inspect.collect_games(self.folder)}
        firered = entries["Pokemon FireRed"]
        self.assertEqual(firered.identifier, FIRERED_SHA1)
        self.assertIsNotNone(firered.save_path)
        self.assertIsNotNone(firered.rom_path)
        self.assertEqual(firered.system, systems.SYSTEMS["gba"])
        self.assertTrue(firered.supported)

    def test_n64_is_reported_but_not_enabled(self) -> None:
        entries = {e.name: e for e in inspect.collect_games(self.folder)}
        oot = entries["Ocarina of Time"]
        self.assertEqual(oot.system, systems.SYSTEMS["n64"])
        self.assertFalse(oot.supported)

    def test_cheats_group_by_game_identifier(self) -> None:
        cheats = inspect.collect_cheats(self.folder)
        self.assertIn(FIRERED_SHA1, cheats)
        self.assertEqual(cheats[FIRERED_SHA1][0]["name"], "Walk Through Walls")
        self.assertEqual(cheats[FIRERED_SHA1][0]["code"], "509197D3542975F4")


class RetroArchConfigTests(unittest.TestCase):
    def test_parses_quoted_values_and_resolves_default_dir(self) -> None:
        from delta_retroarch_sync import discovery

        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "retroarch.cfg"
            cfg.write_text(
                '# a comment\n'
                'savefile_directory = "default"\n'
                'sort_savefiles_enable = "true"\n'
                'sort_savefiles_by_content_enable = "false"\n',
                encoding="utf-8",
            )
            settings = discovery.parse_retroarch_config(cfg)
            self.assertTrue(discovery.truthy(settings, "sort_savefiles_enable"))
            self.assertFalse(
                discovery.truthy(settings, "sort_savefiles_by_content_enable")
            )
            resolved = discovery.resolve_retroarch_dir(
                settings, "savefile_directory", cfg, "saves"
            )
            self.assertEqual(resolved, cfg.parent / "saves")


class ArchivedAttributeTests(unittest.TestCase):
    def test_game_type_is_decoded_from_the_archived_blob(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = build_delta_folder(Path(tmp))
            entries = {e.name: e for e in inspect.collect_games(folder)}
            self.assertEqual(
                entries["Pokemon FireRed"].delta_type,
                "com.rileytestut.delta.game.gba",
            )

    def test_plain_strings_pass_through_untouched(self) -> None:
        self.assertEqual(harmony._unwrap("actionReplay"), "actionReplay")

    def test_long_non_base64_strings_are_not_mangled(self) -> None:
        name = "A Very Long Game Name That Is Not Base64 At All!!"
        self.assertEqual(harmony._unwrap(name), name)


class NamingTests(unittest.TestCase):
    def test_colon_becomes_a_dash_rather_than_vanishing(self) -> None:
        # The real first game synced: a colon is illegal in Windows paths.
        self.assertEqual(
            naming.safe_filename("Pokémon: Fire Red Version"),
            "Pokémon - Fire Red Version",
        )

    def test_accents_are_preserved(self) -> None:
        self.assertIn("é", naming.safe_filename("Pokémon"))

    def test_every_illegal_character_is_removed(self) -> None:
        illegal = r'<>"/\|?*'
        cleaned = naming.safe_filename("a" + illegal + "b")
        for bad in illegal:
            self.assertNotIn(bad, cleaned)
        # The surviving text is still there; only the illegal bytes went.
        self.assertEqual(cleaned, "ab")

    def test_control_characters_are_removed(self) -> None:
        self.assertEqual(naming.safe_filename("a\x00b\x1fc\x7f"), "abc")

    def test_trailing_dot_is_dropped_because_windows_drops_it(self) -> None:
        self.assertEqual(naming.safe_filename("Game."), "Game")

    def test_reserved_device_names_are_escaped(self) -> None:
        self.assertEqual(naming.safe_filename("CON"), "CON_")

    def test_empty_result_falls_back(self) -> None:
        self.assertEqual(naming.safe_filename("///"), "untitled")

    def test_rom_and_save_share_a_stem(self) -> None:
        name = "Pokémon: Fire Red Version"
        rom = naming.rom_filename(name, "gba")
        save = naming.save_filename(name, "srm")
        self.assertEqual(rom.rsplit(".", 1)[0], save.rsplit(".", 1)[0])


if __name__ == "__main__":
    unittest.main()
