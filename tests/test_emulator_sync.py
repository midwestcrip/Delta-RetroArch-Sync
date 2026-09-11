"""Tests for syncing a save into a standalone emulator.

Two things are being pinned here that the RetroArch path does not have to worry
about:

- **The second desktop side gets its own agreed state.** Syncing the same game
  to RetroArch and to mGBA is two agreements, not one, and mixing them would
  make each sync look like a change the other side had made.
- **The file already on disk is what clears the write.** Nobody here has run any
  of these emulators, so the only real evidence about what one writes is what it
  has already written.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from delta_retroarch_synchronizer import emulators
from delta_retroarch_synchronizer import inspect as inspect_module
from delta_retroarch_synchronizer import config as config_module
from delta_retroarch_synchronizer import manifest, sync, systems

GBA_SAVE = b"\xa5" * 131072
SRAM = b"\x77" * 0x8000


def entry_for(key, name, save_path, rom_path=None):
    system = systems.SYSTEMS[key]
    return inspect_module.GameEntry(
        identifier=f"{key}-sha1",
        name=name,
        delta_type=system.delta_type,
        system=system,
        rom_path=rom_path,
        save_path=save_path,
        extra_paths={},
    )


def mupen_rom(world, installed, name):
    """A ROM plus the mupen64plus.ini entry that names its save.

    Mupen64Plus names a save after the ROM's *contents* -- the GoodName from its
    own database, looked up by the ROM's MD5, plus that MD5's first eight hex
    digits. Measured against a real install; see `emulators.mupen64plus_stem`.
    So a test that hands it a ROM it has never heard of is testing the refusal,
    not the naming.
    """
    import hashlib

    # A real z64 header and a plausible size, because the stem is derived from
    # the ROM in *native* byte order and anything without a recognised magic,
    # or smaller than the emulator will open, is refused rather than hashed as
    # if it were a ROM.
    rom = world.root / f"{name}.z64"
    body = (name.encode() * 64).ljust(emulators.MINIMUM_ROM_SIZE, b"\x00")
    rom.write_bytes(emulators.Z64_MAGIC + body)
    digest = hashlib.md5(rom.read_bytes()).hexdigest().upper()
    (installed.install_dir / "mupen64plus.ini").write_text(
        f"[{digest}]\nGoodName={name} (U) [!]\nCRC=00000000 00000000\n",
        encoding="utf-8",
    )
    return rom, f"{name} (U) [!]-{digest[:8]}"


@pytest.fixture
def world(tmp_path):
    """A Delta folder, a ROM folder, and an emulator installed beside them."""

    class World:
        def __init__(self):
            self.root = tmp_path
            self.roms = tmp_path / "roms"
            self.roms.mkdir()
            self.state = tmp_path / "state"
            self.state.mkdir()
            self.paths = sync.Paths(
                delta_folder=tmp_path / "delta",
                retroarch_config=tmp_path / "retroarch.cfg",
                save_dir=tmp_path / "saves",
                state_dir=self.state,
            )

        def install(self, key):
            directory = self.root / key
            directory.mkdir(exist_ok=True)
            executable = directory / emulators.EMULATORS[key].executables[0]
            executable.write_bytes(b"")
            return emulators.Installed(emulators.EMULATORS[key], executable)

        def delta_save(self, data, name="save"):
            path = self.root / name
            path.write_bytes(data)
            return path

    return World()


def only(outcomes):
    """The one outcome that is not a one-time informational note."""
    real = [o for o in outcomes if o.action is not sync.Action.NOTHING or o.applied]
    return real[0] if real else outcomes[0]


# --- the happy path --------------------------------------------------------


def test_a_first_pull_lands_beside_the_rom(world):
    installed = world.install("mgba")
    entry = entry_for("gba", "Pokemon: Fire Red Version", world.delta_save(GBA_SAVE))

    outcomes = sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms
    )

    written = world.roms / "Pokemon - Fire Red Version.sav"
    assert written.is_file()
    assert written.read_bytes() == GBA_SAVE
    assert only(outcomes).action is sync.Action.PULL


def test_a_dry_run_writes_nothing(world):
    installed = world.install("mgba")
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    outcomes = sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, dry_run=True
    )

    assert not (world.roms / "Pokemon.sav").exists()
    assert "would write" in only(outcomes).detail


def test_a_save_already_there_with_no_history_is_a_conflict(world):
    """Not an overwrite. The same refusal the RetroArch path makes.

    This is the ordinary first run for someone who has been playing on the
    desktop already, and it is the one case where being unhelpful is correct:
    there is no evidence about which side is ahead, so neither is chosen.
    """
    installed = world.install("mgba")
    existing = world.roms / "Pokemon.sav"
    existing.write_bytes(b"\x01" * len(GBA_SAVE))
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.CONFLICT
    assert existing.read_bytes() == b"\x01" * len(GBA_SAVE)


def test_the_previous_save_is_backed_up_before_it_is_replaced(world):
    """The second sync, after Delta moves on -- which is the normal pull."""
    installed = world.install("mgba")
    delta = world.delta_save(b"\x01" * len(GBA_SAVE))
    entry = entry_for("gba", "Pokemon", delta)
    sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)

    existing = world.roms / "Pokemon.sav"
    assert existing.read_bytes() == b"\x01" * len(GBA_SAVE)
    delta.write_bytes(GBA_SAVE)

    sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)

    assert existing.read_bytes() == GBA_SAVE
    # Into the emulator's own folder, not the flat one -- see the collision
    # tests below for why the flat folder cannot hold these.
    backups = list(
        world.paths.emulator_backup_dir("mgba").glob("Pokemon.sav.*.bak")
    )
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"\x01" * len(GBA_SAVE)


# --- N64, the row that gets easier -----------------------------------------


def test_n64_is_a_plain_copy_to_the_extension_the_size_names(world):
    """No conversion at all, unlike the RetroArch path's combined .srm."""
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    rom, stem = mupen_rom(world, installed, "Ocarina of Time")
    entry = entry_for("n64", "Ocarina of Time", world.delta_save(SRAM), rom)

    outcomes = sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, override=saves
    )

    written = saves / f"{stem}.sra"
    assert written.is_file()
    assert written.read_bytes() == SRAM
    assert only(outcomes).action is sync.Action.PULL


