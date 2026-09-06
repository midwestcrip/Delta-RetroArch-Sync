"""Tests against a synthetic Delta folder.

The real folder does not exist yet on any dev machine, so these fixtures encode
the layout read out of Harmony's source (LocalRecord.encode, DropboxService).
When real data is available, the first job is to diff it against these fixtures
and fix whichever one is wrong.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_sync import harmony, inspect, systems  # noqa: E402

FIRERED_SHA1 = "0" * 40
UNSUPPORTED_SHA1 = "1" * 40


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
                "type": "com.rileytestut.delta.game.gba",
            },
            "files": {"game": "abc", "artwork": "def"},
            "relationships": {},
        },
    )
    record(
        f"GameSave-{FIRERED_SHA1}",
        {
            "type": "GameSave",
            "identifier": FIRERED_SHA1,
            "record": {"sha1": "abc"},
            "files": {"gameSave": "abc"},
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
                "type": "com.rileytestut.delta.game.n64",
            },
            "files": {"game": "ghi"},
            "relationships": {},
        },
    )
    # A melonDS BIOS pseudo-game, which must be skipped entirely.
    record(
        "Game-com.rileytestut.MelonDSDeltaCore.BIOS",
        {
            "type": "Game",
            "identifier": "com.rileytestut.MelonDSDeltaCore.BIOS",
            "record": {"name": "Home Screen", "type": "com.rileytestut.delta.game.ds"},
            "files": {"bios7": "x", "bios9": "y"},
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


if __name__ == "__main__":
    unittest.main()
