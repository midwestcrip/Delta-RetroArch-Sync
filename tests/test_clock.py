"""The Game Boy real-time clock conversion.

The two byte strings below are not invented. They were read off disk on
2026-09-06 from a real Pokemon Crystal save: one from Delta's Dropbox folder
after playing on an iPad, one from RetroArch's Gambatte save folder after
playing the same game on the desktop. Everything here is pinned to them, so a
change to the conversion has to disagree with a real file to pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from delta_retroarch_synchronizer import clock

#: Delta's GameSave-<sha1>-gameTimeSave, four bytes, big-endian.
REAL_DELTA = bytes.fromhex("6a9dcb79")
#: 2026-09-06 20:22:17 UTC -- when Crystal was last played on the iPad.
REAL_DELTA_VALUE = 1788726137

#: Gambatte's "Pokemon - Crystal Version.rtc", eight bytes, little-endian.
REAL_RETROARCH = bytes.fromhex("a60d9e6a00000000")
#: 2026-09-07 01:04:38 UTC -- when RetroArch was closed.
REAL_RETROARCH_VALUE = 1788743078


def test_delta_file_reads_as_the_time_the_game_was_last_played():
    assert clock.delta_timestamp(REAL_DELTA) == REAL_DELTA_VALUE


def test_gambatte_file_reads_as_the_time_retroarch_was_closed():
    assert clock.retroarch_timestamp(REAL_RETROARCH) == REAL_RETROARCH_VALUE


def test_the_byte_order_is_not_a_coin_flip():
    """Read the other way round, Delta's bytes give a date in 2034.

    This is the evidence that big-endian is right rather than assumed: only one
    of the two readings lands anywhere near when the game was played.
    """
    import struct

    wrong_way = struct.unpack("<I", REAL_DELTA)[0]

    assert wrong_way > 2_000_000_000
    assert abs(REAL_DELTA_VALUE - REAL_RETROARCH_VALUE) < 24 * 3600


def test_converting_delta_to_retroarch_matches_the_real_layout():
    """Same value, in the shape Gambatte writes: eight bytes, low ones first."""
    converted = clock.to_retroarch(REAL_DELTA)

    assert len(converted) == clock.RETROARCH_BYTES
    assert converted[4:] == b"\x00\x00\x00\x00"
    assert clock.retroarch_timestamp(converted) == REAL_DELTA_VALUE


def test_converting_retroarch_to_delta_matches_the_real_layout():
    converted = clock.to_delta(REAL_RETROARCH)

    assert len(converted) == clock.DELTA_BYTES
    assert clock.delta_timestamp(converted) == REAL_RETROARCH_VALUE


@pytest.mark.parametrize("original", [REAL_DELTA, bytes.fromhex("00000000"),
                                      bytes.fromhex("ffffffff")])
def test_a_round_trip_through_retroarch_changes_nothing(original):
    assert clock.to_delta(clock.to_retroarch(original)) == original


def test_a_round_trip_through_delta_changes_nothing():
    assert clock.to_retroarch(clock.to_delta(REAL_RETROARCH)) == REAL_RETROARCH


def test_a_clock_too_large_for_delta_is_refused_not_truncated():
    """After 2106 the timestamp outgrows Delta's four bytes.

    Truncating would move the in-game clock by a hundred and thirty-six years
    while looking like a successful sync, so this has to fail loudly instead.
    """
    too_big = (clock.MAX_DELTA_TIMESTAMP + 1).to_bytes(8, "little")

    with pytest.raises(ValueError, match="refusing to truncate"):
        clock.to_delta(too_big)


def test_the_largest_value_that_does_fit_is_allowed():
    """The boundary itself must work, or the guard is off by one."""
    fits = clock.MAX_DELTA_TIMESTAMP.to_bytes(8, "little")

    assert clock.to_delta(fits) == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("size", [0, 3, 5, 8])
def test_a_delta_file_of_the_wrong_size_is_refused(size):
    """Wrong size means wrong format, and guessing would corrupt the clock."""
    with pytest.raises(ValueError, match="4 bytes"):
        clock.delta_timestamp(b"\x00" * size)


@pytest.mark.parametrize("size", [0, 4, 7, 48])
def test_a_gambatte_file_of_the_wrong_size_is_refused(size):
    """48 is the live case: that is mGBA's struct, a different format entirely."""
    with pytest.raises(ValueError, match="8 bytes"):
        clock.retroarch_timestamp(b"\x00" * size)


def test_the_clock_sits_beside_the_save_with_the_same_name():
    save = Path("saves/Gambatte/Pokemon - Crystal Version.srm")

    assert clock.retroarch_clock_path(save, "rtc") == Path(
        "saves/Gambatte/Pokemon - Crystal Version.rtc"
    )


def test_a_leading_dot_on_the_extension_is_accepted():
    save = Path("saves/Gambatte/Game.srm")

    assert clock.retroarch_clock_path(save, ".rtc").name == "Game.rtc"


def test_a_game_name_containing_a_dot_keeps_all_of_it():
    """"Dr. Mario" must not become "Dr.rtc"."""
    save = Path("saves/Gambatte/Dr. Mario.srm")

    assert clock.retroarch_clock_path(save, "rtc").name == "Dr. Mario.rtc"


def test_describe_reports_a_readable_local_time():
    described = clock.describe(REAL_DELTA_VALUE)

    assert described.startswith("2026-09-0")
    assert len(described) == len("2026-09-06 20:22")


def test_describe_survives_a_nonsense_value():
    """Used in log lines, so it must never be the thing that raises."""
    assert "unreadable" in clock.describe(clock.MAX_DELTA_TIMESTAMP * 1000)
