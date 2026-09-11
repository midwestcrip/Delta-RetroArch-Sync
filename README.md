# Delta-RetroArch Synchronizer

Keeps battery saves, ROMs and cheats in sync between
[Delta](https://deltaemulator.com) on iOS and RetroArch on Windows, with no
manual steps once it is running.

Delta already syncs to Dropbox, and the Dropbox desktop client already mirrors
that to disk. This tool is the missing piece in the middle: it reconciles
Delta's mirrored folder against RetroArch's directories around the moments you
actually play.

**Not in scope:** syncing save states, controller skins, and app configs. Skins
and configs have no shared representation between the two apps to convert
between. Save states are a narrower story than "incompatible formats" — a Delta
`.svs` turns out to be the emulator's own save state byte for byte, and what
stops them interchanging is that Delta and RetroArch ship *different versions* of
the same emulator. The battery save inside a state **can** be recovered, and
that is built — see [Getting a save back out of a save state](#getting-a-save-back-out-of-a-save-state)
and [docs/research.md](docs/research.md#save-states-not-a-proprietary-format-a-version-lock).
The original brief is kept at [docs/brief.md](docs/brief.md) as a historical
record, annotated where it turned out wrong.

## Status

Bidirectional save sync works, confirmed on a real device in both directions for
all six systems: progress made on the phone appears in RetroArch, and progress
made in RetroArch appears in Delta. ROM export and cheat export work, and the
launcher window is what you actually use day to day — it syncs, launches
RetroArch, and syncs again once it closes.

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

**Renaming works from either side too.** A `.cht` carries no identifier, so the
name is the only link back to Delta's record — which used to mean a rename broke
it and the cheat reappeared as an uncreatable RetroArch-only one. The tool now
remembers each cheat's last agreed name and re-pairs them through progressively
weaker signals: the exact name, then the name they last agreed on, then the code,
then elimination where exactly one cheat is unpaired on each side. So editing a
cheat's name and code at once works. Renaming two at once in a way that leaves
the pairing genuinely ambiguous does not, and is reported rather than guessed.

| | State |
| --- | --- |
| Save sync, Delta → RetroArch | Working, verified on device |
| Save sync, RetroArch → Delta | Working, verified on device — needs `auth` |
| Game Boy Color clock sync | Working, Gambatte core only |
| N64 save conversion | Working, Mupen64Plus-Next only |
| ROM export | Working |
| Cheat export, Delta → RetroArch | Working |
| Cheat editing and renaming, both ways | Working |
| Creating a cheat in RetroArch | **Impossible** — see above |
| Read-only inspector (`inspect`) | Working |
| Health checks (`doctor`) | Working |
| Rolling backups and `restore` | Working |
| Launcher window | Working |

**Every system Delta supports now syncs: GBA, SNES, GBC, NES, DS and N64.** Each
was enabled only once a real save had been inspected for a header, footer or
wrapper — being the same plain-copy path was never the bar. Game Boy Color also
syncs its real-time clock, on the Gambatte core only.

Five of the six are a copy with a different extension. **N64 is the one real
conversion**: RetroArch's mupen64plus-next keeps a single 296,960-byte `.srm`
holding EEPROM, SRAM, FlashRAM and four Controller Paks at fixed offsets, while
Delta writes a bare dump of whichever storage the cartridge has. The mapping is
in `n64.py`, written against six real saves covering all four storage types.

That layout is **Mupen64Plus-Next's**, so use that core. ParaLLEl N64 plays fine,
but nothing has checked whether it stores saves the same way — it gets no sync
and a message saying which core to switch to, rather than a save written in a
layout it may not read.

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

> **Getting RetroArch.** Its download page carries advertisements dressed up as
> download buttons, including one that opens when you click the real *Download
> stable build* link. The genuine downloads are the small platform links under
> the version heading, and an ad blocker makes the page much easier to read.

## Install

### Windows will warn you the first time

You will see **"Windows protected your PC"**. Click **More info**, then
**Run anyway**.

This is SmartScreen, and it appears for every program that is not code-signed
and that Windows has not seen many people run yet. A signing certificate is the
only thing that removes it. Nothing is wrong with the download — verify the
SHA-256 on the release page against your copy if you would rather check than
take that on trust.

### Then

**Download the zip**, extract it, run the .exe inside. No Python, nothing else
to install. It keeps its settings, credentials and save backups in that folder,
so a portable location works fine. (In Program Files or anywhere else
non-writable it falls back to `%LOCALAPPDATA%`.)

The window finds Delta's Dropbox folder and your RetroArch install by itself.
If it cannot, the paths are editable in the window. Press **Instructions**,
bottom left, for the whole setup procedure with each step marked according to
what this PC already has.

**The first run offers to add shortcuts** to your Start menu and your desktop,
so you can find it again without going back to the folder you unzipped. It is
asked once, never assumed, installs for you alone, and needs no administrator
rights. The Settings tab has the same button, either way round, whenever you
change your mind.

Both locations are read from Windows rather than guessed, so a desktop
redirected into OneDrive — which is the default on a machine signed into a
Microsoft account — gets the shortcut where you can actually see it.

**Or run from source** — needs Python 3.11+ and nothing else; this tool has no
third-party dependencies. Double-click **`Delta-RetroArch Synchronizer.bat`** in
this folder, or add the shortcuts from the command line:

```
python -m delta_retroarch_synchronizer shortcuts
```

`--remove` takes them back out.

The launcher window finds Delta's Dropbox folder and your RetroArch install by
itself, shows what it found, and gives you one button: **Sync and Play**. That
syncs, launches RetroArch, waits for you to finish, and syncs again once it
closes — the save is copied after the process exits, because mGBA only flushes
it to disk on a clean quit. The window minimises while you play and comes back
when RetroArch closes.

If RetroArch is **already open**, it does not start a second copy: it syncs,
waits for that window to close, and syncs again. So it works around a RetroArch
you launched yourself, not only one it started.

Paths, and whether to sync ROMs and cheats, are editable in the window and
saved to `config.toml`.

There is also a CLI, if you prefer it:

```
python -m delta_retroarch_synchronizer inspect
```

Reports where it found Delta's Dropbox folder and RetroArch's config, then lists
every game Delta has synced with its SHA-1, save sizes, cheats, and whether
RetroArch already holds a matching save. It never writes.

The whole set:

| Command | What it does |
| --- | --- |
| `gui` | Open the launcher window — the same thing the shortcut does |
| `inspect` | Report what both sides hold. Never writes |
| `doctor` | Check that pushed saves are in a state Delta can act on |
| `sync` | Reconcile saves. `--dry-run` to see it first, `--push` to also write back into Delta |
| `backups` | List the saves and cheats that can be put back |
| `restore N` | Put one back. Writes nothing without `--yes` |
| `shortcuts` | Add the Start menu and desktop shortcuts. `--remove` takes them out |
| `extract-save` | Recover the battery save out of a Delta save state |
| `auth` | Authorise Dropbox once, so pushing can read file revisions |

### Getting a save back out of a save state

Save states do not sync, and are not going to. But the battery save sitting
*inside* one can be pulled out, which matters when a state is the only copy of
your progress left:

```bash
python -m delta_retroarch_synchronizer extract-save "Pokemon Platinum.svs"
```

That writes a plain `.sav` beside the state and touches nothing else — not
Delta's folder, not RetroArch's saves, not the manifest. Rename the result to
whatever the side you want it on expects.

**The launcher has a Save states tab**, which is the easier way to do this: it
lists every save state Delta has synced — game, slot, system, and whether it can
be read — so you pick one from a list instead of typing a path with a UUID in
it. Each row names the emulator that wrote the state, including for the states
that cannot be read.

**Install into RetroArch does the last step for you.** Recovering to a file
leaves you to copy it over RetroArch's own save in Explorer, and that was the
one step in the whole flow with no backup behind it — performed on the file
holding the progress you are trying to rescue. The button writes it for you and
copies what it replaces into the rolling backups first, so it comes back from
the Backups tab like any other restore point. Before writing it shows the exact
file and both sizes, and it says whether that file was *found* or worked out
from RetroArch's settings — a worked-out path is only right if the ROM is named
the way this tool names it. It refuses outright when RetroArch is running,
because RetroArch writes the loaded game's save when it closes and would undo
the install without saying anything.

**It checks its own work when it can.** A save state that synced from Delta sits
in the same folder as the record naming its game, and that game's battery save is
usually right there too — so the command compares what it recovered against
Delta's own copy and tells you whether they match byte for byte. That is the
difference between "the file parsed" and "the extraction is correct". A state
you copied somewhere else first simply gets no cross-check, and says so.

**Five of the six systems work, and every one was verified against a real save
state** on 2026-09-08 — each recovered save came out byte-for-byte identical to
Delta's own battery save for that game:

| System | Emulator | Verified against |
| --- | --- | --- |
| Game Boy Advance | visualboyadvance-m | Pokémon Fire Red, 131,072 B |
| Super Nintendo | snes9x | Super Mario World, 2,048 B |
| Game Boy / Color | gambatte | Pokémon Crystal, 32,768 B |
| NES | nestopia | Kirby's Adventure, 8,192 B |
| Nintendo DS | melonDS | Pokémon Platinum, 524,288 B |
| Nintendo 64 | mupen64plus | **nothing to recover** — see below |

Installing was checked against the same five on 2026-09-09, against a copy of
this machine's real RetroArch save folder: every game's target was *found*
rather than constructed, and the save RetroArch already held was byte-for-byte
what the state produced. That is a third copy agreeing — state, Delta's record,
and RetroArch's own file.

**N64 is not a gap, it is an absence.** A mupen64plus save state contains no
battery save at all: it stores the flash controller's registers and none of the
storage behind them, and keeps the cartridge's save in separate `.eep`/`.sra`/
`.fla`/`.mpk` files. The words *eeprom*, *mempak* and *sram* do not appear in
mupen64plus's save-state code. Nothing can be added later to change that, and
the tool says so rather than pretending it is unfinished work.

Installing refuses N64 for a *second*, independent reason: RetroArch keeps the
cartridge save and all four Controller Paks in one combined `.srm`, so writing a
bare cartridge save over it would erase the paks. That guard is unreachable
today — there is no N64 save to recover in the first place — and it is there
because the day anything makes it reachable, the damage would be silent.

Two of the formats — melonDS and gambatte — record how big their save is, so
they work on a state copied anywhere. The other three store a fixed-size buffer
and never say how much of it is the cartridge's, so they need the length: either
from Delta's record (automatic, when the state is still in Delta's folder) or
from `--size`. They refuse rather than guess, because guessing means handing
back a plausible-looking wrong file.

### Controller Paks (Nintendo 64)

Delta does not sync Controller Pak data — ghosts, extra save slots, anything a
game writes to a pak. `GameSave.syncableFiles` declares `gameSave` and, for Game
Boy Color only, `gameTimeSave`; there is no mempak entry, so the `.mpk` files
mupen64plus writes on the phone never reach Dropbox and nothing here can see
them. The cartridge save travels normally.

The feature is split in two, and **the half that needs nothing is built into the
main program**:

| | Needs | Where it lives |
| --- | --- | --- |
| Merging pak files into RetroArch's save | nothing | **Controller Paks** tab |
| Getting them off the phone | a cable, iTunes/Apple Devices, `pymobiledevice3` | separate download |

So you do not need the add-on at all. Delta sets `UIFileSharingEnabled`, which
means **On My iPhone → Delta → Cores → Mupen64Plus → Saves** is browsable in the
Files app and in Explorer — copy that folder off yourself and press *Merge from
a folder…*. The add-on only automates the copying.

**Merging is careful in two specific ways.** RetroArch's save is backed up
first, so it comes back off the Backups tab like any other write. And **a blank
pak never overwrites one that has notes on it** — an empty Controller Pak 2 on
the phone and a season of Mario Kart 64 ghosts in slot 2 on the PC is the
ordinary case, not the exotic one, and a straight copy would erase them. The tab
tells you which slots it will write, which it is keeping, and why.

Only the pak regions are ever touched. `n64.py` writes the cartridge save and
refuses to touch the paks; the pak code writes the paks and touches nothing
else, so every byte of the 296,960 has exactly one owner.

The add-on is its own program: double-click it for its own window, or let the
main program find it. It looks for `addon.json` beside itself or in an
`addons/` folder, and runs the add-on as a **separate process**, reading JSON
back — never importing it, because a frozen build cannot load another build's
compiled wheels and a USB stall must not be able to freeze the launcher.

Build it with `python tools/build_release.py --addon` (needs
`pip install pymobiledevice3`; the build says so loudly if it is missing).

If automatic discovery gets a path wrong, copy `config.example.toml` to
`config.toml` and override it. `config.toml` is gitignored, because this
repository is public and that file holds machine-specific absolute paths.

## Standalone emulators instead of RetroArch

RetroArch is the default target, not the only one. If you play on mGBA,
Mupen64Plus, Snes9x, melonDS, SameBoy or BGB, saves can be synced into those
too, and into several at once — each keeps its own agreed state with Delta, so
syncing a game to RetroArch *and* to mGBA is two independent agreements rather
than one fighting itself.

See what is here:

```bash
python -m delta_retroarch_synchronizer emulators
```

That reports every emulator it found, which systems each one would sync, where
its saves go and how that was worked out. It writes nothing. To actually sync
into one, name it in `config.toml`:

```toml
[emulators]
enabled = ["mgba", "mupen64plus"]
```

Being installed is deliberately not the same as being enabled. Having mGBA on
the machine is not a statement that these games should be synced into it.

**Nintendo 64 is the one case where dropping RetroArch removes work.** RetroArch's
mupen64plus-next packs EEPROM, SRAM, FlashRAM and four Controller Paks into a
single 296,960-byte `.srm`, so syncing to it needs a real conversion. Standalone
Mupen64Plus writes separate `.eep` / `.sra` / `.fla` files — which is exactly the
shape Delta already stores. So there is no conversion at all: the save is copied,
and its own size chooses which of the three files it is.

### Three emulators are recognised but refused

| Emulator | Why |
| --- | --- |
| Nestopia UE | Its `.sav` is compressed on some platforms and raw on others. Windows is reported to be raw, and "reported" is not the standard a save is held to here. |
| DeSmuME | Its `.dsv` is raw data plus a footer ending `\|-DESMUME SAVE-\|`. Delta's DS save shares the extension and has no footer, so this needs a conversion nobody has written. |
| Project64 | Some versions store SRAM and FlashRAM byte-swapped relative to Mupen64Plus. Use Mupen64Plus, where the bytes are known to match. |

Each one is still found and listed, with the reason printed. One real save file
measured from any of them is what would unblock it.

### Game Boy: the progress syncs, the clock does not

mGBA keeps a Game Boy game's real-time clock **inside the save file**, in 48
bytes after the save itself; Delta and RetroArch keep it in a separate file.
Those 48 bytes are dropped on the way to Delta and never rebuilt on the way
back, because mGBA writes its own whenever it opens a save that has none — the
save's own bytes come through untouched either way.

What that costs you is the clock, not the game: progress syncs in full, but a
game waiting real hours for berries or a daily event may think no time has
passed. The sync says so once, the first time it touches a Game Boy save.

### What clears a write

Most of these emulators have never been run by this project, so nothing is
written on the strength of a documented format. (mGBA and Mupen64Plus are the
exceptions — both were run against real ROMs, and the details they need were
measured from the files they produced rather than read out of their source.)

Where the emulator has **already written a save for that game**, that file is
measured before anything replaces it — its size, and whether it is
gzip-compressed or carries a DeSmuME footer. If it is not the same kind of file
as Delta's, nothing is written and the reason is printed. That is a stronger
check than any table, because it is evidence from the emulator itself rather
than a claim about it.

As everywhere else here, a save about to be overwritten is backed up first, and
a genuine conflict is reported rather than resolved.

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

- **Delta's Dropbox folder is read-only except on the two paths that must write
  to it.** Delta warns that editing it can cause data loss, and Harmony
  reconciles against Dropbox file revisions, so a careless write there desyncs
  Delta itself — which is not a worry, it is what happened twice during
  development. Syncing your desktop progress back to your phone is impossible
  without writing, so exactly two operations do: pushing a save, and rewriting a
  cheat. Both back the file up first, change only the bytes of the existing file
  rather than replacing it, and verify by re-reading afterwards. Everything else
  — discovery, `inspect`, ROM export, cheat export, `doctor` — never writes
  there at all.
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
python -m pip install pytest
python -m pytest tests -q
```

As of v0.3.0 the suite reports 630 passed tests and 702 passed subtests on
Windows. Pytest is a development-only dependency; neither application imports
it at runtime.

`python -m unittest discover -s tests` still runs and still passes, but **use
pytest**: unittest collects only the `TestCase` classes, which is 458 of the
630, and it reports that as success. The 172 it skips are written as plain
pytest functions, and silently covering less is a worse failure than refusing
to run.

The suite runs against a synthetic Delta folder built from the layout documented
in `docs/research.md`. Where real data has since been available it has been
diffed against those fixtures, and several of the fixtures were the thing that
turned out to be wrong — the DS save format and the N64 storage sizes both came
back from real files rather than from the layout as first written.
