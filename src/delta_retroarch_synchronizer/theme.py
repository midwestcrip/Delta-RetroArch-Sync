"""Follow the operating system's light/dark setting.

Deliberately not a preference. Somebody who has set their machine to dark mode
has already answered this question, and asking it again in a second place is how
a small tool accumulates settings nobody wants to maintain. The window reads the
system value at startup and keeps checking, so switching the OS theme restyles
the window without restarting it.

ttk's native Windows themes ignore most colour configuration -- a Checkbutton
drawn by the OS stays light no matter what the style says -- so applying a theme
at all means switching to ``clam``, which draws its own widgets and honours
them. That trades native Windows widgets for consistent ones, which is the same
trade IDLE's own settings dialog makes.
"""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from . import display

#: How often to re-read the system setting. Cheap on Windows (one registry
#: read); memoised elsewhere, see system_appearance.
POLL_MS = 4000

LIGHT = "light"
DARK = "dark"


@dataclass(frozen=True)
class Palette:
    """Every colour the window uses, so none are picked at the call site."""

    name: str
    #: Dialog background, behind everything.
    window: str
    #: Raised surfaces: buttons, unselected tabs.
    surface: str
    #: Editable fields.
    field: str
    ink: str
    muted: str
    border: str
    accent: str
    accent_ink: str
    #: The log panel, which is the main element of the window.
    log_bg: str
    log_fg: str
    #: Severity in the log. Always redundant with the wording, never the
    #: only signal -- a line that reads 'FAILED' says so whether or not the
    #: reader can see the colour.
    log_ok: str
    log_warn: str
    log_error: str
    log_muted: str
    select_bg: str
    select_fg: str
    #: Hover descriptions, which are their own small window.
    tip_bg: str
    tip_fg: str


LIGHT_PALETTE = Palette(
    name=LIGHT,
    window="#f0f0f0",
    surface="#fafafa",
    field="#ffffff",
    ink="#1b1b1b",
    muted="#5a5a5a",
    border="#c4c4c4",
    accent="#1a5fb4",
    accent_ink="#ffffff",
    log_bg="#ffffff",
    log_fg="#1b1b1b",
    log_ok="#1a7f37",
    log_warn="#8a6100",
    log_error="#b3261e",
    log_muted="#6f6f6f",
    select_bg="#cfe3ff",
    select_fg="#000000",
    tip_bg="#ffffe0",
    tip_fg="#1b1b1b",
)

DARK_PALETTE = Palette(
    name=DARK,
    window="#2b2b2b",
    surface="#353535",
    field="#1e1e1e",
    ink="#e8e8e8",
    muted="#a0a0a0",
    border="#454545",
    accent="#4a9eff",
    accent_ink="#0d1117",
    # A dark navy rather than pure black, which is what IDLE's own dark theme
    # uses for its code panel and reads as a console rather than a void.
    log_bg="#001b33",
    log_fg="#d6dde6",
    log_ok="#6fdc8c",
    log_warn="#f5c451",
    log_error="#ff8a80",
    log_muted="#87a0b8",
    select_bg="#2f5b8c",
    select_fg="#ffffff",
    tip_bg="#3c3c3c",
    tip_fg="#e8e8e8",
)

PALETTES = {LIGHT: LIGHT_PALETTE, DARK: DARK_PALETTE}

#: The palette last applied. Widgets that are not ttk -- the log and the
#: hover descriptions -- read this so they restyle with everything else
#: rather than keeping the colours they were built with.
_current: Palette = LIGHT_PALETTE


def current() -> Palette:
    return _current


def appearance_from_apps_use_light_theme(value: object) -> str:
    """Read Windows' AppsUseLightTheme DWORD.

    0 is dark and 1 is light -- the name asks the opposite question to the one
    being answered here, which is easy to invert by accident. Anything that is
    not an integer 0 means light, because light is what Windows does by default
    and an unreadable setting should not produce a dark window on a light
    desktop.
    """
    return DARK if isinstance(value, int) and value == 0 else LIGHT


def appearance_from_gsettings(output: str) -> str:
    """Read GNOME's colour-scheme, which prints e.g. 'prefer-dark'."""
    return DARK if "dark" in output.strip().strip("'\"").lower() else LIGHT


def _windows_appearance() -> str:
    try:
        import winreg
    except ImportError:
        return LIGHT
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return LIGHT
    return appearance_from_apps_use_light_theme(value)


#: macOS and Linux need a subprocess to answer, which is far too expensive to
#: repeat on a timer, so their answer is read once. Windows is a registry read
#: and is re-read every poll, which is the platform this tool ships on.
_cached_unix_appearance: str | None = None


def _unix_appearance() -> str:
    global _cached_unix_appearance
    if _cached_unix_appearance is not None:
        return _cached_unix_appearance

    command = (
        ["defaults", "read", "-g", "AppleInterfaceStyle"]
        if sys.platform == "darwin"
        else ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2)
        output = result.stdout
    except (OSError, subprocess.SubprocessError):
        output = ""

    _cached_unix_appearance = (
        DARK if "dark" in output.strip().lower() else LIGHT
    )
    return _cached_unix_appearance


def system_appearance() -> str:
    """"dark" or "light", falling back to light when it cannot be determined."""
    if sys.platform == "win32":
        return _windows_appearance()
    return _unix_appearance()


