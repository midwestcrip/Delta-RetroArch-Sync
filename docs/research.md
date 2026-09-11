# Research findings

Everything below was read out of primary sources — Delta's, Harmony's and the
Delta cores' own source — or measured against a real file on this machine,
rather than inferred from forum posts. Where a fact is still unverified it is
marked as such, and the code refuses to act on it.

Findings that were later revised have been **corrected in place**, with the
superseded claim kept beside the correction and dated, rather than appended as a
later section contradicting an earlier one. Four things recorded here as settled
turned out not to be — the DS save format, the Crystal clock base, the N64 size
guard, and save states — and each is written up where the original claim was,
not at the end.

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
| Rewrite an existing cheat | Yes, built 2026-09-07 | Its record already has property groups; cheats have no files, so no revision to fix |
| Create a new cheat | **No** | A new file has no property groups, and only Delta's app can write them |

`RemoteRecord+Dropbox.swift` returns `nil` without property-group metadata, and
`DropboxService+Records.swift` `compactMap`s the listing — so a file we create is
not rejected, it is silently never seen.

**Two independent proofs, from opposite directions.** The above is read out of
Delta's and Harmony's source. Dropbox's own API spec closes it from its side
without reference to Delta at all: a property template "is owned either by a
user/app pair or team/app pair", and templates "can't be accessed by any app
other than the app that created them". The `Harmony` template belongs to Delta's
app registration, so no scope on any other app key reaches it —
`files.metadata.write` does write properties, but only under a template the
writing app owns, and Harmony reads Delta's.

Re-checked 2026-09-07 specifically to look for a way around it. There is not
one, and now there are two reasons rather than one.

A workaround exists but is not worth much: cheats created in Delta are editable
from the desktop, so pre-creating placeholder cheats on the phone gives the
desktop that many writable slots. It costs manual typing per placeholder and
clutters Delta's cheat list, so it scales to perhaps a dozen, not hundreds.

Worth watching: Delta has begun adding **sanctioned URL-scheme hooks**
(`delta://gameInfo`, the Library Export toggle). None of them touch cheats, but
the existence of that surface is the first sign of a supported way in — which is
what would change this, rather than anything discoverable on the Dropbox side.

## Save format compatibility

`gameSaveFileExtension` read from each core repo; `GameType` raw values read
from each core's `*Types.m` / `*.swift`.

| System | Delta core | Delta ext | RetroArch ext | Verdict |
| --- | --- | --- | --- | --- |
| NES | Nestopia | `.sav` | `.srm` | Rename only |
| SNES | Snes9x | `.srm` | `.srm` | Identical |
| GBA | VBA-M | `.sav` | `.srm` | Rename only |
| GBC | Gambatte | `.sav` + `.rtc` | `.srm` + `.rtc` | Rename, both files |
| DS | melonDS | `.dsv` | `.srm` | Rename only |
| N64 | Mupen64Plus | `.sav` | `.srm` | **Converted** — see `n64.py` |

RetroArch's extension is the frontend's convention, not the core's: the frontend
owns writing SRAM to disk, so cores cannot override it.

The "rename only" rows are all raw battery dumps on both sides, so a copy with a
changed extension is genuinely sufficient.

### DS — resolved 2026-09-07: Delta writes raw

Delta declares the DeSmuME `.dsv` extension while running melonDS. `.dsv` is raw
save data plus a footer ending in the marker `|-DESMUME SAVE-|`; melonDS itself
writes raw. So Delta had either kept the extension for migration compatibility
while writing raw data, or was writing a real footered `.dsv` — and the second
would mean handing RetroArch's core a save with trailing metadata where it
expects none.

Measured on a real Pokémon Platinum save:

| | |
| --- | --- |
| Size | 524,288 bytes exactly — 512 KB, the bare chip size |
| Power of two | Yes, which a footered file cannot be |
| `\|-DESMUME SAVE-\|` | **Absent from the file entirely** |
| Tail | `ff ff ff …`, unwritten flash padding |

The marker was searched for across the whole file rather than inferred from the
size, because a footer on a save that happened to be short would have left the
size looking right.

