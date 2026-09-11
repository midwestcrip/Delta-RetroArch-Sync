"""Tests for placing a save into a standalone emulator instead of RetroArch.

The thing being guarded here is not "does the path come out right". It is that
this module never writes on the strength of a documented format: three of the
rows in the table are blocked outright, and for the rest it is the bytes of the
user's own existing save that clear the write, not the table.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from delta_retroarch_synchronizer import emulators


# --- the size names the N64 file -------------------------------------------


@pytest.mark.parametrize(
    ("size", "extension"),
    [
        (512, "eep"),      # 4 Kbit EEPROM -- Super Mario 64
        (2048, "eep"),     # 16 Kbit EEPROM -- Yoshi's Story
        (32768, "sra"),    # Ocarina of Time
        (131072, "fla"),   # Majora's Mask
    ],
)
def test_n64_extension_comes_from_the_save_size(size, extension):
    """The whole reason standalone N64 needs no conversion."""
    mupen = emulators.EMULATORS["mupen64plus"]

    assert mupen.extension_for("n64", size) == extension


def test_a_size_that_is_not_an_n64_storage_type_is_refused():
    """Rather than picking a file at random to write it into."""
    mupen = emulators.EMULATORS["mupen64plus"]

    with pytest.raises(ValueError, match="four N64 storage sizes"):
        mupen.extension_for("n64", 1234)


def test_every_other_system_ignores_the_size():
    mgba = emulators.EMULATORS["mgba"]

    assert mgba.extension_for("gba", 131072) == "sav"
    assert mgba.extension_for("gba", 32768) == "sav"


# --- the blocked rows ------------------------------------------------------


def test_nestopia_is_blocked_until_a_real_file_is_measured():
    """Documentation says Windows writes raw. That is not the standard."""
    reason = emulators.blocked_reason(emulators.EMULATORS["nestopia"], "nes")

    assert reason is not None
    assert "compress" in reason


def test_desmume_is_blocked_because_delta_s_dsv_is_not_a_dsv():
    reason = emulators.blocked_reason(emulators.EMULATORS["desmume"], "ds")

    assert reason is not None
    assert "footer" in reason


def test_project64_is_blocked_on_byte_order():
    reason = emulators.blocked_reason(emulators.EMULATORS["project64"], "n64")

    assert reason is not None
    assert "byte-swapped" in reason


def test_mupen64plus_is_not_blocked():
    """The one place dropping RetroArch removes work rather than adding it."""
    assert emulators.blocked_reason(emulators.EMULATORS["mupen64plus"], "n64") is None


def test_an_emulator_that_does_not_run_a_system_says_so():
    reason = emulators.blocked_reason(emulators.EMULATORS["snes9x"], "n64")

    assert reason == "Snes9x does not run this system"


def test_handling_finds_every_emulator_for_a_system():
    keys = {e.key for e in emulators.handling("gbc")}

    assert keys == {"mgba", "vbam", "sameboy", "bgb"}


# --- reading an emulator's own config --------------------------------------


def test_a_setting_is_read_regardless_of_section(tmp_path):
    """These files are INI-shaped but not consistently, so sections are ignored."""
    config = tmp_path / "config.ini"
    config.write_text(
        "[ports.qt]\n"
        "someOther = 1\n"
        "savegamePath = C:/Saves/GBA\n",
        encoding="utf-8",
    )

    assert emulators.read_setting(config, "savegamePath") == "C:/Saves/GBA"


def test_quotes_and_spacing_are_stripped(tmp_path):
    config = tmp_path / "vbam.ini"
    config.write_text('batteryDir   =   "D:/Battery"  \n', encoding="utf-8")

    assert emulators.read_setting(config, "batteryDir") == "D:/Battery"


def test_a_key_with_a_space_in_it_is_matched(tmp_path):
    """Project64 writes 'Save Directory', which most INI readers would mangle."""
    config = tmp_path / "Project64.cfg"
    config.write_text("Save Directory=Save\\\n", encoding="utf-8")

    assert emulators.read_setting(config, "Save Directory") == "Save\\"


def test_comments_are_not_settings(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("# savegamePath = C:/Wrong\n; savegamePath = C:/Also\n", encoding="utf-8")

    assert emulators.read_setting(config, "savegamePath") == ""


def test_a_missing_file_is_not_an_error(tmp_path):
    assert emulators.read_setting(tmp_path / "nope.ini", "anything") == ""


# --- deciding where the save goes ------------------------------------------


def _installed(tmp_path, key="mgba"):
    install = tmp_path / "emu"
    install.mkdir(exist_ok=True)
    executable = install / emulators.EMULATORS[key].executables[0]
    executable.write_bytes(b"")
    return emulators.Installed(emulators.EMULATORS[key], executable)


def test_the_user_s_own_setting_ends_the_argument(tmp_path):
    chosen = tmp_path / "wherever"

    location = emulators.resolve_save_dir(
        _installed(tmp_path), tmp_path / "roms", override=chosen
    )

    assert location.directory == chosen
    assert location.source == "set in config.toml"


def test_an_existing_save_outranks_the_config(tmp_path):
    """The machine saying where it writes beats us saying where it should."""
    installed = _installed(tmp_path)
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {tmp_path / 'from-config'}\n", encoding="utf-8"
    )
    (tmp_path / "from-config").mkdir()
    actual = tmp_path / "actually-here"
    actual.mkdir()
    existing = actual / "Pokemon - Fire Red.sav"
    existing.write_bytes(b"")

    location = emulators.resolve_save_dir(
        installed, tmp_path / "roms", observed=existing
    )

    assert location.directory == actual
    assert "already is" in location.source


def test_the_config_is_used_when_it_names_a_real_folder(tmp_path):
    installed = _installed(tmp_path)
    named = tmp_path / "from-config"
    named.mkdir()
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {named}\n", encoding="utf-8"
    )

    location = emulators.resolve_save_dir(installed, tmp_path / "roms")

    assert location.directory == named
    assert location.source == "config.ini says so"


def test_a_config_folder_that_does_not_exist_is_ignored(tmp_path):
    """A stale setting must not be able to send a save into a new folder."""
    installed = _installed(tmp_path)
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {tmp_path / 'deleted-last-year'}\n", encoding="utf-8"
    )
    roms = tmp_path / "roms"
    roms.mkdir()

    location = emulators.resolve_save_dir(installed, roms)

    assert location.directory == roms


def test_a_relative_config_path_is_relative_to_the_install(tmp_path):
    installed = _installed(tmp_path, "project64")
    (installed.install_dir / "Save").mkdir()
    (installed.install_dir / "Project64.cfg").write_text(
        "Save Directory=Save\n", encoding="utf-8"
    )

    location = emulators.resolve_save_dir(installed, None)

    assert location.directory == installed.install_dir / "Save"


def test_beside_the_rom_is_the_fallback(tmp_path):
    roms = tmp_path / "roms"
    roms.mkdir()

    location = emulators.resolve_save_dir(_installed(tmp_path), roms)

    assert location.directory == roms
    assert "beside the ROM" in location.source


def test_an_emulator_with_its_own_folder_does_not_fall_back_to_the_rom(tmp_path):
    """Snes9x does not write beside the ROM, so guessing that it does is wrong."""
    roms = tmp_path / "roms"
    roms.mkdir()

    location = emulators.resolve_save_dir(_installed(tmp_path, "snes9x"), roms)

    assert not location.found
    assert "config.toml" in location.source


# --- what actually clears a write ------------------------------------------


def test_a_matching_save_is_cleared():
    assert emulators.check_shape(b"\x00" * 32768, b"\xff" * 32768) is None


def test_a_gzip_save_on_disk_stops_the_write():
    """The Nestopia trap, caught by measuring instead of by the table."""
    existing = emulators.GZIP_MAGIC + b"\x08\x00" + b"\x00" * 100
    incoming = b"\xff" * len(existing)

    reason = emulators.check_shape(existing, incoming)

    assert reason is not None
    assert "gzip" in reason


def test_a_desmume_footer_on_disk_stops_the_write():
    existing = b"\x00" * 1024 + emulators.DESMUME_MARKER
    incoming = b"\xff" * len(existing)

    reason = emulators.check_shape(existing, incoming)

    assert reason is not None
    assert "footer" in reason


def test_a_different_size_stops_the_write():
    reason = emulators.check_shape(b"\x00" * 8192, b"\x00" * 32768)

    assert reason is not None
    assert "8,192" in reason and "32,768" in reason


def test_the_size_check_reads_both_ways():
    """Shorter on disk and longer on disk are both mismatches."""
    assert emulators.check_shape(b"\x00" * 32768, b"\x00" * 8192) is not None


# --- discovery -------------------------------------------------------------


@pytest.fixture
def no_registry(monkeypatch):
    """An empty registry, so these tests say the same thing on every machine.

    Without this they pass here only because no standalone emulator happens to
    be installed on this machine -- which is a property of the machine, not of
    the code, and would flip the day one is.
    """
    from delta_retroarch_synchronizer import discovery

    monkeypatch.setattr(discovery, "install_dirs", lambda *args, **kwargs: [])
    # And no folder to scan. `find_installed` falls back to walking wherever
    # emulators are kept, which on this machine finds the real Mupen64Plus --
    # a property of the machine, not of the code.
    monkeypatch.setattr(
        emulators, "search_roots", lambda extra=(): [Path(d) for d in extra]
    )


def test_an_emulator_is_found_in_a_folder_the_user_named(tmp_path, no_registry):
    install = tmp_path / "portable"
    install.mkdir()
    (install / "mGBA.exe").write_bytes(b"")

    found = emulators.find_executable(emulators.EMULATORS["mgba"], (install,))

    assert found == install / "mGBA.exe"


def test_nothing_is_found_when_nothing_is_there(tmp_path, no_registry):
    assert emulators.find_executable(emulators.EMULATORS["mgba"], (tmp_path,)) is None


def test_find_installed_reports_each_emulator_once(tmp_path, no_registry):
    """Two emulators kept in one folder, which is how people keep them."""
    (tmp_path / "mGBA.exe").write_bytes(b"")
    (tmp_path / "snes9x.exe").write_bytes(b"")

    found = {installed.emulator.key for installed in emulators.find_installed((tmp_path,))}

    assert found == {"mgba", "snes9x"}


def test_an_unset_environment_variable_yields_no_path(monkeypatch):
    """A blank %APPDATA% must not produce a path rooted at the drive."""
    monkeypatch.delenv("APPDATA", raising=False)

    assert emulators._expand("{appdata}/Mupen64Plus/save", None) is None
