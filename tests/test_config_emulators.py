"""Tests for the [emulators] section of config.toml.

The round trip is the part that matters. The launcher writes this whole file
back whenever any setting changes, so a section it does not know how to write is
deleted by the next press of Save settings -- silently un-enabling an emulator
somebody had configured by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import config as config_module


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_no_emulator_section_means_none_are_enabled(tmp_path):
    """The whole of the opt-in: found is not the same as enabled."""
    config = config_module.load(write(tmp_path, "[options]\nsync_roms = true\n"))

    assert config.emulators_enabled == ()
    assert config.emulator_paths == {}


def test_enabled_emulators_are_read(tmp_path):
    config = config_module.load(
        write(tmp_path, '[emulators]\nenabled = ["mgba", "mupen64plus"]\n')
    )

    assert config.emulators_enabled == ("mgba", "mupen64plus")


def test_per_emulator_paths_are_read(tmp_path):
    config = config_module.load(
        write(
            tmp_path,
            "[emulators]\n"
            'enabled = ["mgba"]\n'
            "\n"
            "[emulators.mgba]\n"
            'path = "C:/Emulators/mGBA"\n'
            'save_dir = "D:/Saves/GBA"\n',
        )
    )

    assert config.emulator_paths["mgba"] == Path("C:/Emulators/mGBA")
    assert config.emulator_save_dirs["mgba"] == Path("D:/Saves/GBA")


def test_a_malformed_section_is_ignored_rather_than_fatal(tmp_path):
    """This file is hand-edited; a typo should not stop the program starting."""
    config = config_module.load(
        write(tmp_path, '[emulators]\nenabled = "mgba"\n')
    )

    assert config.emulators_enabled == ()


def test_the_section_survives_being_saved(tmp_path):
    """The regression this file exists for."""
    original = config_module.Config(
        emulators_enabled=("mgba", "mupen64plus"),
        emulator_paths={"mgba": Path("C:/Emulators/mGBA")},
        emulator_save_dirs={"mupen64plus": Path("D:/N64 saves")},
    )
    path = tmp_path / "config.toml"

    config_module.save(original, path)
    reloaded = config_module.load(path)

    assert reloaded.emulators_enabled == ("mgba", "mupen64plus")
    assert reloaded.emulator_paths["mgba"] == Path("C:/Emulators/mGBA")
    assert reloaded.emulator_save_dirs["mupen64plus"] == Path("D:/N64 saves")


def test_saving_a_config_with_no_emulators_writes_no_section(tmp_path):
    """So an existing config.toml is not grown a section nobody asked for."""
    path = tmp_path / "config.toml"

    config_module.save(config_module.Config(), path)

    assert "[emulators]" not in path.read_text(encoding="utf-8")


def test_the_rest_of_the_file_still_round_trips(tmp_path):
    """The new section must not have disturbed the settings already there."""
    original = config_module.Config(
        delta_folder=Path("C:/Dropbox/Delta Emulator"),
        push_enabled=True,
        sync_roms=False,
        emulators_enabled=("mgba",),
    )
    path = tmp_path / "config.toml"

    config_module.save(original, path)
    reloaded = config_module.load(path)

    assert reloaded.delta_folder == Path("C:/Dropbox/Delta Emulator")
    assert reloaded.push_enabled is True
    assert reloaded.sync_roms is False
    assert reloaded.emulators_enabled == ("mgba",)
