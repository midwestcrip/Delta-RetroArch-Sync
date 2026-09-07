"""A real notification-area icon, for the time the window spends out of the way.

The third naive-user test asked for this: while RetroArch has the screen, the
launcher should get out of it and come back afterwards. The first attempt used
``iconify``, which is not what was asked for and the user said so -- a minimised
window still occupies a taskbar button, which is exactly the thing they wanted
gone. ``withdraw`` does remove it from the taskbar, but on its own it leaves the
program running with no way to reach it, which is worse than a taskbar button.

So: withdraw the window and put an icon in the notification area, which is what
"minimise to the tray" has always meant.

**Why this is threaded.** A tray icon is not an object you own; it is a promise
to answer messages. Windows delivers clicks to a window procedure, and a window
procedure only runs while some thread pumps messages for it. Tk owns the main
thread's message loop and will not forward anything it does not recognise, so
this creates its own message-only window on its own thread and pumps that.
Clicks arrive on that thread, so the callback must be something safe to call
from anywhere -- the launcher hands it a queue put, not a Tk call.

No dependency: ``Shell_NotifyIcon`` is in shell32 and ctypes can reach it. The
alternative was pystray, which pulls in Pillow, for a program that currently
needs nothing outside the standard library.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
#: The first message id applications may define for themselves.
WM_APP = 0x8000
#: Sent to us for every mouse event on the icon; the click is in lParam.
WM_TRAYICON = WM_APP + 1
#: Our own request to take the icon down again, posted from another thread.
WM_TRAYQUIT = WM_APP + 2

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49

CW_USEDEFAULT = 0x80000000


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def supported() -> bool:
    return sys.platform == "win32"


class TrayIcon:
    """One notification-area icon, for as long as it is wanted.

    Started with :meth:`show` and taken down with :meth:`hide`. Both are safe to
    call from any thread and safe to call twice; an icon left behind when the
    program exits is the classic tray bug, so hiding is made hard to get wrong
    rather than merely documented.
    """

    #: Distinguishes our icon from any other this process might add. Any
    #: constant works; it only has to be stable between the add and the delete.
    ICON_ID = 1

    def __init__(self, icon: Path, tooltip: str, on_click: Callable[[], None]) -> None:
        self.icon = icon
        self.tooltip = tooltip[:127]
        self.on_click = on_click
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._failed = False
        # Held on the instance because Windows keeps a raw pointer to it. A
        # WNDPROC that gets garbage collected while registered is a crash
        # inside Windows, with a traceback that names none of this.
        self._wndproc = WNDPROC(self._handle_message)

    # ------------------------------------------------------------- public

    def show(self) -> bool:
        """Put the icon up. False if it could not be done, for any reason.

        The caller must treat False as "stay on the taskbar": a withdrawn window
        with no tray icon is a program the user cannot get back to.
        """
        if not supported() or not self.icon.is_file():
            return False
        if self._thread is not None and self._thread.is_alive():
            return not self._failed

        self._ready.clear()
        self._failed = False
        self._thread = threading.Thread(
            target=self._run, name="tray-icon", daemon=True
        )
        self._thread.start()
        # Bounded, so a failure to create the window cannot hang the sync that
        # is waiting to hand the screen over to RetroArch.
        self._ready.wait(timeout=5.0)
        return not self._failed and self._hwnd is not None

    def hide(self) -> None:
        """Take the icon down and stop the thread. Safe to call when it is
        already down."""
        hwnd = self._hwnd
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, WM_TRAYQUIT, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._hwnd = None

    # ------------------------------------------------------------ internal

    def _run(self) -> None:
        try:
            self._pump()
        except Exception:  # pragma: no cover -- reported through _failed
            self._failed = True
        finally:
            self._ready.set()

    def _pump(self) -> None:
        user32 = ctypes.windll.user32
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        instance = ctypes.windll.kernel32.GetModuleHandleW(None)
        # Unique per instance: registering a class name twice fails, and this
        # object can be created again after a hide.
        class_name = f"DeltaRetroArchTray{id(self)}"

        window_class = WNDCLASS()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._failed = True
            self._ready.set()
            return

        # Message-only (HWND_MESSAGE as the parent): never drawn, never in the
        # task switcher, and it exists purely to receive the icon's clicks.
        hwnd = user32.CreateWindowExW(
            0, class_name, class_name, 0,
            0, 0, 0, 0, wintypes.HWND(-3), None, instance, None,
        )
        if not hwnd:
            self._failed = True
            self._ready.set()
            user32.UnregisterClassW(class_name, instance)
            return

        self._hwnd = hwnd
        if not self._add_icon(hwnd):
            self._failed = True
            self._ready.set()
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, instance)
            self._hwnd = None
            return

        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        user32.UnregisterClassW(class_name, instance)

    def _add_icon(self, hwnd: int) -> bool:
        user32 = ctypes.windll.user32
        size = user32.GetSystemMetrics(SM_CXSMICON)
        # The exact size Windows will draw, so the .ico's matching entry is used
        # rather than a larger one shrunk. See display.set_window_icon.
        handle = user32.LoadImageW(
            None, str(self.icon), IMAGE_ICON, size, size, LR_LOADFROMFILE
        )

        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = self.ICON_ID
        data.uFlags = NIF_MESSAGE | NIF_TIP | (NIF_ICON if handle else 0)
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = handle
        data.szTip = self.tooltip
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

    def _remove_icon(self, hwnd: int) -> None:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = self.ICON_ID
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

    def _handle_message(self, hwnd, message, wparam, lparam):
        user32 = ctypes.windll.user32
        if message == WM_TRAYICON:
            # Any ordinary click brings the window back. Not just double-click:
            # a tray icon that ignores a single click reads as broken, and there
            # is only one thing this icon can do.
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP):
                try:
                    self.on_click()
                except Exception:  # pragma: no cover -- never kill the pump
                    pass
            return 0
        if message == WM_TRAYQUIT:
            self._remove_icon(hwnd)
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)