So the extension is a migration leftover and DS is a rename, like SNES. Verified
end to end the same day: the pull landed byte-identical (SHA-1 match against
Delta's copy) at `saves/melonDS DS/Pokémon - Platinum Version.srm`.

The RetroArch → Delta direction was verified on a real device, confirmed
2026-09-10. DS is now measured in both directions, like the other five systems,
and the plain-copy path is no longer supported only by inference.

What the evidence actually is, since this claim was briefly in the file without
any: a save made in RetroArch was pushed, Delta's copy was written 2026-09-08
13:41, and on 2026-09-10 that save was **visible in Delta on the phone** — the
end of the trip that no amount of local checking can reach. Both sides are
byte-identical and the manifest agrees with them:

    Delta      GameSave-0862ec…755c-gameSave           524,288 B
    RetroArch  saves/melonDS DS/Pokemon - Platinum…srm 524,288 B
    SHA-1      1fe21a1a69da029aa9a591a52cc52fadf4f79269   (both, and the manifest)

Note the local files alone would **not** have proved this. Delta's file changing
is also what a Dropbox download looks like when you play on the phone, so an
mtime is not a direction. Seeing the RetroArch-made save in Delta is.

### The clock base barely ever moves, and Crystal does not move it

Gambatte stores `baseTime_` — the instant the cartridge clock read zero — and
derives the live clock as `std::time(0) - baseTime_` (`Rtc::doLatch`). Two
consequences, both established the hard way on 2026-09-07:

**Fast-forward cannot advance it.** It counts real seconds. Speeding up the CPU
does nothing, on hardware or in any emulator. An in-game clock that "did not
move" during fast-forward is correct behaviour, not a sync failure.

**Setting the in-game time does not move the base either.** `baseTime_` changes
only in `setDh`/`setDl`/`setH`/`setM`/`setS` — the game writing the MBC3 RTC
registers — or on the 511-day overflow. Pokémon Crystal evidently does not write
them: a new game was started in RetroArch with the time set (player name
`TESTBOY` → `TESTGAL`, confirmed in both the primary and backup save copies),
RetroArch rewrote the `.rtc` afterwards, and the base was **unchanged**. Crystal
keeps its own time reference in the save's own data instead.

So the in-game time travels **inside the battery save**, which already syncs.
The `.rtc` is a separate, near-static reference whose only job is to keep both
machines counting from the same instant — which matters, because Gambatte's
constructor sets `baseTime_(0)`, and a machine with no `.rtc` would compute an
elapsed time measured in decades.

The practical upshot: the clock *pull* is what carries the value, and it has run.
The clock *push* is correct but close to unreachable through normal play — it
compares, finds both sides identical, and declines. `doctor` reports whether the
two sides agree, which is the only thing here that can be silently wrong.

**Delta stores the same quantity, confirmed from the record history on
2026-09-08.** This had only ever been established for Gambatte, from its source;
the Delta side was assumed to match because Delta also runs Gambatte. Assuming
it was the weak link, because the conversion in `clock.py` is a plain
width-and-byte-order swap and is correct only if both files mean the same thing.
A last-played timestamp on one side and a base instant on the other would have
been silently shifting Crystal's clock on every sync.

The rolling backups answer it without needing either emulator's source. Eight
consecutive versions of Crystal's `GameSave` record, spanning 2026-09-07 01:05
to 15:08 UTC:

| | `gameSave` | `gameTimeSave` |
| --- | --- | --- |
| across 8 record versions | **6 distinct hashes** | **1 hash, unchanged** |

The game was played and saved six times over fourteen hours and the clock value
never moved. A "when the game was last played" timestamp would have moved with
every one of them. It is a base instant on both sides, the swap is sound, and
the live files agree: Delta's `6a 9d cb 79` and RetroArch's
`79 cb 9d 6a 00 00 00 00` both decode to 1788726137, with `doctor` reporting
`clock: agrees on both sides`.

The reason the original measurements looked like play times is that both were
taken on **fresh saves**, where the clock starts at zero *now* and the base is
therefore indistinguishable from the moment you stopped playing.

### The DS has no clock file, and needs none

Checked 2026-09-07 against the real Dropbox folder rather than reasoned about,
because the absence of a DS clock sync looked like a gap and is not one:

```
Pokemon: Platinum Version  [Nintendo DS]   save 524,288 B   extra: none
Pokemon: Crystal Version   [GBC]           save  32,768 B   extra: ['gameTimeSave']
```

Delta attaches a clock file to Game Boy Color records and to nothing else, which
matches the explicit `if game.type == .gbc` in `GameSave.swift`. There is no DS
clock in Dropbox to sync.

And none is needed, because the two systems keep time differently:

- A **Game Boy** cartridge has its own RTC counting from the instant it was
  started. Gambatte stores that instant as a base and derives the reading as
  `time(0) - base`. Two machines only agree if they share the base — hence the
  sync above.
- A **DS** has a system clock, and melonDS's RTC rework stores an **offset in
  seconds from host time** in a separate `rtc.bin`. With no offset set, the
  emulated DS reads real time. Both the phone and the PC have correct real time,
  so they agree without exchanging anything.

Confirmed on disk: the only `.rtc` anywhere in either RetroArch install is
`saves/Gambatte/Pokémon - Crystal Version.rtc`; the `saves/melonDS DS/` folder
has no clock file, and melonDS DS's own documentation lists `.srm` as the only
save it writes.

The one case that would not travel is a **deliberate** offset — someone setting
a wrong date in DS firmware settings on one side. Delta exposes no way to sync
that and stores nothing to sync, so it stays local. That is a corner, not a gap.

### N64 — needs real conversion

RetroArch's mupen64plus-next writes a single ~290 KB `.srm` packing EEPROM,
SRAM, FlashRAM and four mempaks at fixed offsets. Delta writes one bare save of
whichever type the cartridge uses. Converting means detecting the type by size
(512 B / 2 KB EEPROM, 32 KB SRAM, 128 KB FlashRAM) and placing it at the correct
offset. [`ra_mp64_srm_convert`](https://github.com/drehren/ra_mp64_srm_convert)
is a working reference for the offset layout.

Both N64 and DS were excluded from `ENABLED_SYSTEMS` in `systems.py` until
2026-09-07. DS turned out to need no conversion at all; N64's was written that
day against the offsets below and six real saves, and lives in `n64.py`.

### Delta writes exactly one storage, and its size names the type

`N64EmulatorBridge.saveGameSaveToURL:` branches on `g_dev.cart.use_flashram` and
copies **one** storage, whole:

| `use_flashram` | storage written |
| --- | --- |
| `-1` | `g_dev.cart.sram.storage` |
| `0` | `g_dev.cart.eeprom.storage` |
| `1` | `g_dev.cart.flashram.storage` |

It writes `storage->size` bytes and nothing else — no header, no footer, no
combining. So **the file size identifies the save type unambiguously**, which is
what the conversion needs and is now read out of Delta's source rather than
assumed:

| Size | Type | Verified against |
| --- | --- | --- |
| 512 B | EEPROM 4 Kbit | Super Mario 64 |
| 2,048 B | EEPROM 16 Kbit | Yoshi's Story, Donkey Kong 64 |
| 32,768 B | SRAM | Ocarina of Time |
| 131,072 B | FlashRAM | Majora's Mask, Paper Mario |

All six were captured from real Delta syncs on 2026-09-07, so every cartridge
save type N64 has now has at least one real file behind it. `loadGameSaveFromURL:`
is the mirror image and `memset`s the storage to `0xFF` when no file exists,
which is the correct empty state to write into the combined `.srm` for a region
Delta has nothing for.

### Controller Pak data never leaves the phone

`GameSave.syncableFiles` in Delta's own model declares exactly two file
identifiers — `gameSave`, and `gameTimeSave` behind an explicit
`if game.type == .gbc`. There is no Controller Pak or mempak entry, for N64 or
anything else.

The real Super Mario 64 record agrees: one file, `gameSave`, 512 bytes, where a
Game Boy Color record carries two.

So **Delta syncs the cartridge save and nothing else.** Mario Kart 64 ghosts,
and any other Controller Pak data, exist only on the device — not because this
tool skips them, but because Delta never uploads them. That is not a gap this
project can close from the desktop side.

The data does exist, though. `MupenInitiateControllers` plugs a Mem Pak into all
four ports:

```objc
ControlInfo.Controls[0].Present = 1;
ControlInfo.Controls[0].Plugin  = PLUGIN_MEMPAK;   // and 1, 2, 3
```

and `SaveSRAMPath` points mupen64plus at the core's own `Saves` folder, so it
writes real `.mpk` files there. They are simply outside `syncableFiles`.

**Those files are reachable by hand.** The chain, all from source:

- `Delta.coresDirectoryURL` = `FileManager.urls(for: .documentDirectory)[0]` +
  `Cores/` (`DeltaCore/Delta.swift`)
- `DeltaCoreProtocol.directoryURL` appends the core's `name`, which for N64 is
  `"Mupen64Plus"` (`N64.swift`)
- `gameSaveDirectoryURL` appends `Saves/` (`N64EmulatorBridge.m`)
- Delta's `Info.plist` sets `UIFileSharingEnabled` and `UISupportsDocumentBrowser`
  to `true`, so the Documents directory is browsable

which lands at **`On My iPhone → Delta → Cores → Mupen64Plus → Saves`** in the
iOS Files app. Copying a `.mpk` out is a manual, per-file operation with no
automatic path — this tool reads Dropbox, and these never reach Dropbox.

The practical consequence for the N64 conversion, and the rule `n64.py` is
built around: of the four regions
mupen64plus-next packs into its `.srm`, **only one is ever ours to write.** The
four mempak regions must be left exactly as RetroArch has them — Delta has
nothing to put there, and zeroing them would wipe Controller Pak data the player
created on the desktop.

**A predicted consequence that did not happen.** It was recorded here that the
**size guard** in `delta_writer.push_save` — which refuses a push whose byte
count differs from the record's — would be "exactly wrong for N64" and would
need a per-system exemption. It did not, and the reason is worth keeping,
because it is the same reason the conversion is exact.

Extraction takes its byte count **from Delta's record**, never inferred from the
`.srm`. The record says the cartridge save is 512 bytes; the conversion reads
512 bytes from the EEPROM offset; the staged file is therefore exactly the size
the guard expects, and the guard passes on its own terms. Had the size been
inferred from the combined file instead, the exemption would have been needed —
and it would have been an exemption papering over a conversion that did not know
how much of the file was real.

Prediction superseded 2026-09-07 by the working implementation. Do not "fix"
the guard.

### Reaching the Controller Paks over USB: buildable, and the cost is a dependency

Revisited 2026-09-07. The conclusion above — that pak data cannot travel — is
true of *this* tool and its Dropbox transport. It is not true in general, and
the honest statement of the limit is that the route exists and was declined.

The `.mpk` files are reachable without a jailbreak. `UIFileSharingEnabled`,
already noted above, is exactly the flag that exposes an app's `Documents/` over
**AFC** (Apple File Conduit), and the paks live at
`Documents/Cores/Mupen64Plus/Saves/`.

**pymobiledevice3** is a pure-Python 3 implementation that runs on Windows and
provides `HouseArrestService`, which opens an AFC channel scoped to a single
bundle's container, with a `documents_only` flag limiting it to exactly that
directory. Read and write both.

What it would cost:

- **A third-party dependency**, in a project that currently has none. That is
  the real price, not the code.
- **Apple's usbmux service on Windows**, which arrives with iTunes or the Apple
  Devices app. pymobiledevice3 is pure Python but the transport is not: it talks
  to Apple's driver. A user without iTunes cannot use the feature.
- **A USB cable and a trusted pairing.** Nothing here goes over Dropbox; it is a
  wholly separate transport from everything else the tool does.
- **A device-present model.** Every other operation works against a folder on
  disk whether or not the phone is nearby. This one would not.

So it is not blocked, it is *expensive*, and the expense is architectural rather
than technical. The agreed shape, if it is built, is a **second executable in
the same repository** — which confines the dependency to the people who want the
feature and leaves the main tool standard-library-only. Do not add
pymobiledevice3 to the main tool.

### Building it, 2026-09-09

Built as agreed: a second executable in this repository, plus the discovery and
protocol that lets the main program notice it. Four findings are worth keeping,
three of them bugs that a phone would not have revealed any sooner.

**A process, not an importable plugin.** The obvious design — a folder added to
`sys.path` — cannot work. The main program is frozen around one embedded
CPython, and `pymobiledevice3` brings compiled wheels (`cryptography`) that load
only against exactly that interpreter's version and ABI. An importable add-on
moves the dependency problem from build time to run time, where it breaks the
program it was meant to extend. A process boundary also means a USB stall
waiting on a trust dialog cannot freeze the launcher's event loop.

**One executable does both jobs, and that was measured.** The add-on opens a
window when double-clicked and speaks JSON Lines when driven with `--json`. That
needs a windowed PyInstaller build to write to a stdout its parent hands it,
which is widely said not to work. A throwaway `--windowed` build that printed
JSON was read back through a pipe successfully, so the two-executable design
(console for the protocol, windowed for the window) was not needed.

**`subprocess` needs a valid stdin even when you only want stdout.** Redirecting
any of the three standard handles makes Windows require all three, and a
windowed build has no console — so the inherited stdin is invalid and the call
dies with `[WinError 6] The handle is invalid` before the add-on starts. It
would have shipped as "add-ons work from source and never in the packaged app".
Found because pytest replaces stdin the same way a windowed build does.
`stdin=subprocess.DEVNULL` fixes it, and is the right contract anyway.

**A timeout that kills only the named process is not a timeout.** `Popen.kill`
on Windows leaves grandchildren holding the pipes open, so the read after the
timeout blocks for as long as they live: a one-second timeout against a
thirty-second sleep returned after the full thirty, with the correct error
message and a window that would have been frozen for half a minute. Killing the
tree (`taskkill /F /T`) fixes it, and the test times the call rather than
checking the text.

**pymobiledevice3 11.x is async throughout, and does not look it.** Its AFC
methods carry a `@path_to_str()` decorator, so `inspect.iscoroutinefunction`
reports them as ordinary functions. Calling them as such fails with
`'coroutine' object is not iterable`. The real shapes: `usbmux.list_devices`,
`lockdown.create_using_usbmux`, `InstallationProxyService.get_apps` and every
AFC call are coroutines, and the container is vended by
`HouseArrestService.create(lockdown, bundle_id, documents_only=True)` rather
than by a constructor argument. Found by installing the library and running
`probe` with nothing plugged in.

**What is still gated on the cable**, and deliberately not guessed at: whether
`house_arrest` opens Delta's container on a real device, what Delta's bundle
identifier actually is (matched against the installed-app list rather than
hard-coded, because App Store, AltStore and sideloaded builds differ), and
**what the pak files are named**. That last one decides nothing structural — the
reader tells the one-file-of-four shape from the four-separate-files shape by
**size**, since a pak is 32,768 bytes on every build, and refuses anything that
is neither rather than guessing.

**A cheaper prize sits next to it.** Standalone mupen64plus (and Project64)
write **separate** `.eep` / `.sra` / `.fla` / `.mpk1-4` files — which is exactly
the shape Delta stores. Only RetroArch's core packs them into one 296,960-byte
`.srm`. So a standalone N64 target needs no conversion at all, and Controller
Paks would be plain file copies *if* they could be fetched. See "Standalone
emulators" below.

## Save states: not a proprietary format, a version lock

Two separate questions live under this heading, and only one of them is out of
scope. **Syncing states between Delta and RetroArch** stays out — though for a
different reason than the one originally recorded, and the difference matters
enough to write down. **Recovering the battery save out of a state** is a
different question with a different answer, and it is built.

It had been settled as impossible on the strength of `syncableKeys` carrying
`coreIdentifier` and `coreVersion`, plus Delta's user-facing FAQ ("Delta save
states and cheat files are only compatible with Delta"). The FAQ is a
simplification, and the conclusion drawn from it was too strong.

A `.svs` for DS **is a melonDS save state**, byte for byte, with no Delta
wrapper of any kind.

The layout below was re-read on 2026-09-08 from **melonDS 0.9.5's own
`Savestate.cpp` and `NDSCart.cpp`** — the version Delta 1.6 ships — rather than
from a description of it. That mattered: the description recorded here
previously got two things wrong, and either would have produced a parser that
returned confident nonsense.

File header, 16 bytes, and every section header the same:

```
file header                     section header
00  magic "MELN"                00  section magic
04  version major (u16 LE)      04  section length (u32 LE)
06  version minor (u16 LE)      08  reserved
08  total file length (u32 LE)  0C  reserved
0C  reserved
```

**A section's length includes its own 16-byte header.** `Savestate::Section`
writes `len = pos - CurSection`, measured from the start of the header, and its
reader seeks `length - 8` after consuming the magic and the length. The first
section begins at `0x10`; a zero magic ends the walk. Version major is `9` for
0.9.5, and melonDS refuses a state whose major differs or whose declared length
does not match the file size.

**The battery save is in section `NDCS`, not `NDSC`.** This is the first thing
the earlier note got wrong. They are transpositions of each other and they sit
next to each other in the file, which is how "the save is after NDSC" came to be
written down — true as a statement about position, and a trap as a statement
about which section to key on:

- **`NDSC`** is `NDSCart::DoSavestate` — the cart *controller*. SPI registers,
  `TransferData[0x4000]`, cart type and checksum. This is the ~16 KB section.
- **`NDCS`** is `CartCommon::DoSavestate` — the cart *itself*, and
  `CartRetail::DoSavestate` appends the SRAM to it immediately after.

Inside `NDCS`, counting from the start of its header:

| Offset | Field |
| --- | --- |
| `0x00` | section header, 16 bytes |
| `0x10` | `CmdEncMode` (u32) |
| `0x14` | `DataEncMode` (u32) |
| `0x18` | `DSiMode` (`Bool32`, written as u32) |
| `0x1C` | `SRAMLength` (u32) |
| `0x20` | **the battery save**, `SRAMLength` bytes |
| | then `SRAMCmd` (u8), `SRAMAddr` (u32), `SRAMStatus` (u8) |

So the save starts at `NDCS + 0x20` and its length is stated four bytes earlier.
The second thing the earlier note got wrong was placing that length at "+12 of a
24-byte metadata header" — there is no separate metadata block, just three mode
words ahead of the length field. The "24 bytes" is what sits between the section
magic and the length, which is a true count and a misleading description.

The save sizes are not open-ended either. `CartRetail::SetupSave` picks from a
fixed `sramlen[]` table, so a length outside it means the field being read is not
the length field:

| Bytes | Type | | Bytes | Type |
| --- | --- | --- | --- | --- |
| 512 | EEPROM 4 Kbit | | 262,144 | FLASH 2 Mbit |
| 8,192 | EEPROM 64 Kbit | | 524,288 | FLASH 4 Mbit |
| 65,536 | EEPROM 512 Kbit | | 1,048,576 | FLASH 8 Mbit |
| 131,072 | EEPROM 1 Mbit | | 8–64 MB | NAND |

524,288 is the size of the real Pokémon Platinum save measured from Delta, and
`SetupSave` fills a fresh buffer with `0xFF` — which is exactly the flash padding
seen in the tail of that file. Two independent details of this table already
agree with something measured.

So the blocker is not the container. It is that a save state is a memory dump
whose layout changes whenever the emulator does — Delta's own FAQ records that
updating melonDS 0.9.4 → 0.9.5 broke every existing state, which is the same
fact from the other direction.

**Measured, not assumed:** RetroArch's core here is **melonDS DS 1.3.0**
(`display_version` in `melondsds_libretro.info`, `savestate_features =
serialized`), wrapping melonDS 1.x. Delta 1.6 ships melonDS **0.9.5**. Different
major versions, so states do not interchange *today* — but "the versions do not
match" is a very different claim from "the format is proprietary", and if they
ever converge the barrier disappears on its own without anyone building
anything.

**The immediately useful part, which does not depend on any of that: built
2026-09-08** as `savestate.py`, with `extract-save` on the CLI. It lifts the
battery save out of a state and writes it beside the state as a `.sav`. That is
a real recovery path for someone whose only copy of a save is inside a state,
and it is worth having whatever happens to state interchange.

It is deliberately outside every sync path. It reads one file the user names and
writes one new file; it never touches Delta's folder, RetroArch's saves or the
manifest, so a last-resort recovery cannot damage anything that currently works.

**Confirmed on a real Delta save state, 2026-09-08.** Everything above was read
from melonDS's source first, then measured. The measurement agreed with all of
it. A manual state of Pokémon Platinum, synced from the phone:

| | |
| --- | --- |
| File size | 19,643,269 B — and the header's declared length says the same |
| Magic | `4d 45 4c 4e` = `MELN`, **at offset 0** — no Delta wrapper |
| Version | `(9, 0)` — `SAVESTATE_MAJOR` 9, so Delta 1.6 ships melonDS 0.9.5 |
| Sections | 26, walked cleanly: `NDSG DMA0…DMA7 ARM9 CP15 ARM7` **`NDSC` `NDCS`** `GBAC GPUG GP2A GP2B GP3D SPU. SPIG SPFW SPPW SPTS RTC. WIFI` |
| Extracted | 524,288 B at `NDCS + 0x20`, FLASH 4 Mbit, retail cart |
| Cross-check | **SHA-1 identical** to `GameSave-0862ec35…-gameSave` |

Note `NDSC` and `NDCS` sitting adjacent in that list. The transposition was not
a hypothetical: keying on the wrong one lands in the cart controller's 16 KB
transfer buffer and returns something that looks like a save.

The strict validation stays as it was — magic, version major, declared length
against the real one, `NDCS` present, the length field against the fixed size
table, and the save fitting inside its section. It cost nothing to keep and it
is what makes a future failure legible.

**Save states do sync, so getting a real one needs no cable.** Checked
2026-09-08: `SaveState` conforms to `Syncable` with `syncableFiles` of
`saveState` and `thumbnail`, so a state lands in the same flat folder as
everything else:

```
SaveState-<uuid>            JSON record
SaveState-<uuid>-saveState  the .svs itself
SaveState-<uuid>-thumbnail  the preview PNG
```

with one condition, from `SaveState.isSyncingEnabled`:

```swift
(self.type != .auto && self.type != .quick) && (game is not the melonDS BIOS)
```

**Auto-saves and quick-saves are excluded.** Only a *manual* save state syncs —
the kind made from the pause menu into a numbered slot. The absence of any
`SaveState` record in this folder therefore says the user does not keep manual
states, which matches the brief, and not that states are unsyncable.

`syncableKeys` carries `coreIdentifier` and `coreVersion`, which is the detail
the original "states can never work" conclusion was built on. Worth noting that
it is a true statement about *interchange* and says nothing about reading the
file, which is the distinction this whole section turns on.

**The first real run checks itself.** When the state is still in Delta's synced
folder, `extract-save` follows `SaveState-<uuid>` → `relationships.game.identifier`
→ `GameSave-<sha1>-gameSave` and compares what it recovered against Delta's own
battery save for that game. Byte-identical is the result that promotes this from
"parses" to "verified" — and it costs nothing to check, so it happens unasked.
Three outcomes, each meaning something different:

| Result | Reading |
| --- | --- |
| Identical | The extraction is correct. This is the experiment succeeding. |
| Same length, a few bytes differ | Expected — the game wrote to SRAM after its last in-game save. Save in-game immediately before making the state to avoid it. |
| Different length | Should not happen. Investigate before trusting either file. |

Two further diagnostics are built in for that first real file, because that run
is also the experiment:

- If `MELN` is missing from offset 0 but present later, it says at what offset.
  That would mean Delta wraps the state after all, contradicting the finding
  this is built on — worth hearing about rather than working around.
- If `NDSC` is present without `NDCS`, it says so specifically, rather than
  reporting a generic parse failure.

### The other five systems: settled, 2026-09-08

A manual save state was made on the phone for every system and the headers read
off the real files. **The "it is just the emulator's own state" finding holds for
all six.** Not one of them carries a Delta wrapper — every file begins with its
emulator's own magic:

| System | Emulator | First 4 bytes | What it is |
| --- | --- | --- | --- |
| DS | melonDS | `4d 45 4c 4e` `MELN` | Sectioned, self-describing |
| SNES | snes9x | `23 21 73 39` `#!s9xsnp:0009` | Text header, then named chunks |
| NES | nestopia | `4e 53 54 1a` `NST\x1a` | Chunked, `NFO` block first |
| N64 | mupen64plus | `1f 8b 08 00` | **gzip** — and holds no save at all |
| GBA | visualboyadvance-m | `1f 8b 08 00` | **gzip** — same |
| GBC | gambatte | `00 01 00 00` | Labelled fields, no magic at all |

Sizes ranged from 13 KB (Kirby's Adventure) to 19.6 MB (Platinum), which is
mostly a statement about how much RAM each console has.

### All of them built, 2026-09-08

Five extract. The sixth has nothing to extract. Every one was checked against
Delta's own battery save for that game, and every one came back **byte-for-byte
identical**:

| Emulator | Verified on | Save location | Length from |
| --- | --- | --- | --- |
| melonDS | Pokémon Platinum, 524,288 B | `NDCS + 0x20` | the state |
| gambatte | Pokémon Crystal, 32,768 B | field `sram` | the state |
| snes9x | Super Mario World, 2,048 B | prefix of block `SRA` | Delta's record |
| nestopia | Kirby's Adventure, 8,192 B | prefix of chunk `WRM` | Delta's record |
| visualboyadvance-m | Pokémon Fire Red, 131,072 B | computed offset 594,460 | Delta's record |
| mupen64plus | Paper Mario | **not in the file** | — |

Three things came out of doing all five that were not obvious from the headers.

**Only two formats say how big the save is.** melonDS writes `SRAMLength` and
gambatte gives `sram` its own field length, so those two work on a state copied
anywhere. snes9x writes `Memory.SRAM_SIZE`, a compile-time constant — 0x20000 in
Delta's build, 0x80000 today — and nestopia writes the whole of `GetWram()`;
neither records how much of that buffer is the cartridge's. VBA-M records
nothing at all. So three of the five need the length from Delta's record, which
is the rule `n64.py` already follows, and refuse without it. Guessing from the
filler would have worked on every file measured and would silently truncate a
save that legitimately ends in a run of one byte.

**VBA-M cannot be found by searching, and that was measured rather than
assumed.** Its state is a flat run of raw writes with nothing naming or sizing
them, so the save is reached only by adding up everything written before it.
Scanning the real 2 MB state for a plausible `flashSaveData3` gives **144
matches**, exactly one of which is right. The offsets are therefore computed
from the revision Delta actually ships — `GBADeltaCore` pins the submodule at
453fa0de, whose `CPUWriteState` differs from current upstream — and then
*checked* before being trusted, because computing against upstream master lands
about 500 bytes off, which is the near-miss that returns plausible garbage.

**N64 was answered by the oracle before the source explained it.** Delta's
131,072-byte Paper Mario save appears nowhere in the 16,793,412 decompressed
bytes of its state, in either byte order. `savestates.c` then said why: it
writes the flashram controller's registers and none of the storage, and the
words *eeprom*, *mempak* and *sram* do not occur in it at all. That is a
permanent absence, not a missing feature, and the tool says so in those terms.

Incidental, found on the way: **Delta's N64 saves are 32-bit word-swapped.**
Paper Mario's begins `iraMtS o yro`, which is "Mario Story" in four-byte groups.
Both sides use the same convention so `n64.py` is unaffected, but it is worth
having written down.

### Installing a recovered save, 2026-09-09

Recovery originally stopped at a file in `recovered/`, leaving the copy over
RetroArch's own save to be done in Explorer. That was the only step in the flow
with no backup behind it, and it lands on the file holding the progress being
rescued — so the tool does it now, taking the same rolling backup every other
write takes. It goes through `sync.backup`, which means the replaced save
appears on the existing Backups tab as an ordinary restore point and needed no
new restore code at all.

**The target is found, not computed, whenever RetroArch already has that save.**
Computing it means knowing which core RetroArch picked and whether
`sort_savefiles_enable` is on, and both can have changed since the file was
written — this is the same reasoning `restore.find_retroarch_target` already
uses. A path is only constructed when nothing was found, and the confirmation
says so, because a constructed path is right only if the ROM is named the way
this tool names it.

**Two same-named files is the case worth refusing.** Turning
`sort_savefiles_enable` on and then off leaves a copy loose in the save folder
*and* one inside a core's subfolder, and only one of them is the file RetroArch
reads. Writing into the wrong one looks exactly like success and changes
nothing, which is the worst outcome available, so the search returns every match
and more than one is a refusal naming both paths.

**Installing is refused while RetroArch is running**, which the recover-to-file
path never had to care about. RetroArch writes the loaded game's SRAM when it
closes, straight over anything installed underneath it — silently, and looking
exactly like the recovery having failed.

**N64 is refused for a second, independent reason.** RetroArch keeps the
cartridge save and four Controller Paks in one combined `.srm`, so a bare
cartridge save written over it erases the paks. Unreachable today, since no N64
state holds a save to recover, and asserted anyway: the day something makes it
reachable, the damage is silent and total.

Checked on 2026-09-09 against a copy of this machine's real RetroArch save
folder, all five recoverable games: every target was **found** rather than
constructed, each inside its core's subfolder (`sort_savefiles_enable` is on
here), and in every case the save RetroArch already held was **byte-for-byte
what the state produced**. Three independent copies agreeing — the save state,
Delta's `GameSave` record, and RetroArch's own file.

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

## Standalone emulators instead of RetroArch

Surveyed 2026-09-07; **built 2026-09-10** as `emulators.py` plus
`sync.sync_emulator`. Mostly a discovery-and-naming problem rather than a format
one — with two traps that must be measured rather than assumed.

The survey's prediction held: it was another table plus its own discovery, not a
new architecture. Two things it did not predict:

- **The manifest needed a per-target *pair*, and getting that half-right was
  worse than not doing it.** `Entry` had one desktop side, so `Entry.targets`
  keyed by emulator was the obvious fix. It is not enough: the agreement is a
  *pair* — what Delta looked like **and** what the target looked like — and
  leaving the Delta half shared silently starves every target but the first.
  RetroArch pulls a new save and records "Delta agreed"; mGBA is reconciled a
  moment later, sees an unchanged Delta, and keeps the old save while reporting
  "unchanged on both sides". Whichever target ran first decided for the rest.
  Two further traps in the same area, both caught only by running it:
  - Keying `decide`'s first-sync branch on the entry rather than on *this
    target* was a data-loss path: a newly enabled emulator holding its own save
    read as "Delta unchanged, target changed" and **pushed that save to the
    phone** over the real one, with no conflict reported.
  - **Targets are reconciled in order, so a push from a later one leaves every
    earlier one stale** — while the run reports success, which is what makes it
    dangerous rather than untidy. Play RetroArch before the next sync and that
    push has silently arranged a conflict. `sync.settle` reconciles everything
    once more when Delta actually moved. Exactly one extra pass, capped
    structurally: a second push in the second pass is impossible (Delta has
    moved since every remaining agreement, so a divergent target is a conflict
    now), but a reconcile that *can* iterate is one that can iterate forever,
    and this runs against real saves.
  - **Backups assumed one desktop target too.** mGBA and VBA-M both write
    `Pokemon.sav`, so in the flat backup folder their restore points arrive
    under one name -- losing which emulator it came from, where to put it back,
    and its own history, since `backup` prunes the last ten *by name* and the
    two delete each other's. Standalone backups now go to
    `backups/<emulator key>/`; RetroArch keeps the flat folder it has always
    had, so every backup taken before this reads unchanged.
  - The whole suite passed before all four fixes. None of these bugs is
    reachable with one target, which is what every existing test had.
- **The gate is per-save, not per-emulator.** Nothing here has run any of these
  emulators, so `check_shape` measures the file the emulator has already written
  before replacing it — size, gzip magic, DeSmuME footer. That is stronger than
  the table, because it is evidence from the emulator rather than a claim about
  it. Nestopia, DeSmuME and Project64 are additionally blocked outright.

| Delta core | Standalone equivalent | Save shape | Note |
| --- | --- | --- | --- |
| visualboyadvance-m | mGBA | `.sav` beside the ROM | RetroArch sorts into folders; standalone does not |
| gambatte | mGBA / SameBoy | `.sav` + clock | **`.rtc` layout differs per emulator** — see the clock section |
| nestopia | Nestopia UE | `.sav` | **may be gzip** — see below |
| snes9x | Snes9x | `.srm` beside the ROM | matches |
| mupen64plus | mupen64plus / Project64 | separate `.eep`/`.sra`/`.fla`/`.mpk` | **no conversion needed**, unlike RetroArch |
| melonDS | melonDS | `.sav` in `Documents\melonDS\saves` | configurable |

The two traps:

- **Nestopia's `.sav` is gzip-compressed on some platforms.** The nesdev thread
  reporting it also carries the correction that Windows builds write raw and the
  macOS build compresses. Windows is the only platform this tool targets, so it
  is *probably* raw — and "probably" is not the standard `ENABLED_SYSTEMS` is
  held to. Measure a real file before enabling.
- **Saves live beside the ROM** for most standalone emulators, not in a central
  folder. There is no equivalent of RetroArch's `savefile_directory`, so
  discovery would key off each emulator's own config, and there is no single
  convention across them.

Note the N64 row: standalone mupen64plus stores exactly the shape Delta stores,
so a standalone N64 target is **easier** than the RetroArch one, not harder. It
is the only place in this project where dropping RetroArch removes work.

The structural change needed is smaller than it looks. `systems.py` already
separates "which core" from "what layout", and `System.converted_cores` already
exists precisely to say *this layout has been checked against this core*. A
standalone target is another entry in that table plus its own discovery, not a
new architecture.

## Safety constraint

Delta's documentation warns that files in the Dropbox folder are not intended to
be edited and that manual edits may cause data loss. This is not merely
cautionary: Harmony reconciles against Dropbox file *revisions*, so writing into
that folder out of band desyncs Delta's own state machine — which is not a
prediction here but a description of what happened twice, in the two sections
above.

**The folder is therefore read-only except on two paths**, both of which exist
because syncing RetroArch → Delta is impossible without them:

| Path | What it writes | Guard |
| --- | --- | --- |
| `push_save` | the save file, then its record | in-place write, size guard, backup first, re-read and re-derive after |
| `push_cheat` | the cheat record only | in-place write, backup first; no attached file, so no revision to resolve |

Everything else — discovery, inspection, ROM export, cheat export, `doctor` —
opens the folder read-only and always has. An earlier version of this document
described the folder as "strictly read-only by this tool", which was true when
written and stopped being true when the push was built.

The two rules those paths obey are not conventions, they are the findings above:
**write in place, never rename over the target**, and **never recompute the
record's own hash**. Both are enforced in `delta_writer.py` and both have a
section here explaining the failure that produced them.

## Sources

Every finding above was read out of one of these, or measured on this machine
against a real file. Where the two disagreed, the measurement won — that
happened for the DS save format, the Crystal clock base, and the N64 size guard.

**Delta and Harmony**

- <https://github.com/rileytestut/Delta>
- <https://github.com/rileytestut/Harmony>
- <https://github.com/rileytestut/Harmony-Dropbox>
- <https://github.com/rileytestut/Delta/issues/429>
- <https://github.com/rileytestut/delta/issues/533>
- <https://faq.deltaemulator.com/using-delta/delta-sync>
- <https://faq.deltaemulator.com/using-delta/nintendo-ds/incompatible-save-states>

**Dropbox**

- <https://github.com/dropbox/dropbox-api-spec/blob/main/file_properties.stone>

**Cores and save formats**

- <https://docs.libretro.com/library/mupen64plus/>
- <https://docs.libretro.com/library/melonds_ds/>
- <https://github.com/drehren/ra_mp64_srm_convert>
- <https://github.com/JesseTG/melonds-ds/discussions/168>
- <https://github.com/lautixhx/svs-sav-converter>
- <https://github.com/melonDS-emu/melonDS/blob/0.9.5/src/Savestate.cpp>
- <https://github.com/melonDS-emu/melonDS/blob/0.9.5/src/Savestate.h>
- <https://github.com/melonDS-emu/melonDS/blob/0.9.5/src/NDSCart.cpp>
- <https://melonds.kuribo64.net/comments.php?id=192>
- <https://forums.desmume.org/viewtopic.php?id=1678>
- <https://forums.nesdev.org/viewtopic.php?t=5262>

**iOS device access** (surveyed, not used)

- <https://github.com/doronz88/pymobiledevice3>
- <https://doronz88.github.io/pymobiledevice3/installation/>