def test_a_4k_eeprom_save_becomes_an_eep(world):
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    rom, stem = mupen_rom(world, installed, "Super Mario 64")
    entry = entry_for(
        "n64", "Super Mario 64", world.delta_save(b"\x22" * 0x200), rom
    )

    sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, override=saves
    )

    assert (saves / f"{stem}.eep").is_file()


def test_the_save_is_named_the_way_mupen64plus_names_it(world):
    """Measured against a real install, 2026-09-11.

    Mupen64Plus does not name a save after the ROM *file*. It names it after the
    ROM's contents: the GoodName from its own database, looked up by MD5, plus
    that MD5's first eight hex digits. A real install, handed
    ``Super Mario 64.z64``, wrote ``Super Mario 64 (U) [!]-20B854B2.eep``.

    So the display name this project had been using produced a file the emulator
    silently never opens -- a sync that reports success and changes nothing the
    player can see.
    """
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    rom, stem = mupen_rom(world, installed, "Super Mario 64")
    entry = entry_for("n64", "Super Mario 64", world.delta_save(b"\x22" * 0x200), rom)

    sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, override=saves
    )

    assert stem.startswith("Super Mario 64 (U) [!]-")
    assert (saves / f"{stem}.eep").is_file()
    assert not (saves / "Super Mario 64.eep").exists()


def test_a_rom_mupen64plus_does_not_know_is_refused(world):
    """Rather than written under a name it will never look for."""
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    (installed.install_dir / "mupen64plus.ini").write_text("", encoding="utf-8")
    rom = world.root / "Homebrew.z64"
    rom.write_bytes(b"nobody knows this one" * 32)
    entry = entry_for("n64", "Homebrew", world.delta_save(b"\x22" * 0x200), rom)

    outcome = only(
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=world.roms, override=saves
        )
    )

    assert outcome.action is sync.Action.SKIPPED
    assert "never opens" in outcome.detail
    assert list(saves.iterdir()) == []


def test_an_n64_save_of_an_impossible_size_is_refused(world):
    """Rather than picking one of the three files to write it into."""
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    entry = entry_for("n64", "Corrupt", world.delta_save(b"\x00" * 999))

    outcome = only(
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=world.roms, override=saves
        )
    )

    assert outcome.action is sync.Action.SKIPPED
    assert "four N64 storage sizes" in outcome.detail


# --- what refuses to run ---------------------------------------------------


def test_a_blocked_emulator_is_never_written_to(world):
    installed = world.install("desmume")
    entry = entry_for("ds", "Pokemon Platinum", world.delta_save(b"\xff" * 524288))

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.SKIPPED
    assert "footer" in outcome.detail
    assert list(world.roms.iterdir()) == []


def test_a_gzip_save_on_disk_stops_the_write(world):
    """The Nestopia trap, caught by the bytes rather than by the table."""
    installed = world.install("mgba")
    existing = world.roms / "Pokemon.sav"
    existing.write_bytes(emulators.GZIP_MAGIC + b"\x08" * (len(GBA_SAVE) - 2))
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.SKIPPED
    assert "gzip" in outcome.detail
    assert existing.read_bytes().startswith(emulators.GZIP_MAGIC)


def test_a_differently_sized_save_on_disk_stops_the_write(world):
    installed = world.install("mgba")
    existing = world.roms / "Pokemon.sav"
    existing.write_bytes(b"\x01" * 8192)
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.SKIPPED
    assert existing.read_bytes() == b"\x01" * 8192


