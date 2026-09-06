"""Tests for the launcher's path resolution.

Only ``resolve_paths`` is covered: it is deliberately a module-level function
rather than a method so it can run without a Tk display, which is what makes the
window's startup behaviour testable at all.

The explanations matter as much as the paths. Both naive-user tests found that a
first run said only "not found -- set it below", which is unactionable for
someone who has never heard of Delta's Dropbox folder, while discovery had
composed a usable explanation and the window threw it away.
"""

from __future__ import annotations

from pathlib import Path

from delta_retroarch_synchronizer import launcher
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
