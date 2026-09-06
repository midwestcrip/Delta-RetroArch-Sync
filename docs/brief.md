# Project: Delta ↔ RetroArch Auto Sync (Saves, ROMs, Cheats)

## Goal
Build an automatic, background, bidirectional sync tool that keeps the
following in sync between:
- **Delta** (iOS emulator, already syncing via Delta Sync → Dropbox)
- **RetroArch** (Windows desktop)

What it syncs:
1. Battery/SRAM save files
2. ROM files
3. Cheat codes

No manual steps once running. It should just work in the background.

## Explicit scope (read this first)
- **Sync save files, ROMs, and cheats.** Nothing else.
- **DO NOT sync save states.** User does not use save states (uses
  RetroAchievements instead, which requires hardcore/no-savestate play on
  most systems anyway). Save states between Delta and RetroArch use
  completely different internal formats and are explicitly out of scope —
  don't build for this even as a "nice to have."
- **DO NOT sync controller skins or configs.** These are fundamentally
  different, incompatible formats between the two apps (Delta's
  `.deltaskin` is a zip of JSON + PDF vector art for touchscreen overlays;
  RetroArch's overlay system is PNG + `.cfg`, a different rendering concept
  entirely — there's no shared representation to sync between them).
  RetroArch's config format also has no meaningful 1:1 mapping to Delta's
  app settings. Building converters for either would be a real asset/format
  reverse-engineering project for something that's set once per app and
  never needs to stay live-synced. Not worth it — don't attempt.

## Systems in use
NES, SNES, N64, GBC, GBA, Nintendo DS (Delta's supported systems — confirm
with user which of these they actually have active save files for, to scope
the core-mapping table below).

## Background / already-confirmed facts
- Delta has a built-in "Delta Sync" feature (Settings → Delta Sync) that
  syncs games, saves, skins, and BIOS files to either Google Drive or
  Dropbox.
- **Use Dropbox, not Google Drive**, for this project. Delta's own docs
  state that files synced via Google Drive are NOT visible/accessible as
  normal files, but files synced via Dropbox ARE visible as real files in
  the Dropbox folder structure.
- Delta's docs also warn: files in that Dropbox folder are "not intended to
  be edited" and manual edits "may cause data loss." This means our tool
  must be careful — copy, don't move; never write directly into Delta's
  Dropbox folder structure if avoidable; treat it as read-mostly on our end.
- Assumption: user will have the Dropbox desktop client installed and
  running on the Windows machine, so Delta's synced folder will have a
  local mirror path on disk (e.g. under `C:\Users\<user>\Dropbox\...`).
  Confirm the actual local path once Dropbox is set up — don't assume an
  exact subfolder name, verify by inspecting the folder after Delta has
  synced at least once.
- Delta Sync already includes ROM files ("games") as part of what it
  pushes to Dropbox — this means ROM sync doesn't need new sync
  infrastructure, it can reuse the same file-matching/copy logic as saves,
  just pointed at ROM files instead. No format conversion needed since
  it's the same ROM file on both ends, just needs to land in the folder
  RetroArch expects to find ROMs in.
- Cheat codes are also a case of reusable content rather than a new
  concept: Delta's supported cheat types (Game Genie, Action Replay,
  GameShark, Code Breaker, depending on system) are standardized
  per-console cheat device formats. RetroArch's `.cht` files are plain
  text (`cheat0_desc`, `cheat0_code`, `cheat0_enable`, etc.) and many
  libretro cores accept these same encoded codes directly. The real work
  is extracting Delta's stored cheat list (format not obviously exposed —
  needs inspection) and writing it out in RetroArch's `.cht` syntax.

## Unknowns to verify at build time (do not guess, inspect directly)
1. Exact subfolder path/structure Delta creates inside Dropbox, and the
   naming convention/extension it uses for save files per system.
2. RetroArch's save folder path on Windows (default is a `saves/` folder
   under the RetroArch install/config directory, but this can be
   redirected in RetroArch's settings — check the user's actual
   `retroarch.cfg` for `savefile_directory`, and whether "sort saves into
   folders by core name" is enabled, which changes the path structure).
3. Per-core save file extension RetroArch uses (commonly `.srm`, but some
   cores use other extensions — don't assume, check each core actually in
   use).
4. Which core Delta uses per system vs. which core the user has configured
   in RetroArch for that system — for save file compatibility these should
   be the same underlying emulation core family (e.g. both using
   Gambatte for GBC, both using the same GBA core, etc.). If cores don't
   match, saves likely won't be compatible even after renaming, and that's
   a limitation to flag to the user, not silently paper over.
5. RetroArch's expected ROM directory/directories on Windows (RetroArch
   doesn't require ROMs in one fixed folder — check the playlist/content
   database setup and how the user actually has it configured before
   assuming a path).