def test_an_emulator_that_cannot_be_located_is_skipped_not_guessed(world):
    """Snes9x does not write beside the ROM, so there is nowhere to put this."""
    installed = world.install("snes9x")
    entry = entry_for("snes", "Super Mario World", world.delta_save(b"\x00" * 2048))

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.SKIPPED
    assert "config.toml" in outcome.detail


def test_a_push_is_refused_unless_it_is_asked_for(world):
    installed = world.install("mgba")
    (world.roms / "Pokemon.sav").write_bytes(GBA_SAVE)
    entry = entry_for("gba", "Pokemon", None)

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.PUSH
    assert "--push" in outcome.detail


# --- two desktop sides -----------------------------------------------------


def test_each_emulator_keeps_its_own_agreed_state(world):
    """The reason the manifest grew a per-target slot.

    Without one, the second sync sees mGBA's copy where RetroArch's state was
    recorded, decides RetroArch changed, and reports a conflict on every run.
    """
    installed = world.install("mgba")
    delta = world.delta_save(GBA_SAVE)
    entry = entry_for("gba", "Pokemon", delta)

    state = manifest.Manifest.load(world.paths.manifest_path)
    retro_save = world.paths.save_dir / "Pokemon.srm"
    retro_save.parent.mkdir(parents=True)
    retro_save.write_bytes(GBA_SAVE)
    state.record(entry.identifier, delta, retro_save)

    sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, state=state
    )

    stored = state.get(entry.identifier)
    assert stored.desktop("retroarch") is not None
    assert stored.desktop("mgba") is not None
    assert stored.desktop("mgba").size == len(GBA_SAVE)


def test_a_second_run_finds_nothing_to_do(world):
    """The real test of the above: no phantom conflict on the next pass."""
    installed = world.install("mgba")
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.NOTHING


def test_recording_one_emulator_does_not_erase_another(world):
    state = manifest.Manifest(world.paths.manifest_path)
    delta = world.delta_save(GBA_SAVE)
    other = world.delta_save(b"\x01" * 64, name="other")

    state.record("game", delta, other, "mgba")
    state.record("game", delta, other, "vbam")

    stored = state.get("game")
    assert set(stored.targets) == {"mgba", "vbam"}


def test_the_per_target_state_survives_a_round_trip(world):
    state = manifest.Manifest(world.paths.manifest_path)
    delta = world.delta_save(GBA_SAVE)
    state.record("game", delta, delta, "mgba")
    state.save()

    reloaded = manifest.Manifest.load(world.paths.manifest_path)

    assert reloaded.get("game").desktop("mgba").sha1 == manifest.sha1_of(delta)


def test_a_manifest_with_no_standalone_targets_keeps_its_old_shape(world):
    """So an existing manifest is not rewritten just by upgrading."""
    state = manifest.Manifest(world.paths.manifest_path)
    delta = world.delta_save(GBA_SAVE)
    state.record("game", delta, delta)

    assert "targets" not in state.get("game").to_json()


# --- locating a save the emulator already wrote ----------------------------


def test_an_existing_save_decides_the_folder(world):
    """Preferred over every constructed path: it is where the emulator writes."""
    installed = world.install("mgba")
    elsewhere = world.root / "somewhere else"
    elsewhere.mkdir()
    (elsewhere / "Pokemon.sav").write_bytes(b"\x01" * len(GBA_SAVE))
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    found = sync.find_emulator_save(installed, entry, elsewhere)

    assert found == elsewhere / "Pokemon.sav"


def test_every_n64_extension_is_looked_for(world):
    """Delta may have no save to say which of the three this cartridge uses."""
    installed = world.install("mupen64plus")
    saves = installed.install_dir / "save"
    saves.mkdir()
    rom, stem = mupen_rom(world, installed, "Majora's Mask")
    (saves / f"{stem}.fla").write_bytes(b"\x00" * 0x20000)
    entry = entry_for("n64", "Majora's Mask", None, rom)

    found = sync.find_emulator_save(installed, entry, world.roms)

    assert found == saves / f"{stem}.fla"


# --- the launcher's pass ---------------------------------------------------


def test_the_launcher_syncs_nothing_when_no_emulator_is_enabled(world, monkeypatch):
    """Being installed is not the same as being enabled."""
    from delta_retroarch_synchronizer import config as config_module
    from delta_retroarch_synchronizer import launcher

    installed = world.install("mgba")
    monkeypatch.setattr(
        launcher.emulators_module, "find_installed", lambda extra=(): [installed]
    )
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))

    outcomes = launcher.sync_emulators(
        world.paths, [entry], config_module.Config(), None
    )

    assert outcomes == []
    assert not (world.roms / "Pokemon.sav").exists()


