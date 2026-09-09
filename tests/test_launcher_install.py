"""The Install button on the Save states tab, wired end to end.

``test_savestate_install`` covers the writing. This covers the wiring, which is
where the two failures that actually matter live:

- **It must refuse while RetroArch is running.** RetroArch dumps the loaded
  game's SRAM when it closes, straight over anything installed underneath it.
  That failure is silent and looks exactly like the recovery not having worked,
  so it is checked before anything is written rather than explained afterwards.
- **It must ask before overwriting.** The confirmation names the exact file and
  both sizes, and answering no must leave the save alone.

A real Tk window, for the reason ``test_launcher_recovery`` gives: the bug this
kind of code has is in the wiring between the config, the selection and the
handler, and stubbing any of those tests the stub.
"""

from __future__ import annotations

import json
import struct
import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import config as config_module  # noqa: E402
from delta_retroarch_synchronizer import (  # noqa: E402
    discovery,
    inspect as inspect_module,
    launcher,
    naming,
    processes,
    savestate,
    sync as sync_module,
    systems,
)

SHA1 = "0862ec35b24de5c7e2dcb88c9eea0873110d755c"
UUID = "1B4E28BA-2FA1-11D2-883F-0016D3CCA427"
GAME = "Pokemon: Platinum Version"
SRAM = bytes((i * 13) % 256 for i in range(512))


def melonds_state(sram: bytes) -> bytes:
    def section(magic: bytes, body: bytes) -> bytes:
        return magic + struct.pack("<I", 16 + len(body)) + b"\x00" * 8 + body

    cart = struct.pack("<IIII", 0, 0, 0, len(sram)) + sram + struct.pack("<BIB", 0, 0, 0)
    body = section(b"NDSG", b"\x00" * 64) + section(b"NDCS", cart)
    return (
        b"MELN"
        + struct.pack("<HH", 9, 0)
        + struct.pack("<I", 16 + len(body))
        + b"\x00" * 4
        + body
    )


@pytest.fixture
def delta_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Delta Emulator"
    folder.mkdir()
    (folder / f"Game-{SHA1}").write_text(
        json.dumps(
            {
                "type": "Game",
                "identifier": SHA1,
                "record": {"name": GAME, "type": "com.rileytestut.delta.game.ds"},
                "files": [],
                "relationships": {},
            }
        ),
        encoding="utf-8",
    )
    (folder / f"SaveState-{UUID}").write_text(
        json.dumps(
            {
                "type": "SaveState",
                "identifier": UUID,
                "record": {"name": "Slot 1"},
                "files": [],
                "relationships": {"game": {"type": "Game", "identifier": SHA1}},
            }
        ),
        encoding="utf-8",
    )
    (folder / f"SaveState-{UUID}-saveState").write_bytes(melonds_state(SRAM))
    (folder / f"GameSave-{SHA1}-gameSave").write_bytes(SRAM)
    return folder


@pytest.fixture
def window(monkeypatch, tmp_path, delta_folder):
    """A launcher whose Save states tab has one recoverable DS state."""
    monkeypatch.setattr(
        config_module,
        "load",
        lambda path=None: config_module.Config(
            sync_on_open=False,
            shortcuts_offered=True,
            delta_folder=delta_folder,
            retroarch_exe=tmp_path / "retroarch.exe",
        ),
    )
    monkeypatch.setattr(config_module, "save", lambda cfg, path=None: tmp_path / "c.toml")
    monkeypatch.setattr(discovery, "find_retroarch_exe", lambda *_: None)
    # Nothing running, unless a test says otherwise.
    monkeypatch.setattr(processes, "find_by_name", lambda name: [])

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- no display
        pytest.skip("no display")
    root.withdraw()
    try:
        yield launcher.LauncherWindow(root)
    finally:
        root.destroy()


@pytest.fixture
def prepared(monkeypatch, window, tmp_path):
    """Stand in for ``_prepare``, which is discovery's job and tested there."""
    save_dir = tmp_path / "RetroArch" / "saves"
    save_dir.mkdir(parents=True)
    paths = sync_module.Paths(
        delta_folder=window.config.delta_folder,
        retroarch_config=tmp_path / "retroarch.cfg",
        save_dir=save_dir,
        state_dir=tmp_path / "state",
    )
    entries = inspect_module.collect_games(window.config.delta_folder)
    monkeypatch.setattr(
        launcher.LauncherWindow,
        "_prepare",
        lambda self: (paths, entries, {"melonDS DS"}, False, None, None),
    )
    return paths


@pytest.fixture
def answers(monkeypatch):
    """Every dialog the handler can raise, recorded rather than shown."""
    seen: dict[str, list[str]] = {"info": [], "warning": [], "error": [], "ask": []}

    def record(kind, *, returns=None):
        def handler(_title, message, **_kwargs):
            seen[kind].append(message)
            return returns

        return handler

    monkeypatch.setattr(launcher.messagebox, "showinfo", record("info"))
    monkeypatch.setattr(launcher.messagebox, "showwarning", record("warning"))
    monkeypatch.setattr(launcher.messagebox, "showerror", record("error"))
    seen["yes"] = [True]  # type: ignore[list-item]

    def ask(_title, message, **_kwargs):
        seen["ask"].append(message)
        return seen["yes"][0]

    monkeypatch.setattr(launcher.messagebox, "askyesno", ask)
    return seen


def select_the_state(window) -> None:
    window.on_refresh_states()
    assert window.found_states, "the fixture's save state was not listed"
    window.state_list.selection_set("0")


