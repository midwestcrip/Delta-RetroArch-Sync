"""Attaching to a RetroArch this program did not start.

**Sync and Play** called ``Popen`` unconditionally, so pressing it with
RetroArch already open started a second copy. The third naive-user test found
that, then asked the better question behind it: what happens to saves when
RetroArch is opened some other way? Nothing did -- the sync only ran around the
process this program launched itself. Finding and waiting on a running copy
answers both.

Tested against this very interpreter, which is the only process guaranteed to
exist while the test runs and the only one whose path is known for certain.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import processes  # noqa: E402

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="the Win32 process APIs are Windows-only"
)


@WINDOWS_ONLY
def test_it_can_see_the_processes_on_this_machine():
    found = processes.process_ids()

    assert len(found) > 1
    assert os.getpid() in found


@WINDOWS_ONLY
def test_it_reads_back_where_a_process_was_launched_from():
    found = processes.image_path(os.getpid())

    assert found is not None
    assert found.name.lower().startswith("python")


@WINDOWS_ONLY
def test_a_process_that_cannot_be_opened_is_not_an_error():
    """Ordinary, not exceptional: anything running as another user or at a
    higher integrity level refuses, and none of those are RetroArch."""
    assert processes.image_path(4) is None  # the System process


@WINDOWS_ONLY
def test_it_finds_a_running_copy_of_exactly_this_executable():
    running = processes.find_running(Path(sys.executable))

    assert running is not None


@WINDOWS_ONLY
def test_it_matches_on_the_whole_path_not_just_the_file_name(tmp_path):
    """Someone with a portable RetroArch beside an installed one has two
    different programs sharing a name. Attaching to the wrong one would wait on
    a window the user is not playing in."""
    impostor = tmp_path / Path(sys.executable).name
    impostor.write_bytes(b"")

    assert processes.find_running(impostor) is None


@WINDOWS_ONLY
def test_nothing_named_that_is_running():
    assert processes.find_running(Path(r"C:\nowhere\retroarch.exe")) is None


@WINDOWS_ONLY
def test_waiting_on_something_already_gone_returns_at_once():
    """Reported as ended rather than as a failure, because the caller's next
    move is the sync that follows RetroArch closing -- and a process that has
    already closed should not skip it."""
    assert processes.wait_for_exit(0x7FFFFFFF) is True


def test_it_reports_itself_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(processes.sys, "platform", "linux")

    assert not processes.supported()
    assert processes.process_ids() == []
    assert processes.image_path(1) is None
    assert processes.find_running(Path("/usr/bin/retroarch")) is None
    assert processes.wait_for_exit(1) is False
