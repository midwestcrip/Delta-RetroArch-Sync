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


def _point(original: str, stamp: str, size: int, label: str) -> "launcher.restore.RestorePoint":
    from delta_retroarch_synchronizer import restore

    parsed = restore.parse_backup_name(f"{original}.{stamp}.bak")
    assert parsed is not None
    return restore.RestorePoint(
        restore.Backup(Path(original), parsed[0], parsed[1], size), label
    )


def test_a_backup_row_says_which_side_and_when():
    row = _point(
        "GameSave-6b47bb75d16514b6a476aa0c73a683a2a4c18765-gameSave",
        "20260906T210845793346",
        2048,
        "Super Mario World",
    )
    assert launcher.backup_row(row) == (
        "Super Mario World",
        "Delta save",
        "2026-09-06 21:08:45 UTC",
        "2,048 B",
    )


def test_a_retroarch_row_is_labelled_by_its_filename():
    row = _point("Super Mario World.srm", "20260906T210845793346", 2048, "Super Mario World")
    assert launcher.backup_row(row)[1] == "RetroArch save"


def test_seconds_survive_into_the_table():
    """Two backups from one push differ only in the seconds.

    A push writes the save and then the record, a couple of seconds apart, so a
    table showing only minutes would present them as the same moment and give
    no way to tell which row is which.
    """
    first = launcher.backup_row(
        _point("Game.srm", "20260906T210843793346", 1, "Game")
    )[2]
    second = launcher.backup_row(
        _point("Game.srm", "20260906T210845793346", 1, "Game")
    )[2]
    assert first != second