def palette_for(appearance: str) -> Palette:
    return PALETTES.get(appearance, LIGHT_PALETTE)


def apply(root: tk.Misc, palette: Palette) -> None:
    """Restyle every widget class the window uses."""
    global _current
    _current = palette

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover -- clam ships with every Tk build
        pass

    root.configure(background=palette.window)

    style.configure(
        ".",
        background=palette.window,
        foreground=palette.ink,
        fieldbackground=palette.field,
        bordercolor=palette.border,
        darkcolor=palette.window,
        lightcolor=palette.window,
        troughcolor=palette.field,
        focuscolor=palette.accent,
        selectbackground=palette.select_bg,
        selectforeground=palette.select_fg,
        insertcolor=palette.ink,
    )
    style.configure("TFrame", background=palette.window)
    style.configure("TLabel", background=palette.window, foreground=palette.ink)
    style.configure("Muted.TLabel", foreground=palette.muted)

    style.configure(
        "TLabelframe", background=palette.window, bordercolor=palette.border
    )
    style.configure(
        "TLabelframe.Label", background=palette.window, foreground=palette.muted
    )

    style.configure(
        "TButton",
        background=palette.surface,
        foreground=palette.ink,
        bordercolor=palette.border,
        padding=display.pad(10, 5),
    )
    style.map(
        "TButton",
        background=[("pressed", palette.border), ("active", palette.border)],
        foreground=[("disabled", palette.muted)],
    )

    # The one thing the window exists to do, so it does not look like the
    # buttons beside it.
    style.configure(
        "Primary.TButton",
        background=palette.accent,
        foreground=palette.accent_ink,
        bordercolor=palette.accent,
        padding=display.pad(10, 9),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", palette.accent), ("active", palette.accent)],
        foreground=[("disabled", palette.muted)],
    )

    # A button that has just done something says so on itself. Same padding as
    # TButton on purpose: this style is swapped in and out under the pointer,
    # and a different size would make the surrounding row jump.
    style.configure(
        "Success.TButton",
        background=palette.surface,
        foreground=palette.log_ok,
        bordercolor=palette.log_ok,
        padding=display.pad(10, 5),
    )
    style.map(
        "Success.TButton",
        background=[("pressed", palette.border), ("active", palette.surface)],
        foreground=[("disabled", palette.muted), ("active", palette.log_ok)],
    )

    # The indicator is drawn by clam at a fixed pixel size that ignores
    # `tk scaling`, so on a 150% display it stays a 10-pixel box beside
    # 18-pixel text -- which reads as a rendering fault rather than a style.
    style.configure(
        "TCheckbutton",
        background=palette.window,
        foreground=palette.ink,
        indicatorbackground=palette.field,
        indicatorforeground=palette.accent_ink,
        indicatorsize=display.px(11),
        indicatormargin=display.pad(1, 1, 5, 1),
    )
    style.map(
        "TCheckbutton",
        background=[("active", palette.window)],
        indicatorbackground=[("selected", palette.accent)],
    )

    style.configure(
        "TEntry",
        fieldbackground=palette.field,
        foreground=palette.ink,
        bordercolor=palette.border,
        insertcolor=palette.ink,
        padding=display.px(4),
    )

    style.configure("TNotebook", background=palette.window, bordercolor=palette.border)
    style.configure(
        "TNotebook.Tab",
        background=palette.surface,
        foreground=palette.muted,
        bordercolor=palette.border,
        padding=display.pad(16, 7),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette.window)],
        foreground=[("selected", palette.ink)],
    )

    # The backup list. `fieldbackground` is the part that is easy to miss: without
    # it the empty area below the last row keeps ttk's default white, which in
    # dark mode leaves a bright slab under the list.
    style.configure(
        "Treeview",
        background=palette.field,
        fieldbackground=palette.field,
        foreground=palette.ink,
        bordercolor=palette.border,
        rowheight=display.px(22),
    )
    style.configure(
        "Treeview.Heading",
        background=palette.surface,
        foreground=palette.muted,
        bordercolor=palette.border,
        padding=display.pad(6, 4),
    )
    style.map(
        "Treeview",
        background=[("selected", palette.select_bg)],
        foreground=[("selected", palette.select_fg)],
    )
    style.map("Treeview.Heading", background=[("active", palette.surface)])


#: Tag names the launcher applies to log lines. "" means ordinary text.
LOG_LEVELS = ("heading", "ok", "warn", "error", "muted")


def apply_to_log(widget: tk.Text, palette: Palette) -> None:
    """The log is a tk.Text, which ttk styles do not reach."""
    widget.configure(
        background=palette.log_bg,
        foreground=palette.log_fg,
        insertbackground=palette.log_fg,
        selectbackground=palette.select_bg,
        selectforeground=palette.select_fg,
        highlightthickness=0,
        borderwidth=0,
    )

    # Re-applied on every theme change, which is why these are configured here
    # rather than once at build time.
    family = str(widget.cget("font")).split()[0] or "Consolas"
    widget.tag_configure("heading", foreground=palette.ink, font=(family, 10, "bold"))
    widget.tag_configure("ok", foreground=palette.log_ok)
    widget.tag_configure("warn", foreground=palette.log_warn)
    widget.tag_configure("error", foreground=palette.log_error)
    widget.tag_configure("muted", foreground=palette.log_muted)
