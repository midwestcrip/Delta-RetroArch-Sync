"""Tests for the launcher's path resolution and log colouring.

Both of the things covered here run without a Tk display -- ``resolve_paths`` is
a module-level function and ``_level_for`` a static method, deliberately, so the
window's startup behaviour and its log severities are testable at all.

The explanations matter as much as the paths. Both naive-user tests found that a
first run said only "not found -- set it below", which is unactionable for
someone who has never heard of Delta's Dropbox folder, while discovery had
composed a usable explanation and the window threw it away.
"""

from __future__ import annotations

from pathlib import Path

from delta_retroarch_synchronizer import launcher, sync
from delta_retroarch_synchronizer.config import Config
from delta_retroarch_synchronizer.discovery import Discovery

NO_DROPBOX = "No Dropbox install found. Install the Dropbox desktop client."
NO_RETROARCH = "RetroArch config not found. Install RetroArch and launch it once."


def _stub(monkeypatch, *, delta: Discovery, retroarch: Discovery, exe: Path | None):
    monkeypatch.setattr(launcher.discovery, "find_delta_folder", lambda: delta)
    monkeypatch.setattr(launcher.discovery, "find_retroarch_config", lambda: retroarch)
    monkeypatch.setattr(launcher.discovery, "find_retroarch_exe", lambda _cfg: exe)


def test_explains_every_path_it_could_not_find(monkeypatch):
    """A first run on a bare machine says what to do, not just what is missing."""
    _stub(
        monkeypatch,
        delta=Discovery("Delta Emulator folder", None, NO_DROPBOX),
        retroarch=Discovery("retroarch.cfg", None, NO_RETROARCH),
        exe=None,
    )

    config, notes = launcher.resolve_paths(Config())

    assert notes == [NO_DROPBOX, NO_RETROARCH]
    assert config.delta_folder is None
    assert config.retroarch_config is None


def test_no_notes_when_everything_is_found(monkeypatch):
    """Nothing to explain, and the discovered paths are filled in."""
    delta = Path("D:/Dropbox/Delta Emulator")
    cfg = Path("D:/RetroArch/retroarch.cfg")
    exe = Path("D:/RetroArch/retroarch.exe")
    _stub(
        monkeypatch,
        delta=Discovery("Delta Emulator folder", delta),
        retroarch=Discovery("retroarch.cfg", cfg),
        exe=exe,
    )

    config, notes = launcher.resolve_paths(Config())

    assert notes == []
    assert config.delta_folder == delta
    assert config.retroarch_config == cfg
    assert config.retroarch_exe == exe
    # Derived from the config's location rather than discovered separately.
    assert config.retroarch_rom_dir == cfg.parent / "roms"


def test_configured_paths_are_left_alone(monkeypatch):
    """A path set in config.toml is never second-guessed, and never explained.

    Discovery would have failed here. Reporting that would be actively wrong:
    the user has already told us where the folder is.
    """
    mine = Path("E:/somewhere/else/Delta Emulator")
    _stub(
        monkeypatch,
        delta=Discovery("Delta Emulator folder", None, NO_DROPBOX),
        retroarch=Discovery("retroarch.cfg", None, NO_RETROARCH),
        exe=None,
    )

    config, notes = launcher.resolve_paths(Config(delta_folder=mine))

    assert config.delta_folder == mine
    assert NO_DROPBOX not in notes
    assert notes == [NO_RETROARCH]


def test_partial_discovery_explains_only_the_gap(monkeypatch):
    """Dropbox is there but Delta has not synced: one note, not two."""
    detail = (
        "Dropbox found at D:/Dropbox, but no 'Delta Emulator' folder in it. "
        "In Delta: Settings -> Delta Sync -> connect Dropbox and let one full "
        "sync finish."
    )
    cfg = Path("D:/RetroArch/retroarch.cfg")
    _stub(
        monkeypatch,
        delta=Discovery("Delta Emulator folder", None, detail),
        retroarch=Discovery("retroarch.cfg", cfg),
        exe=Path("D:/RetroArch/retroarch.exe"),
    )

    config, notes = launcher.resolve_paths(Config())

    assert notes == [detail]
    assert config.retroarch_config == cfg


# --------------------------------------------------------------- log severity

# The log is colour-coded because a wall of identical grey text hid the one line
# that mattered. Which colour a line gets is decided from the structured outcome
# rather than its wording, and these pin the distinction that is easiest to lose:
# a push that was *refused* is amber, a push that was *attempted and broke* is
# red. Both are `Action.PUSH` with `applied=False`, so only `failed` separates
# them.


def _outcome(action, *, applied=False, failed=False):
    return sync.Outcome("Game", action, "detail", applied=applied, failed=failed)


def test_a_push_that_broke_is_red():
    assert launcher.LauncherWindow._level_for(
        _outcome(sync.Action.PUSH, failed=True)
    ) == "error"


def test_a_push_that_was_only_refused_is_amber():
    """No Dropbox auth is a thing to fix, not a thing that went wrong."""
    assert launcher.LauncherWindow._level_for(_outcome(sync.Action.PUSH)) == "warn"


def test_a_push_that_worked_is_green():
    assert launcher.LauncherWindow._level_for(
        _outcome(sync.Action.PUSH, applied=True)
    ) == "ok"


def test_a_conflict_is_red():
    assert launcher.LauncherWindow._level_for(_outcome(sync.Action.CONFLICT)) == "error"


def test_a_skip_is_amber():
    """Missing core, unverified clock format: worth seeing, not a failure."""
    assert launcher.LauncherWindow._level_for(_outcome(sync.Action.SKIPPED)) == "warn"


def test_nothing_to_do_is_muted():
    assert launcher.LauncherWindow._level_for(_outcome(sync.Action.NOTHING)) == "muted"


def test_failure_outranks_an_applied_flag():
    """Belt and braces: a red line must never be downgraded by a stale flag."""
    assert launcher.LauncherWindow._level_for(
        _outcome(sync.Action.PULL, applied=True, failed=True)
    ) == "error"
