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


@pytest.mark.parametrize("key", ["nes", "gbc"])
def test_unverified_raw_systems_stay_blocked(key):
    """Modelled and copyable, but no real save has been inspected yet.

    Being raw_compatible is not on its own a reason to enable something: the
    rule is a real save file, checked. Delete the case when that happens.
    """
    assert key not in systems.ENABLED_SYSTEMS


def test_every_enabled_system_can_actually_be_played():
    """An enabled system with no known core would sync saves nothing can load."""
    for key in systems.ENABLED_SYSTEMS:
        system = systems.SYSTEMS[key]
        assert system.retroarch_cores, key
        assert system.rom_exts, key
        assert system.retroarch_db_name, key


def test_gba_and_snes_are_the_enabled_set():
    """Deliberately exact: widening this set is a decision, not a side effect."""
    assert systems.ENABLED_SYSTEMS == frozenset({"gba", "snes"})


def test_snes_is_a_plain_copy_with_the_same_extension():
    """The reason SNES needed no conversion work at all."""
    snes = systems.SYSTEMS["snes"]
    assert snes.delta_save_ext == "srm"
    assert snes.retroarch_save_ext == "srm"
    assert snes.extra_files == ()
