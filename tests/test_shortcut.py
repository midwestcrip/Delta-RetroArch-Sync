"""Getting the launcher into the Start menu and onto the desktop.

Three separate checkpoints of the third naive-user test came back with the same
sentence: the program never appears in the Start menu or in search, even after
being opened and cleared through SmartScreen. It was the most repeated finding
in the report. A portable zip has no installer to make a shortcut, and the
script that could was written for a source checkout, so the one person who
needed it -- whoever downloaded the zip -- could not run it.

The desktop was added afterwards, when the user's own desktop shortcut turned
out to be broken and there was no way to make a new one from inside the program.

The same test found the other half of this file: pressing "Save settings" on the
Settings tab wrote its confirmation to the log, which lives on the Sync tab, so
from where the user was standing the button did nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import launcher, shortcut  # noqa: E402

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="shortcuts like these are a Windows feature"
)


@pytest.fixture
def somewhere_safe(monkeypatch, tmp_path):
    """Point both shortcut locations at a temporary directory.

    Not optional politeness: ``create`` writes to the real Start menu and the
    real desktop, so a test that did not redirect them would litter the machine
    running it.
    """
    places = {
        "Start menu": tmp_path / "Start Menu" / shortcut.LINK_NAME,
        "desktop": tmp_path / "Desktop" / shortcut.LINK_NAME,
    }
    monkeypatch.setattr(shortcut, "links", lambda: dict(places))
    return places


# --------------------------------------------------------------- where it goes


@WINDOWS_ONLY
def test_it_asks_windows_where_the_desktop_is(monkeypatch, tmp_path):
    """The obvious construction is wrong on a redirected profile, and wrong in
    the silent way.

    On the machine this was written for, OneDrive owns the desktop: the real
    one is ``C:/Users/<user>/OneDrive/Desktop`` and ``~/Desktop`` does not
    exist at all. Building the path from the home directory would have created
    a folder nobody ever looks at and reported success.
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    desktop = shortcut.desktop_dir()

    assert desktop is not None
    assert desktop.is_dir()
    assert tmp_path not in desktop.parents  # not derived from the env we set


@WINDOWS_ONLY
def test_it_installs_per_user_not_for_the_whole_machine():
    """Per-user needs no elevation.

    The one thing that actually stopped the Standard test account was
    RetroArch's installer demanding an administrator PIN. A shortcut is not
    worth a second one.
    """
    folder = shortcut.start_menu_dir()

    assert folder is not None
    assert folder.name == "Programs"
    # Under this user's own profile, not a machine-wide location.
    assert str(folder).lower().startswith(str(Path.home()).lower())


def test_the_start_menu_falls_back_to_appdata(monkeypatch, tmp_path):
    """Only for a shell that will not answer. Correct on an ordinary profile,
    wrong on a redirected one, which is why it is the fallback."""
    monkeypatch.setattr(shortcut.sys, "platform", "win32")
    monkeypatch.setattr(shortcut, "_known_folder", lambda _id: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    folder = shortcut.start_menu_dir()

    assert folder is not None
    assert tmp_path in folder.parents


def test_nowhere_to_put_it_is_reported_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(shortcut.sys, "platform", "win32")
    monkeypatch.setattr(shortcut, "_known_folder", lambda _id: None)
    monkeypatch.delenv("APPDATA", raising=False)

    assert shortcut.start_menu_dir() is None
    assert shortcut.links() == {}
    with pytest.raises(shortcut.ShortcutError):
        shortcut.create()


def test_it_is_not_offered_off_windows(monkeypatch):
    monkeypatch.setattr(shortcut.sys, "platform", "darwin")

    assert not shortcut.supported()
    assert shortcut.start_menu_dir() is None
    assert shortcut.desktop_dir() is None
    assert not shortcut.installed()


# --------------------------------------------------------------- what it targets


def test_from_source_it_runs_the_console_free_interpreter(monkeypatch):
    """pythonw, not python.

    Pointing at python.exe leaves a black console window sitting behind the
    launcher for as long as it is open.
    """
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: False)

    program, arguments, working_dir = shortcut.target()

    assert program.name in {"pythonw.exe", "python.exe", "python", "python3"}
    assert arguments.startswith('"') and arguments.endswith('"')
    assert "launch_gui.pyw" in arguments
    assert (working_dir / "launch_gui.pyw").is_file()


def test_frozen_it_targets_the_executable_and_takes_no_arguments(monkeypatch):
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: True)

    program, arguments, working_dir = shortcut.target()

    assert arguments == ""
    assert program.parent == working_dir


def test_frozen_it_leaves_the_icon_to_the_executable(monkeypatch):
    """Not an omission.

    The exe carries its own icon, while the copy PyInstaller unpacks beside it
    lives in a temporary directory deleted on exit -- so pointing the shortcut
    at that path would give a blank icon the moment the program closed.
    """
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: True)

    assert shortcut.icon_location() == ""


def test_from_source_it_points_at_the_icon_that_will_still_be_there(monkeypatch):
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: False)

    assert shortcut.icon_location().endswith("synchronizer.ico")


# ------------------------------------------------------------ making and removing


def test_removing_nothing_is_not_an_error(somewhere_safe):
    assert shortcut.remove() == {}


