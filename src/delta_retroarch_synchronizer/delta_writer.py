"""Writing back into Delta's Dropbox folder.

This is the only module that writes to Delta's side, deliberately kept separate
from the read-only reader so the risk boundary is obvious. Two things can be
written: a save (``push_save``) and an existing cheat's code (``push_cheat``).
Everything below is about the save, which is by far the harder of the two --
``push_cheat`` needs none of the revision handling because a cheat record has no
attached file at all.

Pushing a save means three coordinated changes, not one:

1. Overwrite ``GameSave-<sha1>-gameSave`` with the new save.
2. Update the record's ``record.sha1`` and ``files[].sha1Hash``. Delta compares
   the hash in the record against its local file to decide whether to download;
   leaving it stale means the push is simply ignored.
3. **Leave the record's own top-level ``sha1Hash`` exactly as it was.** This is
   the opposite of what it looks like it should do, and it is the whole reason
   pushing used to leave Delta asking the user to resolve a conflict.

   Harmony stores that hash twice: in the record JSON, and in the Dropbox
   **property group** on the same file, written together in one upload. Its
   conflict test is ``localRecord.sha1Hash != remoteRecord.sha1Hash`` while both
   sides are otherwise ``.normal`` -- and the local half comes from the JSON,
   the remote half from the property group. Property groups belong to the app
   that created the template ("Templates and their associated properties can't
   be accessed by any app other than the app that created them" -- Dropbox
   file_properties docs), so this tool can neither read nor write Delta's half.

   Writing a freshly computed hash therefore updates one half of a pair and
   guarantees a mismatch. Preserving the old value keeps the two halves equal,
   and the stale value corrects itself the next time Delta uploads the record,
   because ``UploadRecordOperation`` calls ``updateSHA1Hash()`` unconditionally
   and rewrites both halves together.

   Confirmed on 2026-09-06 by reading Harmony's source and by the record history
   on this machine: every push changed this field, and a conflict followed every
   push.

``record_hash`` still reproduces Harmony's scheme exactly -- ``LocalRecord.
updateSHA1Hash`` re-encodes with ``JSONEncoder(.sortedKeys)`` and
``isEncodingForHashing = true``, ``files`` becoming an ``{identifier: sha1}``
map with ``sha1Hash`` omitted -- and ``tests/test_delta_writer.py`` pins it
against a real record. It is kept because reproducing a record's stored hash is
how we tell that *Delta* wrote it last, which is worth reporting even though it
is no longer written.

4. Point the record at the save's real Dropbox revision, which has to be read
   back from the API after the desktop client uploads. See
   ``REVISION_MUST_BE_REAL``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .harmony import resolve_existing
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

#: A GameSave record can carry more than one file. Game Boy Color records carry
#: two: the battery save and a four-byte real-time-clock file.
#:
#: ``record.sha1`` describes *this* one and no other -- verified against a real
#: Crystal record, whose ``record.sha1`` equals the ``gameSave`` entry's hash
#: while the ``gameTimeSave`` entry carries its own. So writing a secondary file
#: must leave ``record.sha1`` alone; setting it to the clock's hash would tell
#: Delta the battery save had changed into a four-byte file.
PRIMARY_FILE = "gameSave"

#: How recently a file must have changed for a record/save disagreement to be
#: better explained by Dropbox still downloading than by damage.
#:
#: The desktop client fetches a record and its save as two independent files,
#: and they do not land together. Observed on 2026-09-07: Kirby's Adventure's
#: record and save arrived twelve seconds apart. A sync landing in that window
#: sees a record describing a save that is not there yet -- which is a true
#: statement about a transient state, not corruption, and resolves itself.
#:
#: Five minutes is deliberately generous. Being wrong in this direction says
#: "wait and retry" about a real problem, which costs one more sync; being wrong
#: the other way says "corrupt, run doctor" about a file that was fine, which is
#: what actually happened and is far more alarming.
SETTLING_SECONDS = 300


def recently_written(paths: "list[Path]", *, within: float = SETTLING_SECONDS) -> bool:
    """True if any of these files changed in the last ``within`` seconds.

    Used to tell "Delta is still syncing this" apart from "this is damaged".
    Deliberately reads mtimes rather than Delta's own ``modifiedDate``: the
    question is when the *desktop client* wrote the file here, not when Delta
    wrote it on the phone.
    """
    now = datetime.now(timezone.utc).timestamp()
    for path in paths:
        try:
            if now - path.stat().st_mtime < within:
                return True
        except OSError:
            continue
    return False


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


def write_in_place(path: Path, data: bytes) -> None:
    """Replace a file's contents without replacing the file.

    The usual write-a-temp-then-rename dance is the safer pattern almost
    everywhere, and it is the wrong one here. Delta's records carry Dropbox
    *property groups* -- cloud-side metadata that Harmony needs in order to see
    a record at all, and that only Delta's own app can write. A rename over the
    target is a new file as far as that metadata is concerned, and losing it
    makes the record invisible to Delta permanently. That is exactly what went
    wrong on 2026-09-05.

    So the file object is kept and only its bytes change. The cost is the
    atomicity that rename would have given, which is bought back by the caller:
    a backup is taken first and the result is verified afterwards.
    """
    with path.open("r+b") as handle:
        handle.write(data)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _record_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    # Resolve the real spelling rather than constructing one: writing to a
    # differently-cased name renames Delta's file on Dropbox.
    record_path = resolve_existing(delta_folder, f"GameSave-{identifier}")
    save_path = resolve_existing(
        delta_folder, f"GameSave-{identifier}-{file_identifier}"
    )

    if not record_path.is_file():
        raise FileNotFoundError(f"no GameSave record for {identifier}")
    if not source_save.is_file():
        raise FileNotFoundError(f"no save to push at {source_save}")

    raw = json.loads(record_path.read_text(encoding="utf-8"))

    # The record's top-level hash is preserved, never recomputed -- see the
    # module docstring. Capturing it before anything is modified is the whole
    # trick, so it is read out here rather than left to be picked up later.
    preserved_hash = raw.get("sha1Hash")
    if not isinstance(preserved_hash, str) or not preserved_hash:
        raise ValueError(
            f"{record_path.name} has no usable sha1Hash; refusing to write a "
            "record whose Harmony metadata we cannot preserve"
        )

    # Preflight. This used to require that we could reproduce the stored hash,
    # which stopped being a valid precondition once we began preserving it:
    # after our own first push the stored value deliberately no longer describes
    # the contents. What is still a real invariant, and covers the fields this
    # function actually edits, is that the record agrees with the save beside it.
    #
    # Checked against the *primary* save whichever file is being written, because
    # that is what record.sha1 describes. Comparing it against a secondary file
    # would make every clock push look like a corrupt record.
    primary_path = resolve_existing(
        delta_folder, f"GameSave-{identifier}-{PRIMARY_FILE}"
    )
    on_disk = sha1_of(primary_path) if primary_path.is_file() else None
    stated = raw.get("record", {}).get("sha1") if isinstance(raw.get("record"), dict) else None
    if on_disk is not None and stated != on_disk:
        # The same disagreement has two very different causes, and saying the
        # alarming one about the harmless one is its own kind of bug.
        if recently_written([record_path, primary_path]):
            raise ValueError(
                f"Delta is still syncing {record_path.name}: its record and its "
                "save arrived from Dropbox at different moments and do not agree "
                "yet. Nothing is wrong and nothing was written -- sync again in a "
                "minute."
            )
        raise ValueError(
            f"{record_path.name} says the save is {stated} but "
            f"{primary_path.name} is {on_disk}; refusing to push onto a record "
            "that is already inconsistent -- run doctor first"
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

    new_size = source_save.stat().st_size

    # A cartridge's SRAM is a fixed size, so a save that has changed size has
    # changed *format*, not contents -- almost certainly an emulator appending
    # something Delta does not expect. Game Boy Color is the live case: Delta
    # keeps the clock in a separate 4-byte file, while some libretro cores
    # append RTC state to the .srm, which would make RetroArch's file larger
    # than the 32768 bytes Delta's record describes.
    #
    # Pushing that verbatim would hand Delta a save it cannot read, in the one
    # direction that can damage the phone's copy. Refuse instead, and say what
    # the difference is rather than making the user work it out.
    expected_size = file_entry.get("size")
    if isinstance(expected_size, int) and new_size != expected_size:
        raise ValueError(
            f"{source_save.name} is {new_size:,} B but Delta's record expects "
            f"{expected_size:,} B. A save that changes size has changed format, "
            "so this is not pushed. If the emulator appends clock or footer data "
            "to the save, that has to be handled deliberately before pushing."
        )

    # Everything below writes. Back up both pieces together first, so the record
    # and the save it describes can be restored as a matched pair.
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    shutil.copy2(record_path, backup_dir / f"{record_path.name}.{stamp}.bak")
    if save_path.is_file():
        shutil.copy2(save_path, backup_dir / f"{save_path.name}.{stamp}.bak")

    new_hash = sha1_of(source_save)

    # Save file first: if the record update fails, the record still points at
    # the old hash, so Delta ignores the file rather than acting on a mismatch.
    write_in_place(save_path, source_save.read_bytes())
    if sha1_of(save_path) != new_hash:
        raise OSError(
            f"{save_path.name} does not match the source after writing; "
            "restore it from the backup taken above"
        )

    file_entry["sha1Hash"] = new_hash
    file_entry["size"] = new_size
    if revision is not None:
        file_entry["versionIdentifier"] = revision

    record_fields = raw.setdefault("record", {})
    # Only the primary file is described by record.sha1 -- see PRIMARY_FILE. The
    # modified date is bumped either way, because the record did change.
    if file_identifier == PRIMARY_FILE:
        record_fields["sha1"] = new_hash
    record_fields["modifiedDate"] = unix_to_apple(datetime.now(timezone.utc).timestamp())

    # Deliberately unchanged. Recomputing it here is what made Delta ask the
    # user to resolve a conflict after every push.
    raw["sha1Hash"] = preserved_hash
    write_in_place(record_path, _record_bytes(raw))

    # Read the record back and check the fields we set. A short write is the one
    # failure the in-place approach cannot rule out by construction, and the
    # stored hash can no longer be used to detect it, so the values themselves
    # are what gets verified.
    written = json.loads(record_path.read_text(encoding="utf-8"))
    written_entry = next(
        (
            entry
            for entry in written.get("files", [])
            if isinstance(entry, dict) and entry.get("identifier") == file_identifier
        ),
        None,
    )
    expected_record_sha1 = (
        new_hash if file_identifier == PRIMARY_FILE else record_fields.get("sha1")
    )
    if (
        written.get("sha1Hash") != preserved_hash
        or written_entry is None
        or written_entry.get("sha1Hash") != new_hash
        or written.get("record", {}).get("sha1") != expected_record_sha1
    ):
        raise OSError(
            f"{record_path.name} did not verify after writing; "
            "restore it and the save from the backups taken above"
        )

    return (
        f"wrote {new_size:,} B to {save_path.name}, "
        f"record hash preserved as {preserved_hash[:12]}..."
    )


def push_cheat(
    delta_folder: Path,
    identifier: str,
    new_code: str,
    backup_dir: Path,
    *,
    new_name: str | None = None,
) -> str:
    """Rewrite an existing cheat's code in Delta's folder.

    Simpler than pushing a save, and the reason is structural: a ``Cheat`` record
    has no ``syncableFiles``, so there is no attached file, no Dropbox revision
    to read back, and therefore no API call and no authorisation. Everything that
    made the save push hard is absent here. What remains is the same as ever --
    write in place, and preserve the top-level hash.

    Only an *existing* cheat can be written. Creating one would mean a new file
    with no Dropbox property groups, which Harmony drops from its listing without
    an error; the caller is responsible for never asking for that, and this
    refuses an identifier it cannot already find.
    """
    record_path = resolve_existing(delta_folder, f"Cheat-{identifier}")
    if not record_path.is_file():
        raise FileNotFoundError(
            f"no Cheat record for {identifier}. A cheat that does not already "
            "exist in Delta cannot be created from here -- make it on the phone."
        )

    raw = json.loads(record_path.read_text(encoding="utf-8"))

    preserved_hash = raw.get("sha1Hash")
    if not isinstance(preserved_hash, str) or not preserved_hash:
        raise ValueError(
            f"{record_path.name} has no usable sha1Hash; refusing to write a "
            "record whose Harmony metadata we cannot preserve"
        )

    # A cheat with attached files is not the record shape this was written
    # against, and the difference would matter: files carry hashes and revisions
    # that a save push has to maintain and this does not touch. Refuse rather
    # than write a record we do not understand.
    files = raw.get("files")
    if files not in (None, [], {}):
        raise ValueError(
            f"{record_path.name} has attached files, which a Cheat record should "
            "not; refusing to rewrite a record whose shape is unexpected"
        )

    record_fields = raw.get("record")
    if not isinstance(record_fields, dict) or not isinstance(
        record_fields.get("code"), str
    ):
        raise ValueError(f"{record_path.name} has no readable cheat code")

    previous_name = record_fields.get("name")
    renaming = new_name is not None and new_name != previous_name
    if record_fields["code"] == new_code and not renaming:
        return f"{record_path.name} already holds this code"

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    shutil.copy2(record_path, backup_dir / f"{record_path.name}.{stamp}.bak")

    record_fields["code"] = new_code
    if renaming:
        assert new_name is not None
        record_fields["name"] = new_name
    record_fields["modifiedDate"] = unix_to_apple(
        datetime.now(timezone.utc).timestamp()
    )
    # `type` is an NSKeyedArchiver plist in base64 and is never rewritten -- we
    # decode it to read, and re-encoding it is not something to attempt for a
    # field that never changes.
    raw["sha1Hash"] = preserved_hash
    write_in_place(record_path, _record_bytes(raw))

    written = json.loads(record_path.read_text(encoding="utf-8"))
    written_fields = written.get("record", {})
    if (
        written.get("sha1Hash") != preserved_hash
        or written_fields.get("code") != new_code
        or (renaming and written_fields.get("name") != new_name)
    ):
        raise OSError(
            f"{record_path.name} did not verify after writing; "
            "restore it from the backup taken above"
        )

    what = "code and name" if renaming else "code"
    return f"rewrote {record_path.name} ({what}), record hash preserved"
