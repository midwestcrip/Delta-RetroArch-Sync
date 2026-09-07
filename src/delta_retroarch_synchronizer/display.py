"""Making the window render at the display's real resolution.

Until this existed the program never told Windows it understood high-DPI
displays, so Windows did what it does for every program that stays silent: drew
it at 96 DPI into an off-screen bitmap and stretched that to fit. On a 150%
display -- which is the default for most laptops sold now, and what this was
found on -- every glyph, border and icon in the window was a 1.5x bitmap
enlargement of a smaller drawing. Blurry, including the taskbar icon, which is
the same window's icon scaled the same way.

It also made a subtler bug possible. ``GetWindowRect`` answers a virtualised
process in *logical* pixels while the screen is *physical*, so anything mixing
Tk's idea of a size with a real screen coordinate silently disagreed by the
scale factor.

Two halves, and both are needed. Declaring awareness stops the stretching, and
on its own that makes everything a third too small, because Tk then draws its
96-DPI sizes onto a 144-DPI screen. So the second half rescales: point-sized
fonts follow ``tk scaling``, and every dimension written in raw pixels has to go
through :func:`px`.

Windows-only by nature. Everything here reports failure quietly and leaves the
window working, because a blurry window is a far better outcome than no window.
"""

from __future__ import annotations

import ctypes
import math
import sys
from pathlib import Path

#: Passed to SetProcessDpiAwarenessContext. Per-monitor v2 is the modern one:
#: it rescales the window when it is dragged between monitors of different
#: scaling, rather than fixing the factor at launch.
_PER_MONITOR_AWARE_V2 = -4
#: SetProcessDpiAwareness, the Windows 8.1 spelling of roughly the same idea.
_PROCESS_PER_MONITOR_DPI_AWARE = 2

#: What Windows calls 100%. Every scale factor here is relative to it.
BASELINE_DPI = 96.0
#: Tk sizes fonts in points, and there are 72 points to the inch.
POINTS_PER_INCH = 72.0

WM_SETICON = 0x0080
ICON_BIG = 1
ICON_SMALL = 0
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_SM_CXICON, _SM_CXSMICON = 11, 49

#: Set by :func:`adopt`. One means either a 100% display or a failure to become
#: aware, and in both cases leaving every dimension alone is correct.
_scale = 1.0

#: Which awareness API took, remembered so the window can say so if none did.
_awareness = "not attempted"


def on_windows() -> bool:
    return sys.platform == "win32"


def make_process_aware() -> str:
    """Tell Windows this process draws at the real resolution.

    Must run **before** the first window exists: awareness is fixed when the
    first window is created and cannot be changed afterwards.

    Three APIs, newest first, because each arrived in a different Windows and
    the older ones stay for compatibility. Returns which one took, for the log.
    """
    global _awareness
    _awareness = _detect_awareness()
    return _awareness


def awareness() -> str:
    """Which API granted DPI awareness, or why none did."""
    return _awareness