def test_the_launcher_syncs_an_enabled_emulator(world, monkeypatch):
    """The gap this closes: enabled in config.toml but inert in the window."""
    from delta_retroarch_synchronizer import config as config_module
    from delta_retroarch_synchronizer import launcher

    installed = world.install("mgba")
    monkeypatch.setattr(
        launcher.emulators_module, "find_installed", lambda extra=(): [installed]
    )
    entry = entry_for("gba", "Pokemon", world.delta_save(GBA_SAVE))
    config = config_module.Config(
        emulators_enabled=("mgba",), retroarch_rom_dir=world.roms
    )

    outcomes = launcher.sync_emulators(world.paths, [entry], config, None)

    assert (world.roms / "Pokemon.sav").read_bytes() == GBA_SAVE
    assert any(o.action is sync.Action.PULL for o in outcomes)


def test_the_launcher_skips_systems_the_emulator_does_not_run(world, monkeypatch):
    from delta_retroarch_synchronizer import config as config_module
    from delta_retroarch_synchronizer import launcher

    installed = world.install("mgba")
    monkeypatch.setattr(
        launcher.emulators_module, "find_installed", lambda extra=(): [installed]
    )
    entry = entry_for("n64", "Ocarina of Time", world.delta_save(SRAM))
    config = config_module.Config(
        emulators_enabled=("mgba",), retroarch_rom_dir=world.roms
    )

    assert launcher.sync_emulators(world.paths, [entry], config, None) == []


# --- the bug that per-target state introduced ------------------------------


def test_a_newly_enabled_emulator_never_pushes_its_own_save_to_delta(world):
    """The data-loss path adding a second target opened, and its fix.

    Someone syncs to RetroArch for a while, so Delta has agreed state. Then they
    enable mGBA, which already has its own unrelated save for that game. Delta
    has not changed since the last RetroArch sync, and mGBA has no history --
    so "Delta unchanged, target changed" read as PUSH, and with pushing on that
    sent mGBA's save to the phone over the real one, reporting no conflict.

    Two saves and no agreed history has always meant conflict here. Keying the
    first-sync branch on *this target's* history rather than the entry's is what
    makes it mean that for every target rather than only the first one.
    """
    installed = world.install("mgba")
    delta = world.delta_save(b"D" * 1024)
    entry = entry_for("gba", "Pokemon", delta)

    # Already agreed with RetroArch, which is what fills in the delta half.
    state = manifest.Manifest(world.paths.manifest_path)
    retro = world.paths.save_dir / "Pokemon.srm"
    retro.parent.mkdir(parents=True)
    retro.write_bytes(b"D" * 1024)
    state.record(entry.identifier, delta, retro)

    # mGBA has its own save for the same game, of the same size and different
    # content -- so the shape check clears it and the decision is what decides.
    mgba_save = world.roms / "Pokemon.sav"
    mgba_save.write_bytes(b"M" * 1024)

    outcome = only(
        sync.sync_emulator(
            world.paths,
            entry,
            installed,
            rom_dir=world.roms,
            allow_push=True,
            state=state,
        )
    )

    assert outcome.action is sync.Action.CONFLICT
    assert delta.read_bytes() == b"D" * 1024
    assert mgba_save.read_bytes() == b"M" * 1024


def test_identical_saves_on_a_new_target_are_simply_agreed(world):
    """The other half: no history but nothing to lose is not a conflict."""
    installed = world.install("mgba")
    delta = world.delta_save(b"D" * 1024)
    entry = entry_for("gba", "Pokemon", delta)
    (world.roms / "Pokemon.sav").write_bytes(b"D" * 1024)

    outcome = only(
        sync.sync_emulator(world.paths, entry, installed, rom_dir=world.roms)
    )

    assert outcome.action is sync.Action.NOTHING


# --- the shared-Delta-history bug ------------------------------------------


def test_a_second_target_still_gets_an_update_retroarch_already_took(world):
    """The bug a shared Delta half caused, and the reason it is now per target.

    RetroArch is reconciled first and records that Delta is agreed. If that
    record is global, every target behind it then sees a Delta that "has not
    changed" and is left holding the old save -- reported, worst of all, as
    "unchanged on both sides" rather than as anything that looks wrong.

    So whichever target happened to run first quietly decided that none of the
    others needed the update.
    """
    installed = world.install("mgba")
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)

    # Both sides agreed at v1.
    state = manifest.Manifest(world.paths.manifest_path)
    retro = world.paths.save_dir / "Pokemon.srm"
    retro.parent.mkdir(parents=True)
    retro.write_bytes(b"v1" * 512)
    state.record(entry.identifier, delta, retro)
    sync.sync_emulator(
        world.paths, entry, installed, rom_dir=world.roms, state=state
    )
    mgba_save = world.roms / "Pokemon.sav"
    assert mgba_save.read_bytes() == b"v1" * 512

    # Delta moves on, and RetroArch takes the update first.
    delta.write_bytes(b"v2" * 512)
    retro.write_bytes(b"v2" * 512)
    state.record(entry.identifier, delta, retro)

    outcome = only(
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=world.roms, state=state
        )
    )

    assert outcome.action is sync.Action.PULL
    assert mgba_save.read_bytes() == b"v2" * 512


