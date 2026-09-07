"""Making the Game Boy clock something you can actually check.

The clock is never stored as a value. Gambatte derives it as
``std::time(0) - baseTime_`` every time it is read, which is what keeps it
running while nothing is running and what makes two machines agree when they
share a base. The cost of that design is that nothing on disk *looks* like a
clock -- Delta's file is four opaque bytes -- so until this existed there was no
way to tell a working clock from a broken one except by squinting at the in-game
time and guessing. On 2026-09-07 that produced exactly the wrong guess.

So: `inspect` reports what the cartridge clock reads, and `doctor` checks the one
thing that can silently be wrong -- whether the two sides count from the same
instant.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import clock, health  # noqa: E402

#: A real base time, from Pokemon Crystal on this machine.
BASE = 1788726137
HOUR = 3600
DAY = 86400


class CartridgeClockTests(unittest.TestCase):
    def test_it_reads_zero_at_the_base(self) -> None:
        self.assertEqual(clock.cartridge_clock(BASE, BASE), (0, 0, 0, 0))

    def test_it_counts_forward_in_real_time(self) -> None:
        """The property the whole design turns on: it never stops.

        Not "resumes from where it was" -- it keeps counting while nothing is
        running, on both machines, because it is derived from the base rather
        than stored.
        """
        self.assertEqual(
            clock.cartridge_clock(BASE, BASE + 2 * DAY + 3 * HOUR + 240), (2, 3, 4, 0)
        )

    def test_a_base_in_the_future_does_not_go_negative(self) -> None:
        self.assertEqual(clock.cartridge_clock(BASE, BASE - 500), (0, 0, 0, 0))

    def test_the_reading_is_human_sized(self) -> None:
        self.assertEqual(clock.describe_cartridge_clock(BASE, BASE + 18 * HOUR + 1500), "0d 18:25")

    def test_a_wrapped_counter_says_so(self) -> None:
        """Gambatte's day counter is nine bits and wraps at 511, setting a carry.

        Printing "700d" as though it were a normal reading would be a quiet lie.
        """
        described = clock.describe_cartridge_clock(BASE, BASE + 700 * DAY)
        self.assertIn("wrapped", described)


class ClockCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.delta = self.root / "gameTimeSave"
        self.retro = self.root / "game.rtc"
        self.now = BASE + 18 * HOUR

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_both(self, delta_base: int, retro_base: int) -> None:
        self.delta.write_bytes(delta_base.to_bytes(4, "big"))
        self.retro.write_bytes(retro_base.to_bytes(8, "little"))

    def test_a_game_with_no_clock_is_not_reported_at_all(self) -> None:
        """Every system but Game Boy Color. A passing check about a thing that
        does not exist is just noise in the report."""
        self.assertIsNone(health.clock_check(None, self.retro, now=self.now))

    def test_matching_bases_pass_and_show_the_reading(self) -> None:
        self.write_both(BASE, BASE)

        check = health.clock_check(self.delta, self.retro, now=self.now)

        assert check is not None
        self.assertTrue(check.ok)
        self.assertIn("agrees on both sides", check.detail)
        self.assertIn("0d 18:00", check.detail)

    def test_differing_bases_fail_and_name_both_instants(self) -> None:
        """The one failure that would otherwise be invisible.

        Nothing else in the report would notice, and the symptom -- a different
        in-game date on phone and desktop -- looks like a game bug.
        """
        self.write_both(BASE, BASE - 6 * HOUR)

        check = health.clock_check(self.delta, self.retro, now=self.now)

        assert check is not None
        self.assertFalse(check.ok)
        self.assertIn("different instants", check.detail)
        self.assertIn("apart", check.detail)

    def test_a_missing_retroarch_clock_is_not_a_fault(self) -> None:
        """It is written with the save, so before the first sync there is none."""
        self.delta.write_bytes(BASE.to_bytes(4, "big"))

        check = health.clock_check(self.delta, None, now=self.now)

        assert check is not None
        self.assertTrue(check.ok)
        self.assertIn("no clock yet", check.detail)

    def test_an_unverified_core_is_reported_rather_than_compared(self) -> None:
        """The two sides are *expected* to differ when we deliberately do not
        write the clock, so comparing them would raise a false alarm."""
        self.write_both(BASE, BASE - 6 * HOUR)

        check = health.clock_check(
            self.delta, self.retro, now=self.now, core_note="not synced on mGBA"
        )

        assert check is not None
        self.assertTrue(check.ok)
        self.assertIn("mGBA", check.detail)

    def test_an_unreadable_clock_fails_rather_than_being_skipped(self) -> None:
        self.delta.write_bytes(b"\x01\x02")  # wrong width
        self.retro.write_bytes(BASE.to_bytes(8, "little"))

        check = health.clock_check(self.delta, self.retro, now=self.now)

        assert check is not None
        self.assertFalse(check.ok)
        self.assertIn("unreadable", check.detail)


if __name__ == "__main__":
    unittest.main()
