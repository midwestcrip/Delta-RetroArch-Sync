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
appears in Delta. ROM export and cheat export work, and the launcher window is
what you actually use day to day — it syncs, launches RetroArch, and syncs again
once it closes.

### Cheats: edit anywhere, create on the phone

**Make cheats on your phone.** They appear in RetroArch automatically, and from
then on you can edit the code on either side and it reaches the other. What you
cannot do is *create* one in RetroArch.

| | |
| --- | --- |
| Cheat made in Delta → appears in RetroArch | Works |
| Editing a cheat's code, from either side | Works |
| Brand-new cheat made in RetroArch → Delta | **Impossible** |

Editing needs `--push` (or the launcher's push setting) because it writes into
Delta's folder, but unlike a save it needs no Dropbox authorisation: a cheat
record carries no attached file, so there is no Dropbox revision to look up.

If the same cheat changed on both sides since the last sync, that is reported as
a conflict and **neither** side is touched — including the `.cht`, which is left
exactly as you edited it.

That last table row is a hard limit, not a missing feature. Every item in Delta's
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

A `.cht` entry is matched back to its Delta cheat **by name**, since the code is
the thing that changes and the cheat type is not recoverable from a `.cht` at
all. So renaming a cheat in RetroArch unlinks it, and it is then reported as one
that only exists on the RetroArch side. Rename on the phone instead.

| Phase | State |
| --- | --- |
| 1. Read-only inspector | Working |
| 2. Save sync, Delta -> RetroArch | Working, verified on real data |
| 2b. Save sync, RetroArch -> Delta | Working, confirmed on device (needs `auth`) |
| 6. Health checks (`doctor`) | Working |
| 3. ROM sync | Working |
| 4. Cheat sync (`.cht` generation) | Working, Delta -> RetroArch only |
| 5. Launcher window | Working |

**Every system Delta supports now syncs: GBA, SNES, GBC, NES, DS and N64.** Each
was enabled only once a real save had been inspected for a header, footer or
wrapper — being the same plain-copy path was never the bar. Game Boy Color also
syncs its real-time clock, on the Gambatte core only.

Five of the six are a copy with a different extension. **N64 is the one real
conversion**: RetroArch's mupen64plus-next keeps a single 296,960-byte `.srm`
holding EEPROM, SRAM, FlashRAM and four Controller Paks at fixed offsets, while
Delta writes a bare dump of whichever storage the cartridge has. The mapping is
in `n64.py`, written against six real saves covering all four storage types.

**Controller Pak data is deliberately never touched.** Delta does not sync it, so
those four 32 KB regions belong entirely to this PC and are carried through
untouched — a save arriving from your phone can never wipe your Mario Kart 64
ghosts. The flip side is that pak data does not travel, and the sync says so once
per game rather than leaving it to be discovered.

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
- **Rolling backups** are kept before any save is overwritten, and can be put
  back — see below.
- **Unverified conversions never run.** A system stays out of `ENABLED_SYSTEMS`
  until its format has been checked against a real save, and a save whose size
  the N64 conversion does not recognise is refused rather than placed at a
  guessed offset.
- **Data that is not ours is never written.** The N64 conversion edits one
  region of the combined save and copies every other byte through, so Controller
  Pak contents survive a sync untouched.

## Going back to an earlier save

Every write copies the previous version aside first — saves on both sides, clock
files, Delta's records, cheats. To see them:

```bash
python -m delta_retroarch_synchronizer backups
```

```
  [  1] Pokémon: Crystal Version  ·  Delta save  ·  2026-09-07 01:05:36 UTC  ·  32,768 B
  [  2] Super Mario World  ·  Delta save  ·  2026-09-06 21:08:45 UTC  ·  2,048 B
  [  3] Pokémon - Fire Red Version  ·  RetroArch save  ·  2026-09-06 17:31:26 UTC  ·  131,072 B
```

```bash
python -m delta_retroarch_synchronizer restore 3
```

Without `--yes` that writes nothing and prints exactly which file it would
overwrite. Add `--yes` to do it. There is no interactive prompt, deliberately —
every command here works without a terminal, and this is the one you might want
to script after a bad sync.

Three things worth knowing:

- **A restore is undoable.** Whatever it replaces is backed up first, so it
  becomes a new entry in the list.
- **A restore is a change like any other.** The manifest is deliberately left
  alone, so the next sync carries the restored save to the other side — or
  reports a conflict if that side moved too. Recording it as already-agreed
  would leave the two sides holding different saves while the tool believed they
  matched, and nothing would ever reconcile them.
- **Restoring into Delta is not a file copy.** It goes through the same push
  path a normal sync uses, because the record has to name the restored save's
  new Dropbox revision. Without that, Delta downloads the newer save straight
  back over it. So it needs Dropbox authorisation, the same as any push —
  except for cheats, which have no attached file and so need none.

`GameSave-<sha1>` record files are backed up too but are not listed as restore
points. The record is rewritten from the save it accompanies, so restoring the
save is the operation; those copies are there for manual recovery.

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

Fixed, and confirmed on device on 2026-09-06: a push followed by two clean Delta
syncs, no conflict, and Delta left the record untouched.

Every push used to leave Delta asking you to pick a version on the phone, even
though the save arrived correctly and Delta's own screen showed both sides as
*Normal* with the same timestamp. The data always agreed; only Harmony's
bookkeeping did not.

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

It also explains the timing. Harmony runs conflict detection *before* download,
so the first sync after a push downloaded happily -- which is why the save always
arrived -- and a later sync, with nothing happening on the device, compared the
two hashes and flagged it. That delay is what made it look like a timing problem.

Found by a controlled test -- push with Delta closed on the device and no other
activity for 35 minutes, conflict appeared anyway -- then by reading Harmony's
source, then by the record history on this machine, where every push had changed
that field and a conflict had followed every push.

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

**Run `python tools/build_bootloader.py` first.** Every PyInstaller wheel ships
the same prebuilt bootloader, those exact bytes sit inside a lot of real malware,
and Microsoft's models have learned them. A release built without this step is
quarantined on a stranger's machine as `Trojan:Win32/Wacatac.C!ml` and the
executable is deleted — measured on 2026-09-06, on a clean Windows account, with
the file downloaded from GitHub. Compiling the bootloader locally produces bytes
nobody has seen before, and an A/B a minute apart on one machine put the stock
build at "found 1 threats" and the locally compiled one at "found no threats".
`build_release.py` reports which bootloader it baked in and warns when it is the
stock one.

The default is a **one-directory** build. A one-file build unpacks itself to a
temp folder on every launch, which is what a packer or dropper does, so
one-directory is still the better shape — but it is not a fix for the false
positive. This README used to claim it avoided the heuristic; the naive-user test
falsified that, quarantining a one-directory build. The bootloader is the thing
that matters. `--onefile` remains available.

None of this is code signing, which is the only thing that also removes the
SmartScreen prompt.

The zip's contents are an explicit list rather than "everything not gitignored",
so a release cannot accidentally carry a `config.toml`, a Dropbox token, a
manifest or somebody's save backups.

## Tests

```
python -m pytest tests -q
```

203 tests. `python -m unittest discover -s tests` also runs the whole suite and
needs nothing installed, but it reports 116 — that is the number of test
*methods*, and it does not tally the subtests inside them. Same coverage, and
pytest is the only third-party package this repository asks for anywhere.

The suite runs against a synthetic Delta folder built from the layout documented
in `docs/research.md`. When real data is available, the first job is to diff it
against those fixtures and correct whichever one is wrong.
