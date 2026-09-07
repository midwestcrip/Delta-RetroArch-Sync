"""The setup checklist, and telling apart the two ways a thing can be missing.

Both come from the third naive-user test. It judged the empty state "still a
little undercooked": the advice was a paragraph in a scrolling log, and pressing
**Check status** answered only "Delta folder is not set or missing". Separately,
the window told a user to install the Dropbox desktop client while Dropbox was
installed and running -- it was signed out, which looks identical from the
outside because ``info.json`` only appears once an account is linked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import discovery, guide  # noqa: E402

NOTHING_DONE = guide.Progress()
ALL_DONE = guide.Progress(
    dropbox_installed=True,
    dropbox_signed_in=True,
    delta_folder_found=True,
    retroarch_installed=True,
    retroarch_launched=True,
    cores_installed=3,
)


# ------------------------------------------------------------- the checklist


def test_the_steps_are_in_the_order_they_have_to_be_done_in():
    """Dependency order, not importance order.

    Delta cannot sync to a Dropbox nobody has signed into, and RetroArch does
    not write the config this program reads until it has been opened once.
    """
    titles = [step.title for step in guide.setup_steps(NOTHING_DONE)]

    assert titles.index("Install Dropbox and sign in") < titles.index(
        "Turn on Delta Sync on your phone"
    )
    assert titles.index("Install RetroArch and open it once") < titles.index(
        "Add the cores for the systems you play"
    )
    assert titles[-1] == "Press Sync and Play"


def test_a_bare_machine_has_nothing_ticked():
    steps = guide.setup_steps(NOTHING_DONE)

    assert not any(step.done for step in steps)
    assert all(step.marker == guide.TODO for step in steps)


def test_a_finished_machine_ticks_everything_it_can():
    """Everything except pressing the button, which is not a setup step you
    complete once -- it is the thing you came here to do."""
    steps = guide.setup_steps(ALL_DONE)

    assert [step.done for step in steps] == [True, True, True, True, False]


def test_the_marker_is_redundant_with_the_colour():
    """So the list still reads correctly in a screenshot, in monochrome, or to
    someone who cannot separate the two greens."""
    done, todo = guide.setup_steps(ALL_DONE)[0], guide.setup_steps(NOTHING_DONE)[0]

    assert done.marker == guide.DONE
    assert todo.marker == guide.TODO
    assert done.marker != todo.marker


def test_it_names_the_first_thing_still_to_do():
    """The point of the whole window: on a machine that is half set up, one
    step is worth reading and the other four are not."""
    half = guide.Progress(dropbox_installed=True, dropbox_signed_in=True)

    following = guide.next_step(guide.setup_steps(half))

    assert following is not None
    assert following.title == "Turn on Delta Sync on your phone"


def test_a_later_step_being_done_does_not_skip_an_earlier_one():
    """RetroArch can perfectly well be installed before Delta has ever synced.
    The next thing to do is still the earliest unfinished one."""
    odd = guide.Progress(retroarch_installed=True, retroarch_launched=True)

    following = guide.next_step(guide.setup_steps(odd))

    assert following is not None
    assert following.title == "Install Dropbox and sign in"


def test_every_step_says_what_to_do_not_just_what_is_wrong():
    """The complaint from both earlier tests was messages that state a fact
    the reader cannot act on."""
    for step in guide.setup_steps(NOTHING_DONE):
        assert step.body.strip()
        assert len(step.body) > 40


def test_the_retroarch_step_warns_about_the_download_page():
    """Raised by the third test: RetroArch's download page carries
    advertisements dressed as download buttons, one of which fires on the real
    link."""
    steps = {step.title: step for step in guide.setup_steps(NOTHING_DONE)}

    body = steps["Install RetroArch and open it once"].body

    assert "ad" in body.lower()


def test_the_notes_cover_the_limits_worth_knowing_before_starting():
    joined = " ".join(guide.NOTES).lower()

    assert "save states" in joined
    assert "backups" in joined


# ------------------------------------------- installed, or merely not set up


def test_dropbox_signed_out_is_not_dropbox_missing(monkeypatch, tmp_path):
    """The exact case the third test hit: told to install what it already had.

    Sending someone to a download page when the answer is a sign-in button
    three feet away is worse than saying nothing.
    """
    monkeypatch.setattr(discovery, "dropbox_roots", lambda: [])
    monkeypatch.setattr(discovery, "dropbox_client", lambda: tmp_path / "Dropbox.exe")

    found = discovery.find_delta_folder()

    assert not found.found
    assert "not signed in" in found.detail
    assert "Install the Dropbox desktop client" not in found.detail


def test_dropbox_really_missing_says_install_it(monkeypatch):
    monkeypatch.setattr(discovery, "dropbox_roots", lambda: [])
    monkeypatch.setattr(discovery, "dropbox_client", lambda: None)

    found = discovery.find_delta_folder()

    assert not found.found
    assert "not installed" in found.detail
    assert "dropbox.com" in found.detail


def test_dropbox_signed_in_without_delta_points_at_the_phone(monkeypatch, tmp_path):
    """A third distinct answer, and the only one whose fix is on the phone."""
    monkeypatch.setattr(discovery, "dropbox_roots", lambda: [tmp_path])

    found = discovery.find_delta_folder()

    assert not found.found
    assert "Delta Sync" in found.detail


def test_the_client_is_found_even_where_it_is_not_expected(monkeypatch, tmp_path):
    """Dropbox installs per-user by default, but the machine this was found on
    had it under 32-bit Program Files, so all the locations are checked."""
    client = tmp_path / "Dropbox" / "Client" / "Dropbox.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nowhere"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))

    assert discovery.dropbox_client() == client


def test_retroarch_never_launched_is_not_retroarch_missing(monkeypatch, tmp_path):
    """RetroArch writes its config on first launch, so an install that has
    never been opened looks exactly like no install at all."""
    exe = tmp_path / "retroarch.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(discovery, "find_retroarch_exe", lambda *_: exe)
    monkeypatch.setattr(discovery, "_config_candidates", list)

    found = discovery.find_retroarch_config()

    assert not found.found
    assert "has never been launched" in found.detail
    assert str(tmp_path) in found.detail


def test_retroarch_really_missing_says_where_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "find_retroarch_exe", lambda *_: None)
    monkeypatch.setattr(discovery, "_config_candidates", list)

    found = discovery.find_retroarch_config()

    assert not found.found
    assert "not installed" in found.detail
    assert "retroarch.com" in found.detail
