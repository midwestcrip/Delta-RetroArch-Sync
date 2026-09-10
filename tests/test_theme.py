"""Tests for following the operating system's light/dark setting.

The setting is read, never stored: someone who has put their machine in dark
mode has already answered this, and a second place to answer it is a setting
nobody wants to maintain.

Only the parsing is covered here. Applying a palette needs a Tk display and is
verified by looking at the window.
"""

from __future__ import annotations

import pytest

from delta_retroarch_synchronizer import theme


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, theme.DARK),
        (1, theme.LIGHT),
        # Not a documented value, but light is what Windows does by default and
        # is the safer reading of something unexpected.
        (2, theme.LIGHT),
    ],
)
def test_apps_use_light_theme_is_read_the_right_way_round(value, expected):
    """The name asks the opposite question to the one being answered."""
    assert theme.appearance_from_apps_use_light_theme(value) == expected


@pytest.mark.parametrize("value", [None, "", "0", b"\x00", True])
def test_unreadable_settings_produce_light(value):
    """A dark window on a light desktop is the worse failure of the two.

    True is included deliberately: it is an int in Python and equals 1, so it
    must not be mistaken for a missing value and must read as light.
    """
    assert theme.appearance_from_apps_use_light_theme(value) == theme.LIGHT


@pytest.mark.parametrize(
    "output, expected",
    [
        ("'prefer-dark'\n", theme.DARK),
        ("'default'\n", theme.LIGHT),
        ("'prefer-light'\n", theme.LIGHT),
        ("", theme.LIGHT),
    ],
)
def test_gsettings_colour_scheme(output, expected):
    assert theme.appearance_from_gsettings(output) == expected


def test_both_palettes_define_every_colour():
    """A missing colour surfaces as an unreadable widget, not an exception."""
    for palette in (theme.LIGHT_PALETTE, theme.DARK_PALETTE):
        for field, value in vars(palette).items():
            assert isinstance(value, str) and value, f"{palette.name}.{field}"
            if field != "name":
                assert value.startswith("#"), f"{palette.name}.{field} = {value}"


def test_palettes_actually_differ():
    """Guards against a copy-paste that leaves dark mode looking light."""
    light = vars(theme.LIGHT_PALETTE)
    dark = vars(theme.DARK_PALETTE)
    for field in ("window", "surface", "field", "ink", "log_bg", "log_fg"):
        assert light[field] != dark[field], field


def test_unknown_appearance_falls_back_to_light():
    assert theme.palette_for("solarized") is theme.LIGHT_PALETTE
    assert theme.palette_for("") is theme.LIGHT_PALETTE


def test_system_appearance_answers_one_of_two_things():
    assert theme.system_appearance() in (theme.LIGHT, theme.DARK)


def test_every_log_level_the_launcher_uses_is_a_real_tag():
    """``LOG_LEVELS`` was accurate, unused, and that let a typo through.

    Two calls passed ``"warning"`` where the tag is ``"warn"``. Tkinter ignores
    an unconfigured tag without complaint, so those lines simply rendered as
    ordinary body text -- a bug with no symptom except the colour being wrong,
    which nobody reads a log closely enough to notice.

    Parsed rather than grepped: ``_say(describe(), "" if x else "warn")`` has a
    bracket in it and a regex either misses it or matches the wrong thing.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src"
    launcher = source / "delta_retroarch_synchronizer" / "launcher.py"
    tree = ast.parse(launcher.read_text(encoding="utf-8"))

    allowed = set(theme.LOG_LEVELS) | {""}
    seen: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "_say"):
            continue
        if len(node.args) < 2:
            continue
        # The level may be a literal or a conditional between two literals.
        candidates = [node.args[1]]
        if isinstance(node.args[1], ast.IfExp):
            candidates = [node.args[1].body, node.args[1].orelse]
        for candidate in candidates:
            if isinstance(candidate, ast.Constant) and isinstance(
                candidate.value, str
            ):
                seen.add(candidate.value)

    assert seen, "no _say levels found — the parse stopped matching"
    unknown = seen - allowed
    assert not unknown, (
        f"launcher passes log level(s) {sorted(unknown)} that theme.LOG_LEVELS "
        f"does not define ({sorted(theme.LOG_LEVELS)}); tkinter would ignore "
        f"them silently"
    )