def _detect_awareness() -> str:
    if not on_windows():
        return "not Windows"

    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):  # pragma: no cover -- not reachable here
        return "unavailable"

    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is not None:
        # The context is a handle-sized value, and passing it as a plain int
        # truncates on 64-bit -- which fails silently rather than raising.
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_bool
        if setter(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            return "per-monitor v2"

    try:
        shcore = ctypes.windll.shcore
    except (AttributeError, OSError):
        shcore = None
    if shcore is not None:
        # Returns an HRESULT: zero is success, and E_ACCESSDENIED means someone
        # already set it (a manifest, or a second call), which is also fine.
        if shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "per-monitor"

    if getattr(user32, "SetProcessDPIAware", None) is not None:
        if user32.SetProcessDPIAware():
            return "system"

    return "unavailable"


def window_dpi(root) -> float | None:
    """Ask Windows what this window's display resolution really is.

    ``GetDpiForWindow`` is the per-monitor-correct question and the one to ask
    of a per-monitor-aware process: there is no single system DPI for such a
    process, because the answer changes when the window is dragged to another
    screen.

    This is not what the first version did. It asked Tk instead, via
    ``winfo_fpixels("1i")``, on the reasoning that what mattered was the number
    Tk believed. Running from source that reads 144 on a 150% display and
    everything looked right. **The built executable reported 96 and rendered a
    third too small** -- awareness granted, ``GetDpiForWindow`` answering 144,
    and Tk's screen measurement still saying 96. Only running the real .exe
    found it; every check against a source run had passed.

    ``GetDpiForSystem`` is the fallback for Windows 8.1, where neither
    per-window DPI nor ``GetDpiForWindow`` exists.
    """
    if not on_windows():
        return None
    try:
        user32 = ctypes.windll.user32
        handle = root.winfo_id()
        window = user32.GetParent(handle) or handle
    except Exception:  # pragma: no cover -- no window to ask about
        return None

    for name, arguments in (("GetDpiForWindow", (window,)), ("GetDpiForSystem", ())):
        function = getattr(user32, name, None)
        if function is None:
            continue
        try:
            value = function(*arguments)
        except Exception:  # pragma: no cover
            continue
        if value:
            return float(value)
    return None


def adopt(root) -> float:
    """Rescale a freshly created window to the display it is on.

    Windows is asked first and Tk second. Either way this stays
    self-correcting: with awareness refused both report 96, and every dimension
    below resolves to no change at all.
    """
    global _scale
    per_inch = window_dpi(root)
    if per_inch is None:
        try:
            per_inch = float(root.winfo_fpixels("1i"))
        except Exception:  # pragma: no cover -- a display that cannot be measured
            _scale = 1.0
            return _scale
    if per_inch <= 0:  # pragma: no cover
        _scale = 1.0
        return _scale

    # Fonts are given in points throughout, so this one call handles all of
    # them. Everything written in pixels still has to go through px().
    try:
        root.tk.call("tk", "scaling", per_inch / POINTS_PER_INCH)
    except Exception:  # pragma: no cover
        pass

    _scale = per_inch / BASELINE_DPI
    return _scale


def scale() -> float:
    return _scale


def reset(factor: float = 1.0) -> None:
    """Set the factor directly. For tests, and for nothing else."""
    global _scale
    _scale = factor


def px(value: float) -> int:
    """A pixel dimension, in the display's pixels rather than in 96-DPI ones.

    Rounded half up rather than with :func:`round`, whose banker's rounding
    sends 10.5 to 10 and 11.5 to 12 -- defensible for statistics and merely
    baffling for a padding. And never below one: a hairline that scales to 0.4
    and truncates to zero disappears entirely, which reads as a missing widget
    rather than a rounding error.
    """
    scaled = math.floor(value * _scale + 0.5)
    if value > 0:
        return max(1, scaled)
    return scaled


def pad(*values: float) -> tuple[int, ...]:
    """Scale a ttk padding tuple, which is the commonest use of :func:`px`."""
    return tuple(px(value) for value in values)


def set_app_id(app_id: str) -> bool:
    """Give the process its own taskbar identity.

    Without this, a Python-hosted window is grouped under whichever
    ``pythonw.exe`` launched it and shows *its* icon on the taskbar, however
    good the icon we set on the window. Harmless in the frozen build, which has
    its own executable, and it also keeps a pinned Start menu shortcut attached
    to the right window.
    """
    if not on_windows():
        return False
    try:
        shell32 = ctypes.windll.shell32
    except (AttributeError, OSError):  # pragma: no cover
        return False
    try:
        return shell32.SetCurrentProcessExplicitAppUserModelID(app_id) == 0
    except Exception:  # pragma: no cover
        return False


def set_window_icon(root, icon: Path) -> bool:
    """Load the icon at the sizes Windows is actually asking for.

    Tk's ``iconbitmap`` hands the whole .ico to Windows and lets it choose,
    which on a scaled display frequently means taking the 32x32 entry and
    enlarging it to the 48 pixels the taskbar wants. Asking ``LoadImage`` for a
    specific size makes it pick the entry that matches -- this .ico carries
    16, 32, 48, 64, 128 and 256 -- so nothing is enlarged.

    ``GetSystemMetrics`` is queried rather than assumed because the answer is
    already in physical pixels for a DPI-aware process: 48 at 150%, not 32.
    """
    if not on_windows() or not icon.is_file():
        return False
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):  # pragma: no cover
        return False

    try:
        handle = root.winfo_id()
        window = user32.GetParent(handle) or handle
    except Exception:  # pragma: no cover
        return False

    loaded = False
    for which, metric in ((ICON_BIG, _SM_CXICON), (ICON_SMALL, _SM_CXSMICON)):
        size = user32.GetSystemMetrics(metric)
        image = user32.LoadImageW(
            None, str(icon), _IMAGE_ICON, size, size, _LR_LOADFROMFILE
        )
        if not image:
            continue
        user32.SendMessageW(window, WM_SETICON, which, image)
        loaded = True
    return loaded