def test_two_emulators_both_get_the_update(world):
    """The same thing with no RetroArch involved: neither starves the other."""
    mgba = world.install("mgba")
    vbam = world.install("vbam")
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)
    state = manifest.Manifest(world.paths.manifest_path)

    mgba_dir = world.root / "mgba-saves"
    vbam_dir = world.root / "vbam-saves"
    for directory in (mgba_dir, vbam_dir):
        directory.mkdir()

    for installed, directory in ((mgba, mgba_dir), (vbam, vbam_dir)):
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=directory, state=state
        )

    delta.write_bytes(b"v2" * 512)
    for installed, directory in ((mgba, mgba_dir), (vbam, vbam_dir)):
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=directory, state=state
        )

    assert (mgba_dir / "Pokemon.sav").read_bytes() == b"v2" * 512
    assert (vbam_dir / "Pokemon.sav").read_bytes() == b"v2" * 512


def test_recording_one_target_leaves_another_s_delta_half_alone(world):
    """The manifest-level statement of the same fact."""
    state = manifest.Manifest(world.paths.manifest_path)
    old = world.delta_save(b"v1" * 512, name="old")
    new = world.delta_save(b"v2" * 512, name="new")
    desktop = world.delta_save(b"x" * 64, name="desktop")

    state.record("g", old, desktop, "mgba")
    state.record("g", new, desktop)  # RetroArch moves on

    assert state.get("g").agreement("mgba").delta.sha1 == manifest.sha1_of(old)
    assert state.get("g").agreement("retroarch").delta.sha1 == manifest.sha1_of(new)


def test_the_pair_survives_a_round_trip(world):
    state = manifest.Manifest(world.paths.manifest_path)
    delta = world.delta_save(b"v1" * 512)
    desktop = world.delta_save(b"x" * 64, name="desktop")
    state.record("g", delta, desktop, "mgba")
    state.save()

    agreed = manifest.Manifest.load(world.paths.manifest_path).get("g").agreement("mgba")

    assert agreed.delta.sha1 == manifest.sha1_of(delta)
    assert agreed.desktop.sha1 == manifest.sha1_of(desktop)


def test_a_target_written_before_the_pair_existed_is_read_safely(world):
    """The first shape of per-target state stored only the desktop half.

    It has to read as "we never recorded what Delta looked like", so the next
    sync re-evaluates rather than trusting a pairing that was never written.
    """
    raw = {"delta": None, "retroarch": None, "targets": {"mgba": {"sha1": "abc", "size": 4}}}

    agreed = manifest.Entry.from_json(raw).agreement("mgba")

    assert agreed.delta is None
    assert agreed.desktop.sha1 == "abc"


# --- a push mid-run leaves earlier targets behind ---------------------------


def applied_push(target="mGBA"):
    return sync.Outcome("Game", sync.Action.PUSH, "", target=target, applied=True)


def test_no_second_pass_when_nothing_moved_delta():
    calls = []
    assert sync.settle(lambda: calls.append(1) or [], [
        sync.Outcome("Game", sync.Action.PULL, "", applied=True),
        sync.Outcome("Game", sync.Action.NOTHING, ""),
    ]) == []
    assert calls == []


def test_a_push_that_was_only_planned_does_not_trigger_a_pass():
    """"not pushed (pass --push to enable)" moved nothing, so nothing is stale."""
    calls = []
    planned = [sync.Outcome("Game", sync.Action.PUSH, "not pushed")]

    assert sync.settle(lambda: calls.append(1) or [], planned) == []
    assert calls == []


def test_a_push_that_landed_triggers_one_more_pass():
    calls = []

    def run():
        calls.append(1)
        return [sync.Outcome("Game", sync.Action.PULL, "", applied=True)]

    result = sync.settle(run, [applied_push()])

    assert len(calls) == 1
    assert [o.action for o in result] == [sync.Action.PULL]


def test_the_extra_pass_never_loops():
    """Even if the second pass reports another push, there is no third.

    A reconcile that can iterate is one that can iterate forever, and this runs
    against real saves. The cap is structural rather than an argument about why
    a third pass cannot be needed.
    """
    calls = []

    def run():
        calls.append(1)
        return [applied_push()]

    sync.settle(run, [applied_push()])

    assert len(calls) == 1


def test_a_dry_run_never_makes_a_second_pass():
    calls = []

    sync.settle(lambda: calls.append(1) or [], [applied_push()], dry_run=True)

    assert calls == []


