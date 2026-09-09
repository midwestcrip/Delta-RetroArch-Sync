"""Finding save states in Delta's folder, and deciding where a recovery goes.

Split from ``test_savestate.py``, which is about the byte layout. This is about
the folder: which states are there, what each one is, and the one rule that
matters more than any of it -- **a recovered save never gets written into
Delta's synced folder.** That folder belongs to another app, and a new file in
it is a new file Dropbox pushes to every device the user owns.

The listing exists because the alternative is typing a path with a UUID in it.
It also answers, for free, the open question of what the non-DS cores write:
every state that is not melonDS shows its own magic instead of being a silent
failure.
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import savestate  # noqa: E402

DS_SHA1 = "0862ec35b24de5c7e2dcb88c9eea0873110d755c"
GBA_SHA1 = "dd5945db9b930750cb39d00c84da8571feebf417"
DS_UUID = "1B4E28BA-2FA1-11D2-883F-0016D3CCA427"
GBA_UUID = "AAAAAAAA-1111-2222-3333-444444444444"
N64_SHA1 = "3837f44cda784b466c9a2d99df70d77c322b97a0"
N64_UUID = "BBBBBBBB-1111-2222-3333-444444444444"


def melonds_state(sram: bytes) -> bytes:
    """A minimal but structurally valid melonDS state."""

    def section(magic: bytes, body: bytes) -> bytes:
        return magic + struct.pack("<I", 16 + len(body)) + b"\x00" * 8 + body

    cart = struct.pack("<IIII", 0, 0, 0, len(sram)) + sram + struct.pack("<BIB", 0, 0, 0)
    body = section(b"NDSG", b"\x00" * 64) + section(b"NDCS", cart)
    return (
        b"MELN"
        + struct.pack("<HH", 9, 0)
        + struct.pack("<I", 16 + len(body))
        + b"\x00" * 4
        + body
    )


class DeltaFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.sram = bytes(range(256)) * 2048

        self._write_game(DS_SHA1, "Pokemon: Platinum Version", "ds")
        self._write_game(GBA_SHA1, "Pokemon: Fire Red Version", "gba")
        self._write_state(DS_UUID, DS_SHA1, "Slot 1", melonds_state(self.sram))
        # A GBA state is visualboyadvance-m's own format, not melonDS's.
        self._write_state(GBA_UUID, GBA_SHA1, "Before Elite Four", b"VBA\x00" + b"\x00" * 999)

    def _write_game(self, sha1: str, name: str, key: str) -> None:
        (self.folder / f"Game-{sha1}").write_text(
            json.dumps(
                {
                    "type": "Game",
                    "identifier": sha1,
                    "record": {
                        "name": name,
                        "type": f"com.rileytestut.delta.game.{key}",
                    },
                    "files": [],
                    "relationships": {},
                }
            ),
            encoding="utf-8",
        )

    def _write_state(self, uuid: str, sha1: str, slot: str, blob: bytes) -> None:
        (self.folder / f"SaveState-{uuid}").write_text(
            json.dumps(
                {
                    "type": "SaveState",
                    "identifier": uuid,
                    "record": {"name": slot},
                    "files": [],
                    "relationships": {"game": {"type": "Game", "identifier": sha1}},
                }
            ),
            encoding="utf-8",
        )
        (self.folder / f"SaveState-{uuid}-saveState").write_bytes(blob)

    def test_both_states_are_found_with_their_games_named(self) -> None:
        found = savestate.find_states(self.folder)
        self.assertEqual(len(found), 2)
        self.assertEqual(
            [s.game_name for s in found],
            ["Pokemon: Fire Red Version", "Pokemon: Platinum Version"],
        )

    def test_each_state_carries_its_system_and_the_core_that_wrote_it(self) -> None:
        by_game = {s.game_name: s for s in savestate.find_states(self.folder)}
        ds = by_game["Pokemon: Platinum Version"]
        gba = by_game["Pokemon: Fire Red Version"]
        self.assertEqual((ds.system, ds.delta_core), ("Nintendo DS", "melonDS"))
        self.assertEqual(
            (gba.system, gba.delta_core),
            ("Game Boy Advance", "visualboyadvance-m"),
        )

    def test_both_readable_states_are_offered(self) -> None:
        by_game = {s.game_name: s for s in savestate.find_states(self.folder)}
        self.assertTrue(by_game["Pokemon: Platinum Version"].recoverable)
        self.assertTrue(by_game["Pokemon: Fire Red Version"].recoverable)

    def test_an_N64_state_is_never_offered(self) -> None:
        """Not "not yet" -- a mupen64plus state contains no save at all, and
        the core from the record is the only way to know, since it is gzip on
        the outside exactly like a readable GBA state."""
        self._write_game(N64_SHA1, "Paper Mario", "n64")
        self._write_state(
            N64_UUID, N64_SHA1, "Slot 1", b"\x1f\x8b\x08\x00" + b"\x00" * 64
        )
        found = {s.game_name: s for s in savestate.find_states(self.folder)}
        paper = found["Paper Mario"]
        self.assertFalse(paper.recoverable)
        self.assertIn("no save inside a state", paper.describe_format())

    def test_each_row_names_the_emulator_that_wrote_the_state(self) -> None:
        """This column is how the other-systems question got answered: by
        looking at it rather than by running five commands."""
        by_game = {s.game_name: s for s in savestate.find_states(self.folder)}
        self.assertIn(
            "visualboyadvance-m",
            by_game["Pokemon: Fire Red Version"].describe_format(),
        )
        self.assertIn(
            "melonDS", by_game["Pokemon: Platinum Version"].describe_format()
        )

    def test_each_row_carries_the_game_id_and_system_key_for_installing(
        self,
    ) -> None:
        """Both exist for the install path. The identifier joins the row to
        Delta's game entry -- matching on the display name instead would pick
        the wrong one for two games sharing a name -- and the key reaches the
        real ``System``, which is what says the RetroArch extension and whether
        the system needs converting."""
        by_game = {s.game_name: s for s in savestate.find_states(self.folder)}
        ds = by_game["Pokemon: Platinum Version"]
        gba = by_game["Pokemon: Fire Red Version"]
        self.assertEqual((ds.game_identifier, ds.system_key), (DS_SHA1, "ds"))
        self.assertEqual((gba.game_identifier, gba.system_key), (GBA_SHA1, "gba"))

    def test_the_slot_name_the_user_typed_is_carried_through(self) -> None:
        by_game = {s.game_name: s for s in savestate.find_states(self.folder)}
        self.assertEqual(by_game["Pokemon: Platinum Version"].slot_name, "Slot 1")

    def test_reading_the_list_does_not_read_whole_states(self) -> None:
        """A DS state is ~16 MB and there can be many. The listing reads four
        bytes each; this fails loudly if that ever becomes a full read."""
        big = self.folder / f"SaveState-{DS_UUID}-saveState"
        big.write_bytes(b"MELN" + b"\x00" * (40 * 1024 * 1024))
        opened: list[int] = []
        real_open = Path.open

        def counting_open(self_path, *args, **kwargs):  # noqa: ANN001
            handle = real_open(self_path, *args, **kwargs)
            if self_path == big:
                opened.append(1)
            return handle

        Path.open = counting_open  # type: ignore[method-assign]
        try:
            savestate.find_states(self.folder)
        finally:
            Path.open = real_open  # type: ignore[method-assign]
        self.assertEqual(sum(opened), 1)

    def test_a_folder_with_no_states_is_empty_not_an_error(self) -> None:
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(savestate.find_states(empty), [])

    def test_records_without_their_state_file_are_not_listed(self) -> None:
        (self.folder / f"SaveState-{GBA_UUID}-saveState").unlink()
        found = savestate.find_states(self.folder)
        self.assertEqual([s.game_name for s in found], ["Pokemon: Platinum Version"])


class OutputLocationTests(unittest.TestCase):
    """A recovered save must never land in Delta's folder."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.fallback = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.fallback, True)

        (self.folder / f"Game-{DS_SHA1}").write_text(
            json.dumps(
                {
                    "type": "Game",
                    "identifier": DS_SHA1,
                    "record": {
                        "name": "Pokemon: Platinum Version",
                        "type": "com.rileytestut.delta.game.ds",
                    },
                    "files": [],
                    "relationships": {},
                }
            ),
            encoding="utf-8",
        )
        (self.folder / f"SaveState-{DS_UUID}").write_text(
            json.dumps(
                {
                    "type": "SaveState",
                    "identifier": DS_UUID,
                    "record": {"name": "Slot 1"},
                    "files": [],
                    "relationships": {
                        "game": {"type": "Game", "identifier": DS_SHA1}
                    },
                }
            ),
            encoding="utf-8",
        )
        self.state = self.folder / f"SaveState-{DS_UUID}-saveState"
        self.state.write_bytes(melonds_state(bytes(512)))

    def test_a_state_in_Delta_s_folder_is_recognised_as_such(self) -> None:
        self.assertTrue(savestate.is_in_delta_folder(self.state))

    def test_the_output_never_lands_in_Delta_s_folder(self) -> None:
        """The whole point. Writing here would put a new file into another
        app's storage and Dropbox would sync it everywhere."""
        target = savestate.suggested_output(self.state, self.fallback)
        self.assertNotEqual(target.parent, self.folder)
        self.assertEqual(target.parent, self.fallback)

    def test_the_output_is_named_after_the_game_not_the_uuid(self) -> None:
        target = savestate.suggested_output(self.state, self.fallback)
        self.assertEqual(target.name, "Pokemon Platinum Version.sav")

    def test_a_state_outside_Delta_s_folder_stays_put(self) -> None:
        """Someone who copied a state to their Desktop wants the save there."""
        loose = self.fallback / "my state.svs"
        loose.write_bytes(self.state.read_bytes())
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, True)
        target = savestate.suggested_output(loose, elsewhere)
        self.assertEqual(target, self.fallback / "my state.sav")

    def test_a_game_name_with_path_characters_cannot_escape(self) -> None:
        """Game names come from Delta and end up in a filename. A name with a
        separator in it must not be able to redirect the write."""
        (self.folder / f"Game-{DS_SHA1}").write_text(
            json.dumps(
                {
                    "type": "Game",
                    "identifier": DS_SHA1,
                    "record": {
                        "name": '../../evil: "name" <here>',
                        "type": "com.rileytestut.delta.game.ds",
                    },
                    "files": [],
                    "relationships": {},
                }
            ),
            encoding="utf-8",
        )
        target = savestate.suggested_output(self.state, self.fallback)
        self.assertEqual(target.parent, self.fallback)
        for forbidden in '<>:"/|?*':
            self.assertNotIn(forbidden, target.name)
        self.assertNotIn("\\", target.name)


if __name__ == "__main__":
    unittest.main()
