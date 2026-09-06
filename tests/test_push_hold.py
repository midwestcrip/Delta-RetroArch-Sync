"""Tests for holding a push back while Delta is still writing.

The failure this prevents, seen on 2026-09-06: the desktop pushed a save into
Delta's folder twice within five minutes while the same game was being played on
an iPad. Harmony saw two writers on one record, marked it conflicted, and asked
for a version to be chosen by hand on the device.

Nothing was corrupted -- refusing to guess is correct -- but the chore was caused
purely by timing, and the timing is visible from the desktop.
"""

from __future__ import annotations

import json

from delta_retroarch_synchronizer.manifest import APPLE_EPOCH_OFFSET
from delta_retroarch_synchronizer.sync import PUSH_HOLD_SECONDS, delta_wrote_within

NOW = 1_800_000_000.0
IDENT = "dd5945db9b930750cb39d00c84da8571feebf417"


def _record(folder, name, fields):
    (folder / name).write_text(
        json.dumps({"type": "GameSave", "identifier": IDENT, "record": fields}),
        encoding="utf-8",
    )


def _dated(unix: float) -> dict:
    return {"modifiedDate": unix - APPLE_EPOCH_OFFSET, "sha1": "abc"}


def test_holds_a_push_while_delta_is_still_writing(tmp_path):
    _record(tmp_path, f"GameSave-{IDENT}", _dated(NOW - 40))

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) == 40


def test_allows_a_push_once_delta_has_gone_quiet(tmp_path):
    _record(tmp_path, f"GameSave-{IDENT}", _dated(NOW - PUSH_HOLD_SECONDS - 1))

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) is None


def test_lowercased_record_name_is_still_found(tmp_path):
    """Delta renames records to Dropbox's pathLower once it has touched them."""
    _record(tmp_path, f"gamesave-{IDENT}", _dated(NOW - 10))

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) == 10


def test_a_phone_clock_running_ahead_holds_rather_than_releases(tmp_path):
    """An unreliable comparison should fail towards the cheaper mistake."""
    _record(tmp_path, f"GameSave-{IDENT}", _dated(NOW + 600))

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) == 0.0


def test_no_record_does_not_hold(tmp_path):
    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) is None


def test_undated_record_does_not_hold(tmp_path):
    _record(tmp_path, f"GameSave-{IDENT}", {"sha1": "abc"})

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) is None


def test_a_different_game_is_not_consulted(tmp_path):
    _record(tmp_path, "GameSave-6b47bb75d16514b6a476aa0c73a683a2a4c18765", _dated(NOW))

    assert delta_wrote_within(tmp_path, IDENT, PUSH_HOLD_SECONDS, NOW) is None
