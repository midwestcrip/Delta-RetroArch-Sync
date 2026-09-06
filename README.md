# Delta-RetroArch Synchronizer

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

Bidirectional save sync works, confirmed on a real device in both directions:
progress made on the phone appears in RetroArch, and progress made in RetroArch
appears in Delta. ROM export and cheat export work. The launcher wrapper is
still to do.

### Cheats only travel one way

**Make cheats on your phone.** They appear in RetroArch automatically. A cheat
created in RetroArch will never reach Delta.

| | |
| --- | --- |
| Cheat made in Delta → appears in RetroArch | Works |
| Editing a cheat that already exists, from the desktop | Possible, not built |
| Brand-new cheat made in RetroArch → Delta | **Impossible** |

That last row is a hard limit, not a missing feature. Every item in Delta's
Dropbox folder carries metadata ("property groups") that Harmony requires in
order to see it at all, and Dropbox scopes that metadata to the app that created
it — *"Templates and their associated properties can't be accessed by any app
other than the app that created them."* Only Delta can write it.

So a file this tool creates is invisible to Delta. Not rejected, not an error:
Harmony's listing drops it silently, because `RemoteRecord(file:)` returns `nil`
without the metadata and the result is `compactMap`ped away.

Editing an *existing* cheat is a different matter — its record already carries
the metadata, so rewriting its contents works the same way a save push does, and
in fact more simply, since cheats have no attached files and therefore no Dropbox
revision to resolve. Saves are bidirectional for exactly this reason: Delta
created the record the first time you saved, and we only ever change what is
inside it.

| Phase | State |
| --- | --- |
| 1. Read-only inspector | Working |
| 2. Save sync, Delta -> RetroArch | Working, verified on real data |
| 2b. Save sync, RetroArch -> Delta | Working, confirmed on device (needs `auth`) |
| 6. Health checks (`doctor`) | Working |
| 3. ROM sync | Working |
| 4. Cheat sync (`.cht` generation) | Working, Delta -> RetroArch only |
| 5. Launcher window | Working |

Currently cleared for sync: **GBA and SNES**, each verified against a real
save. NES and GBC are the same plain-copy path and need only a real save to
check against. N64 and DS need genuine format conversion and are hard-blocked
in `systems.py` until then — see [docs/research.md](docs/research.md).

## Requirements

- Windows
- Python 3.11+
- Dropbox desktop client, signed into the account Delta syncs to
- Delta on iOS with **Delta Sync set to Dropbox, not Google Drive** — Google
  Drive stores files in a hidden `appDataFolder` that nothing but Delta can read
- RetroArch, launched at least once so it has written its config

## Install

**Download the zip**, extract it, run the .exe inside. No Python, nothing else
to install. It keeps its settings, credentials and save backups in that folder,
so a portable location works fine. (In Program Files or anywhere else
non-writable it falls back to `%LOCALAPPDATA%`.)

Windows will show *"Windows protected your PC"* the first time — **More info →
Run anyway**. The program is not code-signed, and Windows shows that for every
unsigned program it has not seen before.

The window finds Delta's Dropbox folder and your RetroArch install by itself.
If it cannot, the paths are editable in the window.

**Or run from source** — needs Python 3.11+ and nothing else; this tool has no
third-party dependencies.

Add it to the Start menu once, then search for "Delta":

```
powershell -ExecutionPolicy Bypass -File tools\install_start_menu.ps1
```

That installs per-user, needs no admin rights, and takes `-Uninstall` to remove
it. Or just double-click **`Delta-RetroArch Synchronizer.bat`** in this folder.

The launcher window finds Delta's Dropbox folder and your RetroArch install by
itself, shows what it found, and gives you one button: **Sync and Play**. That
syncs, launches RetroArch, waits for you to finish, and syncs again once it
closes — the save is copied after the process exits, because mGBA only flushes
it to disk on a clean quit.

Paths, and whether to sync ROMs and cheats, are editable in the window and
saved to `config.toml`.

There is also a CLI, if you prefer it:

```
python -m delta_retroarch_synchronizer inspect
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
python -m delta_retroarch_synchronizer sync --push
```

The scope is `files.metadata.read` and nothing more. The tool never uploads —
the desktop client already does that — and never touches file property groups,
which Dropbox scopes to the app that created them and are therefore Delta's
alone.

Press **Authorise Dropbox…** in the launcher; there is no terminal step. An app
registration is bundled so this works out of the box, and **Use own app key…**
swaps in your own. See [docs/dropbox-app.md](docs/dropbox-app.md) for what the
permission covers, how to register your own app, and how to revoke access.

### Why Delta used to ask you to resolve a conflict

It no longer should. Every push used to leave Delta asking you to pick a version
on the phone, even though the save arrived correctly and Delta's own screen
showed both sides as *Normal* with the same timestamp. The data always agreed;
only Harmony's bookkeeping did not.

Harmony stores each record's hash twice: in the record JSON, and in a Dropbox
**property group** attached to the same file, written together in one upload.
Its conflict test is `localRecord.sha1Hash != remoteRecord.sha1Hash` while both
sides are otherwise `.normal` -- and the local half is read from the JSON, the
remote half from the property group. Property groups belong to the app that
created the template, so this tool can neither read nor write Delta's half:

> Templates and their associated properties can't be accessed by any app other
> than the app that created them.
> -- [Dropbox file_properties documentation](https://www.dropbox.com/developers/documentation/http/documentation#file_properties)

Recomputing that hash therefore updated one half of a pair and guaranteed a
mismatch. The fix is to **leave it exactly as it was**. Everything else in the
record still describes the new save truthfully; only that one field is frozen,
and it corrects itself the next time Delta uploads the record, because Harmony
recomputes and rewrites both halves together.

Established on 2026-09-06 by a controlled test -- push with Delta closed on the
device and no other activity for 35 minutes, conflict appeared anyway -- then by
reading Harmony's source, then by the record history on this machine, where
every push had changed that field and a conflict had followed every push.

`doctor` reports which of the two wrote a record last, and no longer treats a
preserved hash as corruption. It cannot see Delta's conflict state at all, as
`docs/research.md` explains, so a clean report is not proof on its own.

## Auto-push

Commits push to `origin` automatically. The hook lives in `.githooks/` so it is
version-controlled; enabling it is one command per clone:

```
git config core.hooksPath .githooks
```

It never blocks a commit — if the push fails (offline, no remote, rejected) it
prints a note and the commit stays safely in local history.

## Building the downloads

```
python tools/build_release.py
```

Produces both the packaged application and a source zip in `dist/`.
PyInstaller is needed for the executable and is a build dependency only —
nothing it produces is imported by the tool, and the zip build does not use it.

The default is a **one-directory** build, deliberately. A one-file build unpacks
itself to a temp folder on every launch, which is what a packer or dropper does,
and Defender's ML heuristic flags it as `Trojan:Win32/Wacatac.B!ml` — on a
machine where Defender is on by default. `--onefile` is still available if you
want the convenience and can live with that.

The zip's contents are an explicit list rather than "everything not gitignored",
so a release cannot accidentally carry a `config.toml`, a Dropbox token, a
manifest or somebody's save backups.

## Tests

```
python -m unittest discover -s tests
```

The suite runs against a synthetic Delta folder built from the layout documented
in `docs/research.md`. When real data is available, the first job is to diff it
against those fixtures and correct whichever one is wrong.
