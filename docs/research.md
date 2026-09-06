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

From `Harmony/Model/Core Data/LocalRecord.swift` (`CodingKeys`):

```json
{
  "type": "Game",
  "identifier": "<sha1>",
  "record":        { "name": "...", "filename": "<sha1>.gba", "type": "com.rileytestut.delta.game.gba" },
  "files":         { "game": "<sha1 of file>", "artwork": "..." },
  "relationships": { "gameCollection": { "type": "...", "identifier": "..." } }
}
```

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

## RetroArch side

- `savefile_directory` in `retroarch.cfg`. A value of `default` or empty means
  the folder beside the config; a leading `:` means the install directory.
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
