# Research findings

Everything below was read out of primary sources — Delta's, Harmony's and the
Delta cores' own source — rather than inferred from forum posts. Where a fact is
still unverified it is marked as such, and the code refuses to act on it.

## Delta's Dropbox layout

Delta syncs through [Harmony](https://github.com/rileytestut/Harmony), a Core
Data sync framework, using the
[Harmony-Dropbox](https://github.com/rileytestut/Harmony-Dropbox) backend.

`Delta/Syncing/SyncManager.swift` sets:

```swift
DropboxService.shared.preferredDirectoryName = "Delta Emulator"
```

`DropboxService.remotePath` builds `"/" + directoryName [+ "/" + filename]`, so
everything lands in **one flat folder** with no per-game subdirectories:

```
<Dropbox>/Delta Emulator/
  Game-<sha1>                   JSON record
  Game-<sha1>-game              the ROM, no extension
  Game-<sha1>-artwork           box art
  GameSave-<sha1>               JSON record
  GameSave-<sha1>-gameSave      the battery save
  GameSave-<sha1>-gameTimeSave  GBC only: the .rtc clock file
  Cheat-<uuid>                  JSON record; the code lives inside
```

Record files are named `String(describing: recordID)`, and `RecordID.description`
is `type + "-" + identifier` (`Harmony/Model/Record.swift`). Attached files add
`"-" + file.identifier` (`DropboxService+Files.swift`).

### The identifier is the ROM's SHA-1

`DatabaseManager.swift` computes `RSTHasher.sha1HashOfFile(at: url)` on import
and uses it as `Game.identifier`. `GameSave.awakeFromSync` treats
`game.identifier != self.identifier` as a corruption error, so a `GameSave`
record always carries the same SHA-1 as its game.

This is the reliable join key between the two sides. The project brief's
original plan of matching by ROM filename does not work here, because Delta's
Dropbox filenames are hashes and its local ROM filename is `<sha1>.<ext>`, not
the game's name. Hashing local ROMs and matching on SHA-1 is both simpler and
more exact — no region-tag or `(USA)` vs `(U)` fuzzy matching.

### Record JSON shape

Confirmed against a real sync on 2026-09-05. Two details differ from what
`LocalRecord.swift` suggests read in isolation, and both were wrong in the first
implementation:

```json
{
  "type": "Game",
  "identifier": "dd5945db9b930750cb39d00c84da8571feebf417",
  "sha1Hash": "<hash of the record itself, not the ROM>",
  "record": {
    "name": "Pokémon: Fire Red Version",
    "filename": "<sha1>.gba",
    "type": "YnBsaXN0MDDUAQIDBAUGBwpYJHZlcnNpb24...",
    "isFavorite": false
  },
  "files": [
    { "identifier": "game", "sha1Hash": "<sha1>", "size": 16777216,
      "remoteIdentifier": "/delta emulator/game-<sha1>-game",
      "versionIdentifier": "65ac6a11de26ecb175c93" }
  ],
  "relationships": { "gameCollection": { "type": "GameCollection", "identifier": "com.rileytestut.delta.game.gba" } }
}
```

1. **`files` is a list of file objects**, not an `{identifier: sha1}` map. The
   encoder has both branches; records with uploaded files use the list.
2. **`type` and `artworkURL` are base64 NSKeyedArchiver plists**, not strings.
   Core Data attributes that are not JSON-native get archived. Decoding
   `type` yields a `$objects` table of `["$null", "com.rileytestut.delta.game.gba"]`.

`files[0].sha1Hash` equals the record identifier, which independently confirms
the identifier is the SHA-1 of the ROM.

`remoteIdentifier` is Dropbox's lowercased path (`/delta emulator/game-...`), so
it must never be used to build a local filename — the file on disk keeps its
original case (`Game-...`).

A `GameCollection-<system>` record also appears, one per system. It carries no
files and is ignored.

`Game.name` is the display name, which is what RetroArch needs the copied ROM
and its `.cht` file to be called.

### Metadata we deliberately do not use

Harmony also attaches `gameID` / `gameName` as Dropbox **property groups**
(`DropboxService.validateMetadata`, `PropertyGroup`). Those are cloud-side
metadata; the desktop client does not mirror them to disk. Since this tool only
reads the local mirror, it must never depend on them — and does not need to,
because the record JSON already carries the same information.

### Cheats are records, not files

`Cheat` conforms to `Syncable` with `syncableKeys` of `code`, `name`, `type`,
`creationDate`, `modifiedDate`, and a `game` relationship — but no
`syncableFiles`. So a cheat is a JSON blob, and extracting Delta's cheat list is
plain JSON parsing rather than the database reverse-engineering the brief
anticipated. `Cheat.identifier` is a `UUID()` assigned in `awakeFromInsert`, so
cheat records cannot be located by game; they must be grouped by walking every
`Cheat-*` record and reading `relationships.game.identifier`.

### Reserved identifiers to skip

`Game.melonDSBIOSIdentifier` and `Game.melonDSDSiBIOSIdentifier`
(`com.rileytestut.MelonDSDeltaCore.BIOS` / `.DSiBIOS`) are pseudo-games carrying
BIOS and firmware, not real content. They must be filtered out.

### Cheat records, confirmed from real data

A real `Cheat-<uuid>` record, 2026-09-05:

```json
{
  "type": "Cheat",
  "identifier": "2946F7C3-1C4C-46D6-932F-E3F199A7ED0C",
  "record": {
    "name": "Faster Text Display",
    "code": "00000000 18002C02
0000E01A 00000000",
    "type": "<base64 NSKeyedArchiver plist -> \"ActionReplay\">",
    "creationDate": 810357820.687162,
    "modifiedDate": 810357820.687162
  },
  "files": [],
  "relationships": {"game": {"type": "Game", "identifier": "<rom sha1>"}}
}
```

Two things this pins down:

- The code is stored **formatted** — display spacing and a newline per line —
  not as bare hex. Converting works from the hex digits regardless.
- `files` is empty. Cheats have no attachments, which is why pushing a cheat
  edit would need no Dropbox revision and therefore no API call, unlike a save.

Converting that record produced
`cheat0_code = "00000000+18002C02+0000E01A+00000000"`, byte-identical to
libretro-database's own entry for the same cheat.

### Why cheats cannot sync RetroArch -> Delta

Three cases, and only the third is impossible:

| Operation | Possible? | Why |
| --- | --- | --- |
| Read Delta's cheats | Yes | Plain JSON in the local mirror |
| Rewrite an existing cheat | Yes (not built) | Its record already has property groups; cheats have no files, so no revision to fix |
| Create a new cheat | **No** | A new file has no property groups, and only Delta's app can write them |

`RemoteRecord+Dropbox.swift` returns `nil` without property-group metadata, and
`DropboxService+Records.swift` `compactMap`s the listing — so a file we create is
not rejected, it is silently never seen.

A workaround exists but is not worth much: cheats created in Delta are editable
from the desktop, so pre-creating placeholder cheats on the phone gives the
desktop that many writable slots. It costs manual typing per placeholder and
clutters Delta's cheat list, so it scales to perhaps a dozen, not hundreds.

## Save format compatibility

`gameSaveFileExtension` read from each core repo; `GameType` raw values read
from each core's `*Types.m` / `*.swift`.

| System | Delta core | Delta ext | RetroArch ext | Verdict |
| --- | --- | --- | --- | --- |
| NES | Nestopia | `.sav` | `.srm` | Rename only |
| SNES | Snes9x | `.srm` | `.srm` | Identical |
| GBA | VBA-M | `.sav` | `.srm` | Rename only |
| GBC | Gambatte | `.sav` + `.rtc` | `.srm` + `.rtc` | Rename, both files |
| DS | melonDS | `.dsv` | `.srm` | **Unverified** |
| N64 | Mupen64Plus | `.sav` | `.srm` | **Needs conversion** |

RetroArch's extension is the frontend's convention, not the core's: the frontend
owns writing SRAM to disk, so cores cannot override it.

The four "rename only" rows are all raw battery dumps on both sides, so a copy
with a changed extension is genuinely sufficient.

### DS — unverified

Delta declares the DeSmuME `.dsv` extension while running melonDS. `.dsv` is
raw save data plus a footer ending in the marker `|-DESMUME SAVE-|`; melonDS
itself writes raw. Delta may have kept the extension for migration compatibility
while writing raw data, or may write a real footered `.dsv`.

**To verify:** read the tail of a real Delta DS save and look for the marker.
Strip or append the footer accordingly. Do not guess.

### N64 — needs real conversion

RetroArch's mupen64plus-next writes a single ~290 KB `.srm` packing EEPROM,
SRAM, FlashRAM and four mempaks at fixed offsets. Delta writes one bare save of
whichever type the cartridge uses. Converting means detecting the type by size
(512 B / 2 KB EEPROM, 32 KB SRAM, 128 KB FlashRAM) and placing it at the correct
offset. [`ra_mp64_srm_convert`](https://github.com/drehren/ra_mp64_srm_convert)
is a working reference for the offset layout.

Both DS and N64 are excluded from `ENABLED_SYSTEMS` in `systems.py`. The
inspector reports them; the sync will never write them until their conversion is
implemented and tested against real data.

## Push: what actually failed

Tested for real on 2026-09-05. The write itself was correct — the save landed
byte-identical, `record.sha1`, `files[0].sha1Hash` and the file's actual hash all
agreed, and the record's own `sha1Hash` was recomputed by the verified scheme.
Delta reported: it attempted a download, then failed to sync.

### First diagnosis, and why it was wrong

The initial conclusion was that Dropbox *file property groups* were to blame.
Harmony does build a record's identity from them, and they are genuinely
unreachable for us — Dropbox's API spec is explicit:

> Templates and their associated properties can't be accessed by any app other
> than the app that created them.

So we can never write the `Harmony` template's property groups. That part is
true. It is not what failed, and the observed behaviour rules it out:

```swift
// DropboxService+Records.swift
result.entries.lazy.compactMap { $0 as? Files.FileMetadata }
              .compactMap { RemoteRecord(file: $0, metadata: nil, ...) }
```

A record whose property groups are missing returns `nil` from that initializer
and is **silently dropped from the listing**. There would have been no download
attempt at all. Since Delta did attempt one, the record was constructed
correctly, which means the property groups survived the desktop client's
overwrite and still carry the right `recordedObjectType` / `recordedObjectIdentifier`.

The failure is later, at the file download.

### The real cause: the invalidated revision

`DownloadRecordOperation` asks Dropbox for the *exact* revision named in the
record, and falls back to the latest version only on one specific error:

```swift
service.download(remoteFile, version: versionID) { result in
    ...
    catch .doesNotExist(let fileID) { /* fall back to version: nil */ }
```

The push wrote a deliberately nonexistent revision (`"0" * 21`) to force that
fallback. Dropbox evidently does not answer a bogus revision with the error that
maps to `.doesNotExist`, so the fallback never fires and the error propagates as
a failed sync.

### The fix: use the real revision

The revision cannot be computed locally, but it can be *read*: once the desktop
client has uploaded the file, `files/get_metadata` returns the new `rev`. That
needs only the `files.metadata.read` scope on our own Dropbox app — no property
groups, no template ownership, nothing that belongs to Delta's app.

The push therefore becomes: write the save, wait for the desktop client to
upload it, read back the real revision (confirming via Dropbox's `content_hash`
that the upload is the file we wrote), then write the record referencing that
revision.

**Confirmed working on device, 2026-09-05.** A save made in RetroArch reached
Delta on the phone and loaded correctly. The record named revision
`65ac7c6d4d5cdcb175c93`, which is exactly what Dropbox held — the difference
from the failed attempt, which named a revision that did not exist.

### Why the second and third pushes failed, and what fixed them

The first push worked. The next ones did not, and the cause was the write
itself rather than the format.

`push_save` wrote via the usual temp-file-then-rename. That is the safer pattern
almost everywhere and the wrong one here: a rename over the target is a new file
as far as Dropbox *property groups* are concerned. Harmony builds a record's
identity from those groups and `compactMap`s away anything without them, so the
record became **invisible to Delta** — not rejected, never seen. Delta then
concluded it had no remote copy and switched to `mode = .add`, which fails
forever against a path that already exists. Hence "sync complete" with nothing
applied, an upload error that survived a per-record toggle, and a full
disconnect/reconnect that changed nothing.

Recovering meant deleting the record file so the path was free, letting Delta's
`.add` recreate it with fresh property groups.

The fix is `write_in_place`: keep the file object, change only its bytes. The
atomicity rename gave up is recovered at the call site — back up first, verify
afterwards by re-reading the record and re-deriving its hash.

Verified 2026-09-05: a push with the in-place write reached Delta cleanly on the
first attempt, with all health checks green before and after.

### Detecting this class of failure

None of it surfaced an error. `doctor` (see `health.py`) checks the specific
things that were wrong: the record's stored hash against its contents, the
record against the save on disk, the revision the record names against the one
Dropbox holds, and whether the desktop client has finished uploading.

What it cannot see is Delta's own state — a record it has marked conflicted or
pinned to a chosen version — or the property groups themselves, which are
readable only by the app that wrote them. A failure with every check green
points there.

## RetroArch side

Confirmed live on 2026-09-05 against `C:\Media\Games\Emulators\RetroArch`:

- `savefile_directory` in `retroarch.cfg`. A value of `default` or empty means
  the folder beside the config; a leading `:` means the install directory. This
  install has `":\saves"`.
- **`sort_savefiles_enable = "true"` on this machine**, so saves are written to
  `saves/<Core Name>/`, not `saves/` directly. The sync must mirror that.
- RetroArch's installer accepts any location, and this one is outside every
  conventional path, so the install is located via the uninstall registry entry
  (`DisplayIcon`, since `InstallLocation` is blank) rather than a path guess.
- `sort_savefiles_enable` and `sort_savefiles_by_content_enable` add subfolders
  by core or by content, changing the path the sync must write to.
- Cheats live at `cheats/<System>/<ROM Name>.cht`, plain text:

  ```
  cheats = 1
  cheat0_desc = "Walk Through Walls"
  cheat0_code = "509197D3+542975F4"
  cheat0_enable = false
  ```

## Safety constraint

Delta's documentation warns that files in the Dropbox folder are not intended to
be edited and that manual edits may cause data loss. This is not merely
cautionary: Harmony reconciles against Dropbox file *revisions*, so writing into
that folder out of band would desync Delta's own state machine. The Dropbox
folder is therefore treated as strictly read-only by this tool.

## Sources

- <https://faq.deltaemulator.com/using-delta/delta-sync>
- <https://github.com/rileytestut/Delta>
- <https://github.com/rileytestut/Harmony>
- <https://github.com/rileytestut/Harmony-Dropbox>
- <https://github.com/rileytestut/Delta/issues/429>
- <https://github.com/rileytestut/delta/issues/533>
- <https://docs.libretro.com/library/mupen64plus/>
- <https://github.com/drehren/ra_mp64_srm_convert>
- <https://github.com/JesseTG/melonds-ds/discussions/168>
- <https://forums.desmume.org/viewtopic.php?id=1678>