def test_retroarch_is_brought_up_to_date_by_the_settle_pass(world):
    """The whole scenario, end to end.

    RetroArch is reconciled first and has nothing to do. mGBA then pushes a
    newer save to Delta. Without a second pass the run reports success while
    RetroArch still holds the old save -- and if it is played before the next
    sync, that push has silently arranged a conflict.
    """
    installed = world.install("mgba")
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)

    retro = world.paths.save_dir / "Pokemon.srm"
    retro.parent.mkdir(parents=True)
    retro.write_bytes(b"v1" * 512)
    mgba_save = world.roms / "Pokemon.sav"
    mgba_save.write_bytes(b"v1" * 512)

    state = manifest.Manifest(world.paths.manifest_path)
    state.record(entry.identifier, delta, retro)
    state.record(entry.identifier, delta, mgba_save, "mgba")
    state.save()

    # Played in mGBA. Its push is simulated by writing Delta directly, which is
    # what push_with_revision ends up doing -- the Dropbox half needs a network.
    mgba_save.write_bytes(b"v2" * 512)

    def one_pass():
        outcomes = list(
            sync.run_sync(
                world.paths, [entry], "mGBA", False, allow_push=False
            ).outcomes
        )
        fresh = manifest.Manifest.load(world.paths.manifest_path)
        result = sync.sync_emulator(
            world.paths, entry, installed, rom_dir=world.roms, state=fresh
        )
        # Stand in for the push: Delta takes mGBA's save and the pair is agreed.
        if any(o.action is sync.Action.PUSH for o in result):
            delta.write_bytes(mgba_save.read_bytes())
            fresh.record(entry.identifier, delta, mgba_save, "mgba")
            result = [applied_push()]
        fresh.save()
        return outcomes + result

    first = one_pass()
    assert delta.read_bytes() == b"v2" * 512
    assert retro.read_bytes() == b"v1" * 512, "stale, as the bug describes"

    sync.settle(one_pass, first)

    assert retro.read_bytes() == b"v2" * 512


# --- backups have to say which emulator they came from ----------------------


def test_two_emulators_backups_do_not_collide(world):
    """mGBA and VBA-M both write Pokemon.sav, so the name cannot tell them apart.

    In one flat folder that loses three things at once: which emulator a restore
    point came from, where it would be put back, and its own rolling history --
    `backup` keeps the last ten *by name*, so the two would prune each other.
    """
    from delta_retroarch_synchronizer import restore

    for key, content in (("mgba", b"from mGBA"), ("vbam", b"from VBA-M")):
        installed = world.install(key)
        directory = world.root / f"{key}-roms"
        directory.mkdir()
        save = directory / "Pokemon.sav"
        save.write_bytes(b"old" + content)
        delta = world.delta_save(content, name=f"delta-{key}")
        entry = entry_for("gba", "Pokemon", delta)
        state = manifest.Manifest(world.paths.manifest_path)
        state.record(entry.identifier, delta, save, key)
        delta.write_bytes(b"new" + content)
        sync.sync_emulator(
            world.paths, entry, installed, rom_dir=directory, state=state
        )

    points = {b.side: b for b in restore.scan(world.paths.backup_dir)}

    assert set(points) == {"mGBA", "VisualBoyAdvance-M"}
    assert points["mGBA"].path.read_bytes() == b"oldfrom mGBA"
    assert points["VisualBoyAdvance-M"].path.read_bytes() == b"oldfrom VBA-M"


def test_an_emulator_backup_is_not_labelled_retroarch(world):
    """It would otherwise be offered as a RetroArch restore point and fail."""
    from delta_retroarch_synchronizer import restore

    folder = world.paths.emulator_backup_dir("mgba")
    folder.mkdir(parents=True)
    (folder / "Pokemon.sav.20260910T120000000000.bak").write_bytes(b"x")

    backup = restore.scan(world.paths.backup_dir)[0]

    assert backup.side == "mGBA"
    assert backup.emulator == "mgba"
    assert not backup.is_delta


def test_retroarch_backups_still_read_exactly_as_before(world):
    """Every backup taken before this change lives in the flat folder."""
    from delta_retroarch_synchronizer import restore

    world.paths.backup_dir.mkdir(parents=True)
    (world.paths.backup_dir / "Pokemon.srm.20260910T120000000000.bak").write_bytes(b"x")

    backup = restore.scan(world.paths.backup_dir)[0]

    assert backup.side == restore.RETROARCH
    assert backup.emulator == ""


def test_a_delta_backup_is_still_recognised_as_delta(world):
    from delta_retroarch_synchronizer import restore

    world.paths.backup_dir.mkdir(parents=True)
    (
        world.paths.backup_dir / "GameSave-abc123-gameSave.20260910T120000000000.bak"
    ).write_bytes(b"x")

    backup = restore.scan(world.paths.backup_dir)[0]

    assert backup.is_delta
    assert backup.identifier == "abc123"


