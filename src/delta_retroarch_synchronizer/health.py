"""Checks that a pushed save is in a state Delta can actually act on.

Pushing failed on 2026-09-05 in a way that produced no error anywhere: Delta
reported "sync complete", and simply did not apply the change. Every individual
piece looked right, and finding the problem meant listing the Dropbox folder by
hand and comparing revisions. That is the gap this closes.

Each check corresponds to a way the push has actually broken, not a hypothetical:

- The record's stored hash no longer matching its contents means Delta treats it
  as corrupt.
- The record's ``sha1`` disagreeing with the save file means Delta either
  ignores the push or downloads something that fails validation.
- The record naming a revision other than the one Dropbox holds is what made the
  first push attempt fail outright -- Delta asks for that exact revision.
- Local content that has not reached Dropbox yet means the desktop client is
  still syncing and nothing downstream can be trusted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import dropbox_api, harmony


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Health:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def problems(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]


def check_game(
    delta_folder: Path,
    identifier: str,
    dropbox: "dropbox_api.DropboxClient | None" = None,
) -> Health:
    """Inspect one game's record and save for the failures seen in practice."""
    from .delta_writer import verify_record_hash

    checks: list[Check] = []

    record_path = harmony.resolve_existing(delta_folder, f"GameSave-{identifier}")
    save_path = harmony.resolve_existing(
        delta_folder, f"GameSave-{identifier}-gameSave"
    )

    if not record_path.is_file():
        checks.append(
            Check(
                "record present",
                False,
                f"{record_path.name} is missing. Delta will recreate it on its "
                "next sync; if it does not, the game may have been removed.",
            )
        )
        return Health(checks)
    checks.append(Check("record present", True, record_path.name))

    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        checks.append(Check("record parses", False, str(error)))
        return Health(checks)

    verified = verify_record_hash(raw)
    checks.append(
        Check(
            "record hash matches contents",
            verified,
            "consistent" if verified
            else "Delta will treat this record as corrupt. Restore it from backups.",
        )
    )

    if not save_path.is_file():
        checks.append(Check("save present", False, f"{save_path.name} is missing"))
        return Health(checks)

    file_hash = hashlib.sha1(save_path.read_bytes()).hexdigest()
    claimed = str(raw.get("record", {}).get("sha1", ""))
    agrees = claimed == file_hash
    checks.append(
        Check(
            "record points at the save on disk",
            agrees,
            "consistent" if agrees
            else f"record says {claimed[:12]}, file is {file_hash[:12]}",
        )
    )

    if dropbox is None:
        checks.append(
            Check(
                "Dropbox revision",
                True,
                "not checked (authorise Dropbox to include this)",
            )
        )
        return Health(checks)

    folder = delta_folder.name
    for label, path in (("record", record_path), ("save", save_path)):
        try:
            meta = dropbox.get_metadata(f"/{folder}/{path.name}")
        except dropbox_api.DropboxError as error:
            checks.append(Check(f"{label} on Dropbox", False, str(error)[:120]))
            continue

        uploaded = dropbox_api.content_hash(path) == meta.get("content_hash")
        checks.append(
            Check(
                f"{label} uploaded to Dropbox",
                uploaded,
                "in sync" if uploaded
                else "the desktop client has not finished uploading this yet",
            )
        )

        if label == "save":
            files = raw.get("files")
            named = ""
            if isinstance(files, list):
                for entry in files:
                    if isinstance(entry, dict) and entry.get("identifier") == "gameSave":
                        named = str(entry.get("versionIdentifier", ""))
            actual = str(meta.get("rev", ""))
            matches = named == actual
            checks.append(
                Check(
                    "record names the current revision",
                    matches,
                    "matches" if matches
                    else f"record says {named}, Dropbox holds {actual} -- "
                    "Delta would download the wrong version",
                )
            )

    return Health(checks)
