"""Tests for registering copied ROMs in a RetroArch playlist.

The gap this closes, found on 2026-09-06: Super Mario World was copied
correctly into the ROM folder and could not be found anywhere in RetroArch,
because RetroArch lists content from playlists and there were none.

Two properties matter more than the format details. A playlist is the user's
collection, so it must never be destroyed; and sync runs on every launch, so
registering an already-present game must change nothing at all.
"""

from __future__ import annotations

import json

from delta_retroarch_synchronizer import playlist

DB = "Nintendo - Super Nintendo Entertainment System"


def _rom(tmp_path, name="Super Mario World.sfc", data=b"\x00" * 64):
    roms = tmp_path / "roms"
    roms.mkdir(exist_ok=True)
    path = roms / name
    path.write_bytes(data)
    return path


def _read(tmp_path):
    return json.loads(
        playlist.playlist_path(tmp_path / "playlists", DB).read_text(encoding="utf-8")
    )


def test_creates_the_playlist_when_none_exists(tmp_path):
    rom = _rom(tmp_path)

    written = playlist.register(tmp_path / "playlists", DB, rom, "Super Mario World")

    assert written is not None
    assert written.name == f"{DB}.lpl"
    data = _read(tmp_path)
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["path"] == str(rom)
    assert item["label"] == "Super Mario World"
    assert item["db_name"] == f"{DB}.lpl"
    assert item["core_path"] == playlist.DETECT


def test_registering_twice_changes_nothing(tmp_path):
    """Sync runs on every launch; a no-op must really be a no-op."""
    rom = _rom(tmp_path)
    playlist.register(tmp_path / "playlists", DB, rom, "Super Mario World")
    before = playlist.playlist_path(tmp_path / "playlists", DB).read_bytes()

    again = playlist.register(tmp_path / "playlists", DB, rom, "Super Mario World")

    assert again is None
    assert playlist.playlist_path(tmp_path / "playlists", DB).read_bytes() == before


def test_existing_entries_and_settings_survive(tmp_path):
    """The playlist is the user's collection, not ours to rewrite."""
    playlists = tmp_path / "playlists"
    playlists.mkdir()
    playlist.playlist_path(playlists, DB).write_text(
        json.dumps(
            {
                "version": "1.5",
                "sort_mode": 2,
                "scan_content_dir": "D:\\games",
                "items": [
                    {"path": "D:\\games\\Chrono Trigger.sfc", "label": "Chrono Trigger"}
                ],
            }
        ),
        encoding="utf-8",
    )
    rom = _rom(tmp_path)

    playlist.register(playlists, DB, rom, "Super Mario World")

    data = _read(tmp_path)
    labels = [item["label"] for item in data["items"]]
    assert labels == ["Chrono Trigger", "Super Mario World"]
    # An unknown key from a newer RetroArch must round-trip untouched.
    assert data["scan_content_dir"] == "D:\\games"
    assert data["sort_mode"] == 2


def test_a_playlist_that_will_not_parse_is_left_alone(tmp_path):
    """Better a missing menu entry than a discarded collection."""
    playlists = tmp_path / "playlists"
    playlists.mkdir()
    target = playlist.playlist_path(playlists, DB)
    target.write_text("{ this is not json", encoding="utf-8")
    rom = _rom(tmp_path)

    assert playlist.register(playlists, DB, rom, "Super Mario World") is None
    assert target.read_text(encoding="utf-8") == "{ this is not json"


def test_path_matching_ignores_case_and_separator(tmp_path):
    """Windows spells the same path several ways; none of them is a new game."""
    rom = _rom(tmp_path)
    playlists = tmp_path / "playlists"
    playlists.mkdir()
    playlist.playlist_path(playlists, DB).write_text(
        json.dumps(
            {"items": [{"path": str(rom).replace("\\", "/").upper(), "label": "x"}]}
        ),
        encoding="utf-8",
    )

    assert playlist.register(playlists, DB, rom, "Super Mario World") is None


def test_a_missing_rom_is_not_registered(tmp_path):
    missing = tmp_path / "roms" / "Nothing.sfc"

    assert playlist.register(tmp_path / "playlists", DB, missing, "Nothing") is None


def test_crc32_is_retroarchs_form(tmp_path):
    rom = _rom(tmp_path, data=b"123456789")

    value = playlist.crc32_of(rom)

    # The standard CRC-32 check value for "123456789".
    assert value == "CBF43926|crc"


def test_two_games_land_in_one_playlist(tmp_path):
    first = _rom(tmp_path, "Super Mario World.sfc")
    second = _rom(tmp_path, "Chrono Trigger.sfc", data=b"\x01" * 64)
    playlists = tmp_path / "playlists"

    playlist.register(playlists, DB, first, "Super Mario World")
    playlist.register(playlists, DB, second, "Chrono Trigger")

    assert [item["label"] for item in _read(tmp_path)["items"]] == [
        "Super Mario World",
        "Chrono Trigger",
    ]
