"""Tests for detecting that Delta has stopped writing to this folder.

The failure being caught: Delta signed into a different Dropbox account than the
one this folder belongs to. Account B leaves no trace in account A, so there is
nothing to detect directly -- the only observable is that Delta stops writing,
and the only moment that observation is worth anything is a sync that found
nothing to do.
"""

from __future__ import annotations

import json

from delta_retroarch_synchronizer import health
from delta_retroarch_synchronizer.manifest import APPLE_EPOCH_OFFSET

HOUR = 3600.0
DAY = 24 * HOUR
NOW = 1_800_000_000.0


def _record(folder, name, type_, identifier, fields):
    """Write a Harmony record the way Delta lays them out."""
    (folder / name).write_text(
        json.dumps({"type": type_, "identifier": identifier, "record": fields}),
        encoding="utf-8",
    )


def _dated(unix: float) -> dict:
    """Delta stores Core Data reference dates, not Unix timestamps."""
    return {"modifiedDate": unix - APPLE_EPOCH_OFFSET, "sha1": "abc"}


def test_finds_the_newest_write_across_records(tmp_path):
    _record(tmp_path, "GameSave-a", "GameSave", "a", _dated(NOW - 5 * DAY))
    _record(tmp_path, "Cheat-b", "Cheat", "b", _dated(NOW - 2 * HOUR))
    _record(tmp_path, "GameSave-c", "GameSave", "c", _dated(NOW - 30 * DAY))

    activity = health.delta_activity(tmp_path)

    assert activity.records == 3
    assert activity.dated == 3
    assert activity.age_seconds(NOW) == 2 * HOUR


def test_undated_records_are_counted_but_not_used(tmp_path):
    """Game and GameCollection records carry no modifiedDate; that is normal."""
    _record(tmp_path, "Game-a", "Game", "a", {"name": "Fire Red"})
    _record(tmp_path, "GameCollection-b", "GameCollection", "b", {"index": 0})
    _record(tmp_path, "GameSave-c", "GameSave", "c", _dated(NOW - 3 * HOUR))

    activity = health.delta_activity(tmp_path)

    assert activity.records == 3
    assert activity.dated == 1
    assert activity.age_seconds(NOW) == 3 * HOUR


def test_no_dated_records_at_all(tmp_path):
    _record(tmp_path, "Game-a", "Game", "a", {"name": "Fire Red"})

    activity = health.delta_activity(tmp_path)

    assert activity.last_write is None
    assert activity.age_seconds(NOW) is None
    assert "no way to tell" in health.idle_sync_note(activity, NOW)


def test_empty_folder(tmp_path):
    activity = health.delta_activity(tmp_path)

    assert activity.records == 0
    assert activity.last_write is None


def test_attached_data_files_are_ignored(tmp_path):
    """Saves and ROMs sit beside the records and are not JSON."""
    _record(tmp_path, "GameSave-a", "GameSave", "a", _dated(NOW - HOUR))
    (tmp_path / "GameSave-a-gameSave").write_bytes(b"\x00\xff" * 64)

    activity = health.delta_activity(tmp_path)

    assert activity.records == 1
    assert activity.age_seconds(NOW) == HOUR


def test_a_clock_ahead_of_us_does_not_produce_negative_age(tmp_path):
    """The phone's clock can be slightly ahead; that is not a reason to panic."""
    _record(tmp_path, "GameSave-a", "GameSave", "a", _dated(NOW + 90))

    activity = health.delta_activity(tmp_path)

    assert activity.age_seconds(NOW) == 0.0


def test_idle_note_names_the_account_as_the_thing_to_check(tmp_path):
    _record(tmp_path, "GameSave-a", "GameSave", "a", _dated(NOW - 6 * DAY))

    note = health.idle_sync_note(health.delta_activity(tmp_path), NOW)

    assert "6 days ago" in note
    assert "Dropbox account" in note
    assert "Delta Sync" in note


def test_describe_age_reads_like_speech():
    assert health.describe_age(60) == "1 minute"
    assert health.describe_age(45 * 60) == "45 minutes"
    assert health.describe_age(2 * HOUR) == "2 hours"
    assert health.describe_age(DAY) == "24 hours"
    assert health.describe_age(3 * DAY) == "3 days"
    assert health.describe_age(1.4 * DAY) == "34 hours"