6. How Delta actually stores its cheat list internally (inspect what
   lands in the Dropbox mirror — likely a database file or per-game
   metadata rather than a plain text file) so it can be parsed reliably
   rather than guessed at.
7. Whether the specific cheat codes in use are stored by Delta in the
   same raw encoded form (e.g. the literal Game Genie string) or in some
   decoded/internal representation — this determines whether the cheat
   sync is a straight reformat or needs an actual decode step.

## Sync trigger model
- User wants this to behave like Delta Sync feels from the user's side:
  it syncs around when you open the app, not as a constantly-running
  background watcher.
- Concretely: hook the sync into RetroArch's process lifecycle — run a
  sync pass right before RetroArch launches, and another right after it
  closes.
- **Crash resilience matters — the pre-launch step must do real two-sided
  conflict detection, not just "push local changes first, then pull."**
  If RetroArch crashes or is force-killed instead of closing cleanly, the
  post-sync push never runs, and a naive "just pull on launch" design
  would silently strand that session's save progress. But a naive
  "always push local changes before pulling" fix has its own gap: if the
  user also played on iOS in the meantime (before ever relaunching
  through this tool on desktop), both sides changed independently since
  the last known-good sync, and blindly pushing local first would
  overwrite the newer iOS progress instead of catching the conflict.
  Correct logic:
  - Keep a manifest recorded at the end of every successful sync — a
    hash or last-modified timestamp per synced file, representing the
    last known-good state both sides agreed on.
  - On launch, compare BOTH the local RetroArch file and the Dropbox
    mirror file against that manifest.
    - Only local changed since last sync → push local to Dropbox.
    - Only remote changed since last sync → pull remote to local.
    - Neither changed → nothing to do.
    - BOTH changed since last sync → genuine conflict. Do not silently
      pick one. Flag it (log clearly, and ideally surface it to the user
      somehow) rather than guessing — silently resolving a real conflict
      is how progress quietly gets lost.
  - This is what actually closes the crash gap without introducing a new
    silent-overwrite risk — not a bigger architectural change like a
    background daemon.
- Simplest implementation is a small launcher/wrapper: the user launches
  RetroArch through this tool (a shortcut/script) instead of launching it
  directly. It runs the pre-launch reconcile, launches RetroArch and
  waits for it to exit, then runs the post-close push. No persistent
  background process needed.
- This is simpler to build and reason about than a continuous file
  watcher, and matches the user's actual mental model — go with this
  instead of a background daemon unless there's a strong reason not to.

## Core sync logic
- Sync runs at the trigger points above (RetroArch launch/close), not
  continuously. Each sync pass compares the local Dropbox mirror path
  (Delta's files) against the corresponding local RetroArch directories,
  for each of the three synced content types (saves, ROMs, cheats).
- Match by ROM name (filename minus extension) — this is the reliable
  link between a Delta save/ROM/cheat set and its RetroArch counterpart
  for the same game.
- On change on either side, copy (with rename/extension/format conversion
  as needed per content type) to the other side.
  - Saves and ROMs: direct file copy, extension conversion where needed.
  - Cheats: parse Delta's cheat list for a game and write/update the
    matching RetroArch `.cht` file (and the reverse direction if cheats
    are ever added on the RetroArch side).
- Use the same manifest-based comparison described above to resolve
  conflicts — never silently overwrite a newer file with an older one.
  This matters most for saves; ROMs and cheats change far less often but
  should follow the same rule for consistency.
- Keep a simple log of every sync action (content type, source,
  destination, timestamp) so issues are debuggable.
- Keep a rolling backup of the last N versions of each save (and cheat
  file) before overwriting, in case a sync goes wrong — data loss is the
  failure mode to design against. ROMs don't need version backups, just
  a check that a copy completed fully before treating it as synced,
  given their larger file size.

## Platform / runtime
- Windows. Runs on the launch/close trigger model described above, not as
  a persistent background service — see "Sync trigger model."
- Should be as close to zero-effort as possible day to day: ideally the
  user just double-clicks one shortcut/icon like they normally would to
  play, and syncing happens invisibly around that.
- Language/approach is Claude Code's call — pick whatever's simplest to
  build and maintain (a Python wrapper script is a reasonable default).

## Version control
- Set up as a git repo with auto-push to GitHub configured — user will
  designate the specific repo later. Get this wired up (git init, remote
  added, auto-push on commit or whatever cadence Claude Code judges
  sensible) once the repo URL is provided; don't block other progress on
  this, but don't skip it either — it's a standing preference of the
  user's for all his projects, not unique to this one.

## Not needed
- No RetroArch or Delta source modification — this is a standalone
  syncing tool that only touches files on disk (saves, ROMs, cheat files).
- No cloud API integration beyond what's needed to read the local Dropbox
  mirror — Dropbox desktop client is already doing the cloud part.
- No skin or config syncing/conversion — see scope section above for why.