def test_an_emulator_backup_is_restored_to_that_emulator_s_folder(world):
    """The other half: a backup that cannot be put back is not a backup."""
    from delta_retroarch_synchronizer import config as config_module
    from delta_retroarch_synchronizer import restore

    installed = world.install("mgba")
    saves = world.root / "mgba-saves"
    saves.mkdir()
    live = saves / "Pokemon.sav"
    live.write_bytes(b"current")

    folder = world.paths.emulator_backup_dir("mgba")
    folder.mkdir(parents=True)
    (folder / "Pokemon.sav.20260910T120000000000.bak").write_bytes(b"older")

    point = restore.restore_points(
        restore.scan(world.paths.backup_dir), {}
    )[0]
    config = config_module.Config(
        emulator_paths={"mgba": installed.install_dir},
        emulator_save_dirs={"mgba": saves},
    )

    note = restore.restore_retroarch(
        point, world.paths.save_dir, world.paths.backup_dir, config
    )

    assert live.read_bytes() == b"older"
    assert str(saves) in note
    # Undoable: what was there is now the newest point in the same folder.
    assert any(p.read_bytes() == b"current" for p in folder.glob("*.bak"))


# --- two emulators resolving to one file ------------------------------------


def test_two_emulators_sharing_one_file_is_refused_not_reconciled(world):
    """mGBA and VBA-M both run GBA, both write <game>.sav beside the ROM.

    Enable both with one ROM folder and they are two agreements over one file.
    Reconciling the second reports a conflict the user cannot clear -- there is
    no second version to choose between -- and it comes back every run. So the
    second says what is actually wrong instead.
    """
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)
    state = manifest.Manifest(world.paths.manifest_path)
    claimed: dict[Path, str] = {}

    first = only(
        sync.sync_emulator(
            world.paths, entry, world.install("mgba"), rom_dir=world.roms,
            state=state, claimed=claimed,
        )
    )
    second = only(
        sync.sync_emulator(
            world.paths, entry, world.install("vbam"), rom_dir=world.roms,
            state=state, claimed=claimed,
        )
    )

    assert first.action is sync.Action.PULL
    assert second.action is sync.Action.SKIPPED
    assert "same file" in second.detail
    assert "save_dir" in second.detail


def test_giving_one_of_them_its_own_folder_resolves_it(world):
    """Which is what that message tells the user to do."""
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)
    state = manifest.Manifest(world.paths.manifest_path)
    claimed: dict[Path, str] = {}
    elsewhere = world.root / "vbam-saves"
    elsewhere.mkdir()

    sync.sync_emulator(
        world.paths, entry, world.install("mgba"), rom_dir=world.roms,
        state=state, claimed=claimed,
    )
    second = only(
        sync.sync_emulator(
            world.paths, entry, world.install("vbam"), rom_dir=world.roms,
            override=elsewhere, state=state, claimed=claimed,
        )
    )

    assert second.action is sync.Action.PULL
    assert (world.roms / "Pokemon.sav").read_bytes() == b"v1" * 512
    assert (elsewhere / "Pokemon.sav").read_bytes() == b"v1" * 512


def test_without_a_claimed_dict_nothing_changes(world):
    """The parameter is opt-in, so a single-emulator caller is unaffected."""
    delta = world.delta_save(b"v1" * 512)
    entry = entry_for("gba", "Pokemon", delta)

    outcome = only(
        sync.sync_emulator(
            world.paths, entry, world.install("mgba"), rom_dir=world.roms
        )
    )

    assert outcome.action is sync.Action.PULL


# --- a backup has to be restorable wherever the sync put it ------------------


def _mgba_at(world):
    installed = world.install("mgba")
    return installed, config_module.Config(
        emulator_paths={"mgba": installed.install_dir}
    )


def _backup(world, name="Pokemon.sav"):
    from delta_retroarch_synchronizer import restore

    folder = world.paths.emulator_backup_dir("mgba")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.20260910T120000000000.bak").write_bytes(b"older")
    return restore.restore_points(restore.scan(world.paths.backup_dir), {})[0]


def test_a_save_in_the_folder_the_emulators_own_config_names_is_found(world):
    """The first automatic answer the hand-written candidate list missed."""
    from delta_retroarch_synchronizer import restore

    installed, config = _mgba_at(world)
    named = world.root / "from-config"
    named.mkdir()
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {named}\n", encoding="utf-8"
    )
    live = named / "Pokemon.sav"
    live.write_bytes(b"current")

    note = restore.restore_retroarch(
        _backup(world), world.paths.save_dir, world.paths.backup_dir, config
    )

    assert live.read_bytes() == b"older"
    assert str(named) in note


def test_a_save_beside_the_derived_rom_folder_is_found(world):
    """The second, and the commonest: beside-the-ROM with no rom dir configured.

    ``retroarch_rom_dir`` unset does not mean there is no ROM folder -- the sync
    derives one beside retroarch.cfg. Reading only the configured value made
    every default-placed standalone save unrestorable.
    """
    from delta_retroarch_synchronizer import restore

    _, config = _mgba_at(world)
    assert config.retroarch_rom_dir is None
    derived = config_module.rom_dir(config, world.paths.retroarch_config)
    # Which is the fixture's own ROM folder -- retroarch.cfg sits beside it,
    # exactly as it does in a real install.
    assert derived == world.roms
    derived.mkdir(parents=True, exist_ok=True)
    live = derived / "Pokemon.sav"
    live.write_bytes(b"current")

    note = restore.restore_retroarch(
        _backup(world),
        world.paths.save_dir,
        world.paths.backup_dir,
        config,
        derived,
    )

    assert live.read_bytes() == b"older"
    assert str(derived) in note