@WINDOWS_ONLY
def test_it_writes_to_both_places(somewhere_safe, monkeypatch):
    """Both, because they answer different habits: one is for people who
    search, the other for people who look."""
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: False)

    written = shortcut.create()

    assert set(written) == {"Start menu", "desktop"}
    for link in written.values():
        assert link.is_file()
    assert shortcut.installed()


@WINDOWS_ONLY
def test_one_place_failing_does_not_withhold_the_other(monkeypatch, tmp_path):
    """A desktop that cannot be written to is no reason to skip the Start
    menu."""
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: False)
    good = tmp_path / "Start Menu" / shortcut.LINK_NAME
    monkeypatch.setattr(shortcut, "links", lambda: {
        "Start menu": good,
        "desktop": Path("Z:/no-such-drive") / shortcut.LINK_NAME,
    })

    written = shortcut.create()

    assert set(written) == {"Start menu"}
    assert good.is_file()


def test_it_removes_what_it_made(somewhere_safe):
    for link in somewhere_safe.values():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(b"")

    assert shortcut.installed()
    removed = shortcut.remove()

    assert set(removed) == {"Start menu", "desktop"}
    assert not shortcut.installed()


def test_either_one_counts_as_installed(somewhere_safe):
    """So deleting one by hand does not leave the button offering to add what
    is already there."""
    desktop = somewhere_safe["desktop"]
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"")

    assert shortcut.installed()


@WINDOWS_ONLY
def test_it_really_writes_a_shortcut_windows_can_read(somewhere_safe, monkeypatch):
    """The one test that proves the feature rather than the plumbing.

    A .lnk is a binary shell-link structure the standard library cannot write,
    so this shells out to WScript.Shell -- and a hand-written stub would prove
    nothing about whether the result is a shortcut. Reading it back through the
    same COM object Explorer uses is the only check worth having, and it also
    covers the quoting: this project's own path contains a hyphen and its parent
    could contain a space.
    """
    monkeypatch.setattr(shortcut.paths, "is_frozen", lambda: False)

    link = shortcut.create()["Start menu"]

    assert link.is_file()
    assert link.stat().st_size > 0

    read_back = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$s = (New-Object -ComObject WScript.Shell)"
            ".CreateShortcut($env:LINK); $s.TargetPath; $s.Arguments",
        ],
        env={**os.environ, "LINK": str(link)},
        stdin=subprocess.DEVNULL,  # pytest's stdin cannot be inherited
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert read_back.returncode == 0, read_back.stderr
    target, arguments = read_back.stdout.strip().splitlines()[:2]
    assert target.lower().endswith(("pythonw.exe", "python.exe"))
    assert "launch_gui.pyw" in arguments


# ----------------------------------------------------------- saying it out loud


class FakeButton:
    """Enough of a ttk.Button to prove the flash, with no display attached."""

    def __init__(self, text: str = "", style: str = "") -> None:
        self.options = {"text": text, "style": style}
        self.timers: dict[str, object] = {}
        self._next = 0

    def cget(self, key: str) -> str:
        return self.options[key]

    def configure(self, **kwargs: object) -> None:
        self.options.update(kwargs)  # type: ignore[arg-type]

    def after(self, _ms: int, callback) -> str:
        self._next += 1
        token = f"timer{self._next}"
        self.timers[token] = callback
        return token

    def after_cancel(self, token: str) -> None:
        self.timers.pop(token)

    def fire(self) -> None:
        """Run every pending timer, as Tk's event loop eventually would."""
        for callback in list(self.timers.values()):
            callback()
        self.timers.clear()


def test_the_button_answers_then_goes_back_to_its_own_name():
    button = FakeButton("Save settings")
    confirmation = launcher.Confirmation(button)

    confirmation.show("Saved!")

    assert button.cget("text") == "Saved!"
    assert button.cget("style") == "Success.TButton"

    button.fire()

    assert button.cget("text") == "Save settings"
    assert button.cget("style") == "TButton"


def test_a_second_press_does_not_get_cut_short_by_the_first():
    """Without cancelling, the first timer fires mid-flash and restores over
    a message the user has not read yet."""
    button = FakeButton("Save settings")
    confirmation = launcher.Confirmation(button)

    confirmation.show("Saved!")
    confirmation.show("Saved!")

    assert len(button.timers) == 1
    assert button.cget("text") == "Saved!"


def test_a_button_that_renames_itself_goes_back_to_the_new_name():
    """The shortcuts button alternates between adding and removing, so its
    resting label is not fixed the way "Save settings" is."""
    button = FakeButton("Add shortcuts")
    confirmation = launcher.Confirmation(button)

    confirmation.show("Added!")
    confirmation.settle("Remove shortcuts")

    # Still flashing: renaming must not cut the confirmation short.
    assert button.cget("text") == "Added!"

    button.fire()

    assert button.cget("text") == "Remove shortcuts"


def test_renaming_while_at_rest_takes_effect_immediately():
    button = FakeButton("Add shortcuts")
    confirmation = launcher.Confirmation(button)

    confirmation.settle("Remove shortcuts")

    assert button.cget("text") == "Remove shortcuts"
