"""Rendering at the display's real resolution.

The program never told Windows it understood high-DPI displays, so Windows drew
it at 96 DPI into an off-screen bitmap and stretched that to fit. On the 150%
display it was found on, every glyph, border and icon -- the taskbar icon
included -- was a 1.5x enlargement of a smaller drawing.

Declaring awareness is only half of it. Doing that alone leaves everything a
third too small, because Tk then paints its 96-DPI sizes onto a 144-DPI screen.
Point-sized fonts follow ``tk scaling``; every raw pixel dimension has to go
through ``px``, and these tests are what stop one being added back unscaled.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import display  # noqa: E402

ICON = Path(__file__).resolve().parents[1] / "assets" / "synchronizer.ico"


@pytest.fixture(autouse=True)
def unscaled():
    """Leave the factor as it was found. It is process-wide state, and a test
    that scaled and did not restore would silently resize another one."""
    before = display.scale()
    display.reset(1.0)
    yield
    display.reset(before)


# ------------------------------------------------------------------ scaling


def test_a_hundred_percent_display_changes_nothing():
    assert display.px(10) == 10
    assert display.pad(10, 5) == (10, 5)


def test_every_dimension_grows_with_the_display():
    display.reset(1.5)

    assert display.px(10) == 15
    assert display.px(22) == 33
    assert display.pad(16, 7) == (24, 11)


def test_a_hairline_never_rounds_away_to_nothing():
    """A one-pixel border scaled by 0.4 truncates to zero and the widget looks
    missing rather than thin. Rounding down to nothing is never right for
    something that was asked for."""
    display.reset(0.4)

    assert display.px(1) == 1
    assert display.px(2) == 1


def test_zero_stays_zero():
    """Padding of zero is a deliberate choice, not a small number."""
    display.reset(2.0)

    assert display.px(0) == 0


class FakeRoot:
    """Just enough Tk to measure. ``adopt`` asks the toolkit rather than
    Windows, because what matters is the number Tk believes."""

    def __init__(self, per_inch: float) -> None:
        self.per_inch = per_inch
        self.calls: list[tuple] = []
        self.tk = self

    def winfo_fpixels(self, _spec: str) -> float:
        return self.per_inch

    def call(self, *args) -> None:
        self.calls.append(args)


def test_adopt_takes_the_factor_from_what_tk_believes():
    root = FakeRoot(144.0)

    factor = display.adopt(root)

    assert factor == 1.5
    assert display.px(10) == 15


def test_adopt_puts_tk_scaling_in_points_not_in_the_scale_factor():
    """`tk scaling` is pixels per *point*, and there are 72 to the inch --
    not 96. Feeding it the 96-based factor would size every font by three
    quarters of what was asked for."""
    root = FakeRoot(144.0)

    display.adopt(root)

    assert root.calls == [("tk", "scaling", 2.0)]


def test_a_display_tk_reports_as_ordinary_leaves_everything_alone():
    """Self-correcting: if awareness was refused, Tk still says 96 and nothing
    below it changes. That is what makes a failure cosmetic rather than a
    window laid out for a resolution it is not on."""
    assert display.adopt(FakeRoot(96.0)) == 1.0
    assert display.px(10) == 10


def test_an_unmeasurable_display_does_not_take_the_window_down():
    class Broken(FakeRoot):
        def winfo_fpixels(self, _spec):
            raise RuntimeError("no display")

    assert display.adopt(Broken(0)) == 1.0


# ------------------------------------------------------ off Windows entirely


def test_nothing_here_is_attempted_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(display.sys, "platform", "linux")

    assert display.make_process_aware() == "not Windows"
    assert display.set_app_id("x") is False
    assert display.set_window_icon(None, ICON) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI APIs")
def test_windows_grants_awareness_through_one_of_the_three_apis():
    """Three are tried because each arrived in a different Windows. Any of them
    is a pass; "unavailable" would mean the window is still being stretched."""
    assert display.make_process_aware() in {"per-monitor v2", "per-monitor", "system"}
    assert display.awareness() != "unavailable"


# ---------------------------------------------------------------- the icon


def icon_sizes() -> set[int]:
    """Read the .ico directory. Parsed rather than opened with Pillow, which is
    a build dependency and not installed to run the tool."""
    data = ICON.read_bytes()
    _, kind, count = struct.unpack("<HHH", data[:6])
    assert kind == 1, "not an icon file"
    sizes = set()
    for index in range(count):
        offset = 6 + index * 16
        width = data[offset]
        sizes.add(width or 256)  # zero means 256 in the .ico header
    return sizes


def test_the_icon_carries_a_real_entry_for_every_scaling_windows_offers():
    """The reported symptom, and the half of it that awareness does not fix.

    Windows asks for the small icon at 16 logical pixels and the large one at
    32, both in *physical* pixels once the process is DPI-aware -- so 20 and 40
    at 125%, 24 and 48 at 150%. The set used to be 16/32/48/64/128/256, which
    left a 125% display with neither size it wanted, and Windows enlarged a 16
    into a 20. Enlarging is what makes an icon look soft, and shipping the
    sizes is the whole reason an .ico holds more than one.
    """
    have = icon_sizes()

    for percent in (100, 125, 150, 175, 200):
        small = round(16 * percent / 100)
        large = round(32 * percent / 100)
        assert small in have, f"no {small}px entry for {percent}% displays"
        assert large in have, f"no {large}px entry for {percent}% displays"


def test_the_icon_still_carries_the_large_sizes_the_shell_uses():
    assert {128, 256} <= icon_sizes()