def test_the_rom_dir_derivation_has_one_definition(world):
    """Both drivers must agree, or the sync writes where restore does not look."""
    config = config_module.Config()

    assert config_module.rom_dir(config, Path("C:/RetroArch/retroarch.cfg")) == Path(
        "C:/RetroArch/roms"
    )
    assert config_module.rom_dir(config, None) is None
    assert config_module.rom_dir(
        config_module.Config(retroarch_rom_dir=Path("D:/Games")), Path("C:/x.cfg")
    ) == Path("D:/Games")


def test_an_older_save_is_still_found_after_the_config_moves(world):
    """The candidates after the resolved one still matter.

    Someone whose emulator now points somewhere new is exactly who needs a
    backup from where it used to write.
    """
    from delta_retroarch_synchronizer import restore

    installed, config = _mgba_at(world)
    moved = world.root / "new-location"
    moved.mkdir()
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {moved}\n", encoding="utf-8"
    )
    # The save still sits in the install folder, where it was written before.
    live = installed.install_dir / "Pokemon.sav"
    live.write_bytes(b"current")

    restore.restore_retroarch(
        _backup(world), world.paths.save_dir, world.paths.backup_dir, config
    )

    assert live.read_bytes() == b"older"


def test_the_emulators_report_names_the_folder_the_sync_will_use(world, monkeypatch, capsys):
    """It resolved with the *configured* ROM folder, which is normally unset.

    So for every emulator that writes beside the ROM -- most of them -- the
    report said "could not be determined, set it in config.toml" about a setup
    that already worked, and named no folder at all.
    """
    from delta_retroarch_synchronizer import __main__ as main
    from delta_retroarch_synchronizer import discovery

    installed = world.install("mgba")
    config = config_module.Config(
        retroarch_config=world.paths.retroarch_config,
        emulators_enabled=("mgba",),
        emulator_paths={"mgba": installed.install_dir},
    )
    monkeypatch.setattr(main.config_module, "load", lambda *a, **k: config)
    monkeypatch.setattr(main, "installed_emulators", lambda c: [installed])
    monkeypatch.setattr(
        discovery, "find_retroarch_config", lambda: discovery.Discovery("cfg", None)
    )

    main.run_emulators_command()

    out = capsys.readouterr().out
    assert str(world.roms) in out
    assert "could not be determined" not in out


# --- the search has to cover where the sync would write --------------------


def test_a_save_in_the_configured_folder_is_pushed_not_missed(world):
    """The search assembled its folders by hand and left out the two that only
    `resolve_save_dir` knows: the `save_dir` set in config.toml, and the folder
    the emulator's own config names.

    With nothing in Delta to compare against, a save in either read as "neither
    side has a save" -- so the desktop's progress was never pushed to the phone,
    and the message said there was nothing there. That is the one direction
    where being wrong loses something that exists nowhere else.
    """
    installed = world.install("mgba")
    chosen = world.root / "my-gba-saves"
    chosen.mkdir()
    (chosen / "Pokemon.sav").write_bytes(b"DESKTOP PROGRESS" * 8192)
    entry = entry_for("gba", "Pokemon", None)  # Delta has nothing

    found = sync.find_emulator_save(installed, entry, world.roms, chosen)
    target, why = sync.emulator_target(installed, entry, world.roms, override=chosen)

    assert found == chosen / "Pokemon.sav"
    assert target == chosen / "Pokemon.sav"
    assert "neither side" not in why


def test_a_save_in_the_folder_the_emulators_config_names_is_found(world):
    """The same gap, reached without any config.toml override at all."""
    installed = world.install("mgba")
    named = world.root / "from-config"
    named.mkdir()
    (named / "Zelda.sav").write_bytes(b"x" * 8192)
    (installed.install_dir / "config.ini").write_text(
        f"savegamePath = {named}\n", encoding="utf-8"
    )
    entry = entry_for("gba", "Zelda", None)

    assert sync.find_emulator_save(installed, entry, world.roms) == named / "Zelda.sav"


def test_the_resolved_folder_is_searched_first(world):
    """Ahead of the hand-built candidates, which are still tried after it."""
    installed = world.install("mgba")
    chosen = world.root / "elsewhere"
    chosen.mkdir()

    dirs = sync.emulator_search_dirs(installed, world.roms, chosen)

    assert dirs[0] == chosen
    assert world.roms in dirs
    assert installed.install_dir in dirs
