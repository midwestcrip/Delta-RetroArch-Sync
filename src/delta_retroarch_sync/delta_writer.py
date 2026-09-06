"""Writing a save back into Delta's Dropbox folder.

This is the only module that writes to Delta's side, deliberately kept separate
from the read-only reader so the risk boundary is obvious.

Pushing a save means three coordinated changes, not one:

1. Overwrite ``GameSave-<sha1>-gameSave`` with the new save.
2. Update the record's ``record.sha1`` and ``files[].sha1Hash``. Delta compares
   the hash in the record against its local file to decide whether to download;
   leaving it stale means the push is simply ignored.
3. Recompute the record's own ``sha1Hash``. Harmony derives it from the record's
   contents, so an out-of-date value marks the record as corrupt.

The scheme for (3) is not guessed. ``LocalRecord.updateSHA1Hash`` re-encodes the
record with ``JSONEncoder(.sortedKeys)`` and ``isEncodingForHashing = true``,
which differs from the on-disk form in two ways: ``files`` becomes an
``{identifier: sha1}`` map, and ``sha1Hash`` itself is omitted. Reproducing that
and hashing it regenerates the stored value exactly for every real record --
see ``tests/test_delta_writer.py``, which pins this against a real record.

4. Point the record at the save's real Dropbox revision, which has to be read
   back from the API after the desktop client uploads. See
   ``REVISION_MUST_BE_REAL``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest import APPLE_EPOCH_OFFSET, sha1_of

#: Delta downloads the *exact* revision a record names
#: (``DownloadRecordOperation``: ``service.download(remoteFile, version: versionID)``),
#: so a record whose revision is stale makes Delta re-download the previous save
#: and silently undo the push.
#:
#: A first attempt wrote a deliberately nonexistent revision, aiming to trigger
#: Harmony's own fallback ("Exact version does not exist, so fall back to latest
#: version"). Tested against real Delta: it does not work. Dropbox answers a
#: bogus revision with something other than the error that maps to
#: ``.doesNotExist``, so the fallback never fires and the sync fails.
#:
#: The revision therefore has to be the real one, read back from Dropbox after
#: the desktop client uploads. See ``dropbox_api.wait_for_revision``.
REVISION_MUST_BE_REAL = (
    "pushing needs the save's real Dropbox revision; run "
    "`delta-retroarch-sync auth` once to enable it"
)


def encode_for_hashing(payload: dict[str, Any]) -> bytes:
    """Reproduce Swift's ``JSONEncoder`` output for the hashing pass.

    Compact, sorted keys, UTF-8, and forward slashes escaped -- the last matches
    what Foundation writes in the records on disk (``\\/delta emulator\\/...``).
    No real record's hashed payload has contained a slash so far, so that detail
    is faithful to the observed encoder rather than confirmed by a hash match.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.replace("/", "\\/").encode("utf-8")


def hashing_form(raw: dict[str, Any]) -> dict[str, Any]:
    """The record as Harmony re-encodes it for hashing."""
    files = raw.get("files")
    if isinstance(files, list):
        mapped = {
            entry["identifier"]: entry["sha1Hash"]
            for entry in files
            if isinstance(entry, dict)
            and "identifier" in entry
            and "sha1Hash" in entry
        }
    elif isinstance(files, dict):
        mapped = files
    else:
        mapped = {}

    return {
        "type": raw["type"],
        "identifier": raw["identifier"],
        "record": raw.get("record", {}),
        "relationships": raw.get("relationships", {}),
        "files": mapped,
    }


def record_hash(raw: dict[str, Any]) -> str:
    """The value Harmony would store in the record's ``sha1Hash``."""
    return hashlib.sha1(encode_for_hashing(hashing_form(raw))).hexdigest()


def verify_record_hash(raw: dict[str, Any]) -> bool:
    """True if a record's stored hash matches what we would compute.

    Used as a preflight before writing: if we cannot reproduce the hash of the
    record as it stands, our understanding of the format is wrong for this
    record and we must not rewrite it.
    """
    stored = raw.get("sha1Hash")
    return isinstance(stored, str) and record_hash(raw) == stored


def unix_to_apple(timestamp: float) -> float:
    """Convert a Unix timestamp to Core Data's 2001-based reference date."""
    return timestamp - APPLE_EPOCH_OFFSET


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def push_save(
    delta_folder: Path,
    identifier: str,
    source_save: Path,
    backup_dir: Path,
    *,
    file_identifier: str = "gameSave",
    revision: str | None = None,
) -> str:
    """Write ``source_save`` into Delta's folder for the game ``identifier``.

    ``revision`` is the save file's real Dropbox revision, read back after the
    desktop client has uploaded it. Passing ``None`` leaves the record's existing
    revision alone, which is only correct when the save content did not change.

    Returns a description of what changed. Raises if anything about the record
    is not as expected -- a half-applied push is worse than no push, so every
    check happens before the first write.
    """
    record_path = delta_folder / f"GameSave-{identifier}"
    save_path = delta_folder / f"GameSave-{identifier}-{file_identifier}"

    if not record_path.is_file():
        raise FileNotFoundError(f"no GameSave record for {identifier}")
    if not source_save.is_file():
        raise FileNotFoundError(f"no save to push at {source_save}")

    raw = json.loads(record_path.read_text(encoding="utf-8"))

    # Preflight: if we cannot reproduce the record's current hash, our model of
    # the format does not hold for this record and rewriting it would corrupt
    # Delta's state. Refuse rather than write something we cannot predict.
    if not verify_record_hash(raw):
        raise ValueError(
            f"cannot reproduce the existing sha1Hash of {record_path.name}; "
            "refusing to rewrite a record whose format we do not fully model"
        )

    files = raw.get("files")
    if not isinstance(files, list):
        raise ValueError(f"unexpected 'files' shape in {record_path.name}")
    matching = [
        entry
        for entry in files
        if isinstance(entry, dict) and entry.get("identifier") == file_identifier
    ]
    if len(matching) != 1:
        raise ValueError(
            f"expected exactly one '{file_identifier}' file in {record_path.name}, "
            f"found {len(matching)}"
        )
    file_entry = matching[0]

    # Everything below writes. Back up both pieces together first, so the record
    # and the save it describes can be restored as a matched pair.
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    shutil.copy2(record_path, backup_dir / f"{record_path.name}.{stamp}.bak")
    if save_path.is_file():
        shutil.copy2(save_path, backup_dir / f"{save_path.name}.{stamp}.bak")

    new_hash = sha1_of(source_save)
    new_size = source_save.stat().st_size

    # Save file first: if the record update fails, the record still points at
    # the old hash, so Delta ignores the file rather than acting on a mismatch.
    temporary = save_path.with_name(save_path.name + ".partial")
    shutil.copy2(source_save, temporary)
    if temporary.stat().st_size != new_size:
        temporary.unlink(missing_ok=True)
        raise OSError("short copy while writing save into Delta's folder")
    temporary.replace(save_path)

    file_entry["sha1Hash"] = new_hash
    file_entry["size"] = new_size
    if revision is not None:
        file_entry["versionIdentifier"] = revision

    record_fields = raw.setdefault("record", {})
    record_fields["sha1"] = new_hash
    record_fields["modifiedDate"] = unix_to_apple(datetime.now(timezone.utc).timestamp())

    raw["sha1Hash"] = record_hash(raw)
    _write_json_atomically(record_path, raw)

    return (
        f"wrote {new_size:,} B to {save_path.name}, "
        f"record hash now {raw['sha1Hash'][:12]}..."
    )
