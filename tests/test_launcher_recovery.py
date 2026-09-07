"""Recovering when the missing thing stops being missing.

The empty state tells you to go and install Dropbox, sign in, install
RetroArch, run it once -- and then press **Check status**. That sequence could
not work. Paths were resolved once when the window opened, so the button
reported the same absence forever and restarting the program was the only way
through. The third naive-user test walked straight into it: "Check status just
repeats that Delta folder is not set or missing".

These use a real Tk window, because the bug lived in the wiring between the
worker thread, the config and the entry fields, and a stub of any of the three
would have reproduced the wiring rather than tested it.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import config as config_module  # noqa: E402
from delta_retroarch_synchronizer import discovery, launcher  # noqa: E402


@pytest.fixture
def window(monkeypatch, tmp_path):
    """A launcher on a machine where nothing is installed yet."""
    monkeypatch.setattr(config_module, "load", lambda path=None: config_module.Config(
        sync_on_open=False, shortcuts_offered=True
    ))
    monkeypatch.setattr(config_module, "save", lambda cfg, path=None: tmp_path / "c.toml")
    monkeypatch.setattr(
        discovery, "find_delta_folder",
        lambda: discovery.Discovery("Delta Emulator folder", None, "not signed in"),
    )
    monkeypatch.setattr(
        discovery, "find_retroarch_config",
        lambda: discovery.Discovery("retroarch.cfg", None, "not installed"),
    )
    monkeypatch.setattr(discovery, "find_retroarch_exe", lambda *_: None)

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- no display
        pytest.skip("no display")
    root.withdraw()
    try:
        yield launcher.LauncherWindow(root)
    finally:
        root.destroy()


def test_a_bare_machine_finds_nothing(window):
    assert window.config.delta_folder is None
    assert window._rediscover() is False


def test_signing_into_dropbox_is_picked_up_without_a_restart(
    window, monkeypatch, tmp_path
):
    """The whole point. Before this, the answer never changed."""
    delta = tmp_path / "Dropbox" / "Delta Emulator"
    delta.mkdir(parents=True)
    monkeypatch.setattr(
        discovery, "find_delta_folder",
        lambda: discovery.Discovery("Delta Emulator folder", delta),
    )

    assert window._rediscover() is True
    assert window.config.delta_folder == delta


def test_what_was_found_reaches_the_entry_fields(window, monkeypatch, tmp_path):
    """Not decoration. The next action reads its paths back out of these
    fields, so finding a folder and leaving the box empty would throw it away
    again on the very next button press."""
    delta = tmp_path / "Dropbox" / "Delta Emulator"
    delta.mkdir(parents=True)
    monkeypatch.setattr(
        discovery, "find_delta_folder",
        lambda: discovery.Discovery("Delta Emulator folder", delta),
    )

    window._rediscover()
    window._drain()  # the queued update, as the event loop would run it

    assert window.delta_var.get() == str(delta)
    assert window._current_config().delta_folder == delta


def test_a_path_the_user_typed_is_never_overwritten(window, monkeypatch, tmp_path):
    """Discovery fills in blanks; it does not second-guess an explicit choice."""
    chosen = tmp_path / "somewhere else"
    chosen.mkdir()
    window.config = launcher.replace(window.config, delta_folder=chosen)
    monkeypatch.setattr(
        discovery, "find_delta_folder",
        lambda: discovery.Discovery("Delta Emulator folder", tmp_path / "guessed"),
    )

    window._rediscover()

    assert window.config.delta_folder == chosen
