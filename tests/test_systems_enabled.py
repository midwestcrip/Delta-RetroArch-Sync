"""Tests guarding which systems the sync pass is allowed to write.

This set is the safety gate. A system reaching it before its save format has
been checked against a real file does not fail loudly -- it silently writes a
save the emulator cannot read, over one the player cared about.
"""

from __future__ import annotations

import pytest

from delta_retroarch_synchronizer import systems


def test_enabled_systems_are_all_raw_compatible():
    """Nothing needing conversion may be enabled until the conversion exists."""
    for key in systems.ENABLED_SYSTEMS:
        assert systems.SYSTEMS[key].raw_compatible, f"{key} needs conversion first"


def test_enabled_systems_carry_no_conversion_note():
    """A conversion note is the marker for 'we know this is not a plain copy'."""
    for key in systems.ENABLED_SYSTEMS:
        assert not systems.SYSTEMS[key].conversion_note, key


@pytest.mark.parametrize("key", ["n64", "ds"])
def test_systems_needing_conversion_stay_blocked(key):
    """N64 packs into a combined .srm; DS may carry a DeSmuME footer."""
    assert key not in systems.ENABLED_SYSTEMS
    assert not systems.SYSTEMS[key].raw_compatible
    assert systems.SYSTEMS[key].conversion_note


def test_nes_is_a_plain_copy_of_battery_ram():
    """Enabled 2026-09-06 against a real Kirby's Adventure save.

    8192 bytes exactly -- the MMC3 mapper's battery-backed PRG-RAM -- raw, no
    header or footer. Super Mario Bros. could never have verified this: that
    cartridge has no SRAM, so Delta stores no save file for it at all.
    """
    nes = systems.SYSTEMS["nes"]

    assert "nes" in systems.ENABLED_SYSTEMS
    assert nes.raw_compatible
    assert nes.extra_files == ()


def test_every_enabled_system_can_actually_be_played():
    """An enabled system with no known core would sync saves nothing can load."""
    for key in systems.ENABLED_SYSTEMS:
        system = systems.SYSTEMS[key]
        assert system.retroarch_cores, key
        assert system.rom_exts, key
        assert system.retroarch_db_name, key


def test_the_enabled_set_is_exactly_what_was_verified():
    """Deliberately exact: widening this set is a decision, not a side effect."""
    assert systems.ENABLED_SYSTEMS == frozenset({"gba", "snes", "gbc", "nes"})


def test_gbc_carries_a_clock_file_that_is_not_synced():
    """Delta pairs a 4-byte RTC timestamp with the save; we move only the save.

    extra_files is read by the inspector for reporting and by nothing else. If
    that ever changes, RTC handling must first be checked against what
    RetroArch's Gambatte and mGBA cores actually expect.
    """
    assert systems.SYSTEMS["gbc"].extra_files == ("gameTimeSave",)


def test_snes_is_a_plain_copy_with_the_same_extension():
    """The reason SNES needed no conversion work at all."""
    snes = systems.SYSTEMS["snes"]
    assert snes.delta_save_ext == "srm"
    assert snes.retroarch_save_ext == "srm"
    assert snes.extra_files == ()


def test_missing_core_advice_names_the_core_and_the_menu_path():
    """"No core installed for Super Nintendo" is not a task anyone can act on.

    Both naive-user tests found messages that stated a fact and left the reader
    to work out what to do. This one has to name the core and say where the
    button is.
    """
    advice = systems.missing_core_advice(systems.SYSTEMS["snes"])

    assert "Snes9x" in advice
    assert "Download a Core" in advice
    # The trap that would otherwise waste an evening: year-suffixed forks and
    # bsnes variants carry different display names and are never matched.
    assert "suffixed" in advice


def test_missing_core_advice_leads_with_the_preferred_core():
    """The first line must be the one to act on, not a list to choose from."""
    advice = systems.missing_core_advice(systems.SYSTEMS["gbc"]).splitlines()

    assert "Gambatte" in advice[1]
    assert "SameBoy" not in advice[1]
    assert "SameBoy" in advice[-1]


def test_missing_core_advice_survives_a_system_with_no_known_cores():
    bare = systems.System(
        key="x", name="Nothing", delta_type="t", delta_core="c",
        delta_save_ext="sav", retroarch_save_ext="srm",
    )

    assert systems.missing_core_advice(bare) == "no core installed for Nothing."
