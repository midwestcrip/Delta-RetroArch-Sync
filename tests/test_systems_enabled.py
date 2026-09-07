"""Tests guarding which systems the sync pass is allowed to write.

This set is the safety gate. A system reaching it before its save format has
been checked against a real file does not fail loudly -- it silently writes a
save the emulator cannot read, over one the player cared about.
"""

from __future__ import annotations

import pytest

from delta_retroarch_synchronizer import systems


def test_enabled_systems_are_copyable_or_converted():
    """Nothing needing conversion may be enabled until the conversion exists.

    Two ways to satisfy this, and they are kept separate on purpose. Marking N64
    ``raw_compatible`` to get it through the gate would have been the easy move
    and a lie -- it is emphatically not a plain copy, and the next person to read
    that flag would have believed it.
    """
    for key in systems.ENABLED_SYSTEMS:
        system = systems.SYSTEMS[key]
        assert system.raw_compatible or system.converted, f"{key} needs conversion first"


def test_only_n64_needs_a_conversion():
    """Every other enabled system is a straight copy with a new extension."""
    converted = {k for k in systems.SYSTEMS if systems.SYSTEMS[k].converted}
    assert converted == {"n64"}
    assert not systems.SYSTEMS["n64"].raw_compatible


def test_enabled_systems_carry_no_conversion_note():
    """A conversion note is the marker for 'we know this is not a plain copy
    and cannot do it yet'. Once the conversion exists, the note has to go, or it
    goes on being printed as a reason the game was skipped."""
    for key in systems.ENABLED_SYSTEMS:
        assert not systems.SYSTEMS[key].conversion_note, key
        assert not systems.SYSTEMS[key].conversion_summary, key


def test_ds_is_raw_despite_its_extension():
    """Enabled 2026-09-07 against a real Pokemon Platinum save.

    The extension is the trap here. Delta declares DeSmuME's `.dsv` while
    running melonDS, and a real `.dsv` carries a trailing `|-DESMUME SAVE-|`
    footer that would have to be stripped. The file says otherwise: 524,288
    bytes exactly -- 512 KB, the bare chip size and a power of two, which a
    footered file cannot be -- with no marker anywhere in it.

    So the extension is a migration leftover, the payload is raw, and the
    "conversion" is a rename. This test exists to make the claim explicit: if
    DS is ever found to need a footer after all, this is what has to change.
    """
    ds = systems.SYSTEMS["ds"]

    assert "ds" in systems.ENABLED_SYSTEMS
    assert ds.raw_compatible
    assert not ds.conversion_note
    assert ds.delta_save_ext == "dsv"
    assert ds.retroarch_save_ext == "srm"
    assert ds.extra_files == ()
    # melonDS is what Delta runs, so its libretro core is what this was
    # verified against and what should be preferred.
    assert ds.retroarch_cores[0] == "melonDS DS"


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
    assert systems.ENABLED_SYSTEMS == frozenset(
        {"gba", "snes", "gbc", "nes", "ds", "n64"}
    )


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

    assert systems.missing_core_advice(bare) == "No core installed for Nothing."


def test_the_advice_is_given_once_and_names_every_game_it_covers():
    """Six N64 games used to produce the same four lines six times over, which
    the third naive-user test called "very unclear if multiple games are found".
    The advice is about the system, not the game."""
    games = ["Super Mario 64", "Ocarina of Time", "Paper Mario"]

    advice = systems.missing_core_advice(systems.SYSTEMS["n64"], games)

    assert advice.count("Download a Core") == 1
    assert "3 games skipped" in advice
    for game in games:
        assert game in advice


def test_one_game_is_named_rather_than_counted():
    advice = systems.missing_core_advice(systems.SYSTEMS["nes"], ["Kirby's Adventure"])

    assert "Kirby's Adventure skipped" in advice
    assert "1 games" not in advice


def test_a_converted_system_recommends_only_a_core_that_would_actually_sync():
    """ParaLLEl N64 is listed so an existing install can be recognised, never
    so it can be recommended: the conversion was written against
    Mupen64Plus-Next's layout and the sync skips anything else. Suggesting it
    would send someone to install a core and find nothing syncs."""
    preferred, others = systems.core_recommendation(systems.SYSTEMS["n64"])

    assert preferred == "Mupen64Plus-Next"
    assert others == ()

    advice = systems.missing_core_advice(systems.SYSTEMS["n64"], ["Super Mario 64"])

    assert "ParaLLEl" not in advice
    assert "play fine but are skipped" in advice


def test_a_plain_copy_system_may_recommend_its_alternates():
    """Not the same judgement: RetroArch's frontend owns writing the .srm and
    cores cannot override it, so for a system that is a straight copy every
    core in the list really does work."""
    preferred, others = systems.core_recommendation(systems.SYSTEMS["gba"])

    assert preferred == "mGBA"
    assert "VBA-M" in others
