"""Tests for the launcher's saved settings."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import config as config_module  # noqa: E402

DELTA_FOLDER = Path("C:/Data/Dropbox/Delta Emulator")
RETROARCH_EXE = Path("C:/Games/RetroArch/retroarch.exe")


class ConfigRoundTripTests(unittest.TestCase):
    def test_paths_and_options_survive_a_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = config_module.Config(
                delta_folder=DELTA_FOLDER,
                retroarch_exe=RETROARCH_EXE,
                push_enabled=True,
                sync_roms=False,
                sync_cheats=True,
            )
            config_module.save(original, path)
            loaded = config_module.load(path)

            self.assertEqual(loaded.delta_folder, DELTA_FOLDER)
            self.assertEqual(loaded.retroarch_exe, RETROARCH_EXE)
            self.assertTrue(loaded.push_enabled)
            self.assertFalse(loaded.sync_roms)
            self.assertTrue(loaded.sync_cheats)

    def test_pushing_defaults_to_off(self) -> None:
        # Pushing is the only part that writes into Delta's folder, so it must
        # never become enabled by accident -- including via a config file that
        # simply does not mention it.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[paths]\n", encoding="utf-8")
            self.assertFalse(config_module.load(path).push_enabled)

    def test_missing_config_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            loaded = config_module.load(Path(tmp) / "nope.toml")
            self.assertIsNone(loaded.delta_folder)
            self.assertFalse(loaded.push_enabled)

    def test_unset_paths_are_written_as_comments(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_module.save(config_module.Config(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# retroarch_exe", text)
            # A commented-out path must not read back as a real one.
            self.assertIsNone(config_module.load(path).retroarch_exe)

    def test_windows_paths_are_written_with_forward_slashes(self) -> None:
        # TOML treats a backslash as an escape inside a basic string, so a raw
        # Windows path would either fail to parse or silently lose characters.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            # Note "C:/A", not Path("C:") / "A" -- the latter is drive-relative
            # and produces "C:A", which is a different path entirely.
            original = Path("C:/A/B/C")
            config_module.save(
                config_module.Config(delta_folder=original), path
            )
            self.assertIn("C:/A/B/C", path.read_text(encoding="utf-8"))
            self.assertEqual(config_module.load(path).delta_folder, original)


if __name__ == "__main__":
    unittest.main()
