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


def test_nes_stays_blocked_until_a_real_save_exists():
    """Modelled and copyable, but no real NES save has been inspected.

    Being raw_compatible is not on its own a reason to enable something: the
    rule is a real save file, checked. Note that Super Mario Bros. cannot
    supply one -- that cartridge has no SRAM -- so verifying NES needs a game
    with a battery.
    """
    assert "nes" not in systems.ENABLED_SYSTEMS


def test_every_enabled_system_can_actually_be_played():
    """An enabled system with no known core would sync saves nothing can load."""
    for key in systems.ENABLED_SYSTEMS:
        system = systems.SYSTEMS[key]
        assert system.retroarch_cores, key
        assert system.rom_exts, key
        assert system.retroarch_db_name, key


def test_the_enabled_set_is_exactly_what_was_verified():
    """Deliberately exact: widening this set is a decision, not a side effect."""
    assert systems.ENABLED_SYSTEMS == frozenset({"gba", "snes", "gbc"})


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
