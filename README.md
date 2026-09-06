# Delta ↔ RetroArch Sync

Keeps battery saves, ROMs and cheats in sync between
[Delta](https://deltaemulator.com) on iOS and RetroArch on Windows, with no
manual steps once it is running.

Delta already syncs to Dropbox, and the Dropbox desktop client already mirrors
that to disk. This tool is the missing piece in the middle: it reconciles
Delta's mirrored folder against RetroArch's directories around the moments you
actually play.

**Not in scope:** save states (different internal formats, and RetroAchievements
hardcore mode rules them out anyway), controller skins, and app configs. See
[docs/brief.md](docs/brief.md).

## Status

Delta -> RetroArch works and is verified against real data: saves and ROMs land
correctly and byte-identically. The reverse direction is built but not yet
confirmed on a real device — the first attempt failed for a known reason and the
fix needs one round trip to verify. See [docs/research.md](docs/research.md).

| Phase | State |
| --- | --- |
| 1. Read-only inspector | Working |
| 2. Save sync, Delta -> RetroArch | Working, verified on real data |
| 2b. Save sync, RetroArch -> Delta | Built, needs `auth` + a real round trip to confirm |
| 3. ROM sync | Working |
| 4. Cheat sync (`.cht` generation) | Not started |
| 5. Launcher wrapper | Not started |

Currently cleared for sync: **GBA only**. NES, SNES and GBC are trivial
extensions of the same path. N64 and DS need real format conversion first and
are hard-blocked in `systems.py` until then — see
[docs/research.md](docs/research.md).

## Requirements

- Windows
- Python 3.11+
- Dropbox desktop client, signed into the account Delta syncs to
- Delta on iOS with **Delta Sync set to Dropbox, not Google Drive** — Google
  Drive stores files in a hidden `appDataFolder` that nothing but Delta can read
- RetroArch, launched at least once so it has written its config

## Usage

```
python -m delta_retroarch_sync inspect
```

Reports where it found Delta's Dropbox folder and RetroArch's config, then lists
every game Delta has synced with its SHA-1, save sizes, cheats, and whether
RetroArch already holds a matching save. It never writes.

If automatic discovery gets a path wrong, copy `config.example.toml` to
`config.toml` and override it. `config.toml` is gitignored, because this
repository is public and that file holds machine-specific absolute paths.

## How matching works

Delta's Dropbox folder is flat and its filenames are hashes, not game names:

```
Game-<sha1>-game              the ROM
GameSave-<sha1>-gameSave      the battery save
Cheat-<uuid>                  the cheat, as JSON
```

That `<sha1>` is the SHA-1 of the ROM file, which Delta computes on import and
uses as the game's identifier. So matching is exact — hash the local ROM, find
the record — with none of the fuzzy filename matching these tools usually need.

## Safety

Data loss is the failure mode this is designed against.

- **Delta's Dropbox folder is treated as read-only.** Delta warns that editing it
  can cause data loss, and Harmony reconciles against Dropbox file revisions, so
  writing there out of band would desync Delta itself.
- **Conflicts are never silently resolved.** A manifest of the last known-good
  state is compared against *both* sides. Only one side changed, that side wins;
  both changed, it is flagged, not guessed. This is what closes the gap when
  RetroArch crashes before the post-close sync runs.
- **Rolling backups** are kept before any save is overwritten.
- **Unverified conversions never run.** N64 and DS are reported by the inspector
  and refused by the sync until their formats are confirmed against real files.

## Pushing back to Delta

The Delta -> RetroArch direction needs nothing but the local Dropbox folder.
The reverse direction needs one extra thing: the save's Dropbox *revision*.
Delta downloads the exact revision a record names, and that value is assigned by
Dropbox on upload — it is not derivable locally and not exposed in the mirror.

So pushing requires read-only Dropbox API access, authorised once:

```
python -m delta_retroarch_sync auth --app-key <your app key>
python -m delta_retroarch_sync sync --push
```

The scope is `files.metadata.read` and nothing more. The tool never uploads —
the desktop client already does that — and never touches file property groups,
which Dropbox scopes to the app that created them and are therefore Delta's
alone.

## Auto-push

Commits push to `origin` automatically. The hook lives in `.githooks/` so it is
version-controlled; enabling it is one command per clone:

```
git config core.hooksPath .githooks
```

It never blocks a commit — if the push fails (offline, no remote, rejected) it
prints a note and the commit stays safely in local history.

## Tests

```
python -m unittest discover -s tests
```

The suite runs against a synthetic Delta folder built from the layout documented
in `docs/research.md`. When real data is available, the first job is to diff it
against those fixtures and correct whichever one is wrong.