def test_the_tab_offers_installing_not_just_recovering(window) -> None:
    assert hasattr(window, "install_confirmation")


def test_with_nothing_selected_it_says_so_and_writes_nothing(
    window, prepared, answers
) -> None:
    window.on_install_state()
    assert answers["info"], "no explanation was given"
    assert "Pick a save state" in answers["info"][0]
    assert list(prepared.save_dir.rglob("*")) == []


def test_it_refuses_while_RetroArch_is_running(
    window, prepared, answers, monkeypatch
) -> None:
    """The guard that matters. RetroArch writes the loaded game's save when it
    closes, so installing underneath it is undone without a word."""
    monkeypatch.setattr(
        processes, "find_by_name", lambda name: [(4321, Path("C:/RetroArch.exe"))]
    )
    select_the_state(window)
    window.on_install_state()

    assert answers["warning"], "installing under a running RetroArch was allowed"
    assert "would not stick" in answers["warning"][0]
    assert not answers["ask"], "it asked before checking whether this could work"
    assert list(prepared.save_dir.rglob("*")) == []


def test_answering_no_leaves_the_save_alone(window, prepared, answers) -> None:
    target = prepared.save_dir / naming.save_filename(GAME, "srm")
    target.write_bytes(b"do not touch me")
    answers["yes"][0] = False

    select_the_state(window)
    window.on_install_state()

    assert answers["ask"], "it overwrote a save without asking"
    assert target.read_bytes() == b"do not touch me"


def test_the_question_names_the_file_and_both_sizes(window, prepared, answers) -> None:
    target = prepared.save_dir / naming.save_filename(GAME, "srm")
    target.write_bytes(b"x" * 512)
    answers["yes"][0] = False

    select_the_state(window)
    window.on_install_state()

    question = answers["ask"][0]
    assert str(target) in question
    assert "512 B" in question


def test_a_size_that_does_not_match_is_pointed_out_in_the_question(
    window, prepared, answers
) -> None:
    target = prepared.save_dir / naming.save_filename(GAME, "srm")
    target.write_bytes(b"x" * 8192)
    answers["yes"][0] = False

    select_the_state(window)
    window.on_install_state()

    assert "different size" in answers["ask"][0]


def test_a_constructed_target_is_flagged_as_a_guess(window, prepared, answers) -> None:
    """Nothing to find, so the path is worked out -- and only right if the ROM
    is named the way this tool names it. That has to be said before writing."""
    answers["yes"][0] = False
    select_the_state(window)
    window.on_install_state()

    question = answers["ask"][0]
    assert "No existing save was found" in question
    assert "nothing" in question


def test_the_save_is_installed_and_the_old_one_backed_up(
    window, prepared, answers
) -> None:
    target = prepared.save_dir / naming.save_filename(GAME, "srm")
    target.write_bytes(b"the previous save")

    select_the_state(window)
    window.on_install_state()

    assert target.read_bytes() == SRAM
    backups = list(prepared.backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"the previous save"


def test_installing_where_there_was_nothing_needs_no_backup(
    window, prepared, answers
) -> None:
    select_the_state(window)
    window.on_install_state()

    target = prepared.save_dir / naming.save_filename(GAME, "srm")
    assert target.read_bytes() == SRAM
    assert not prepared.backup_dir.exists() or not list(
        prepared.backup_dir.glob("*.bak")
    )


def test_an_N64_state_is_turned_away_before_any_of_this(
    window, prepared, answers, delta_folder
) -> None:
    """Two reasons, and the first one is enough: there is no battery save
    inside a mupen64plus state to install."""
    (delta_folder / f"Game-{SHA1}").write_text(
        json.dumps(
            {
                "type": "Game",
                "identifier": SHA1,
                "record": {
                    "name": "Paper Mario",
                    "type": systems.SYSTEMS["n64"].delta_type,
                },
                "files": [],
                "relationships": {},
            }
        ),
        encoding="utf-8",
    )
    (delta_folder / f"SaveState-{UUID}-saveState").write_bytes(
        b"\x1f\x8b\x08\x00" + b"\x00" * 64
    )

    select_the_state(window)
    window.on_install_state()

    assert answers["info"], "an N64 state was not explained"
    assert "no battery save inside an N64 save state" in answers["info"][0]
    assert list(prepared.save_dir.rglob("*")) == []


def test_the_readable_formats_are_named_when_one_is_not(
    window, prepared, answers, delta_folder
) -> None:
    """The message used to say only DS could be read, which stopped being true
    when the other four landed.

    Reached through a system this tool does not know -- Delta emulates more
    than the six that are wired up here, and one of those states is the
    remaining way to arrive at "cannot read this format" now that five of the
    six can be read."""
    (delta_folder / f"Game-{SHA1}").write_text(
        json.dumps(
            {
                "type": "Game",
                "identifier": SHA1,
                "record": {
                    "name": "Sonic the Hedgehog",
                    "type": "com.rileytestut.delta.game.genesis",
                },
                "files": [],
                "relationships": {},
            }
        ),
        encoding="utf-8",
    )
    (delta_folder / f"SaveState-{UUID}-saveState").write_bytes(b"XYZZY" + b"\x00" * 64)

    select_the_state(window)
    window.on_install_state()

    assert answers["info"], "an unreadable format was not explained"
    message = answers["info"][0]
    for core in savestate.READABLE_CORES:
        assert core in message
    assert list(prepared.save_dir.rglob("*")) == []
