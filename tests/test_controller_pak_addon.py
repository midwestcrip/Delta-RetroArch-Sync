"""The Controller Pak add-on: its commands, its refusals, and its wire format.

Everything here runs without a phone, which is the design rather than a
concession. The add-on reaches its files through a small interface with two
implementations -- a folder on disk and Delta's container over USB -- so every
command, every message and every failure path is the same code either way. What
a cable is genuinely required for is one question: whether ``house_arrest``
opens Delta's container on a real device, and what the files in it are called.
``probe`` and ``list`` exist to answer that and are asserted here to *report*
rather than assume.

Two things are worth the care they get:

- **The result line.** The main program treats a run that printed no
  ``{"type": "result"}`` as a crash, so every path -- including the unexpected
  ones -- has to reach one. ``AlwaysAnswersTests`` is that.
- **The argument order.** ``prog list --folder X`` is what a person types, and
  plain argparse rejects it. Worse, the obvious fix makes a subcommand's unset
  ``--folder`` silently overwrite the one given before it, which does not error
  -- it talks to the phone instead of the folder.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import delta_retroarch_controller_pak as addon  # noqa: E402
from delta_retroarch_controller_pak import PROTOCOL, __main__ as cli  # noqa: E402
from delta_retroarch_controller_pak.sources import (  # noqa: E402
    DeviceSource,
    FolderSource,
    SourceError,
)
from delta_retroarch_synchronizer import addons, n64, paks  # noqa: E402


def a_used_pak(marker: int) -> bytes:
    data = bytearray(n64.format_controller_pak())
    data[256 + 10] = 0x00
    data[256 + 11] = 0x04 + marker
    return bytes(data)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.phone = self.root / "Saves"
        self.phone.mkdir()

    def a_bundle(self, name: str = "Paper Mario.mpk") -> Path:
        path = self.phone / name
        path.write_bytes(b"".join(a_used_pak(i + 1) for i in range(4)))
        return path

    def run_cli(self, *argv: str) -> tuple[int, dict, list[dict]]:
        """Run a command in process and read the JSON Lines it printed."""
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--json", *argv])

        events = [
            json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()
        ]
        result = next(
            (event for event in reversed(events) if event.get("type") == "result"), {}
        )
        return code, result, events


class ListingTests(Base):
    def test_it_reports_what_is_there_rather_than_what_was_expected(self) -> None:
        """The measurement. Nothing in this program knows what Delta's build
        names its pak files, and this is how that gets settled."""
        self.a_bundle("Paper Mario.mpk")
        (self.phone / "Paper Mario.eep").write_bytes(b"\x00" * 2048)

        code, result, _ = self.run_cli("list", "--folder", str(self.phone))
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        names = {item["name"]: item["size"] for item in result["files"]}
        self.assertEqual(names["Paper Mario.mpk"], paks.BUNDLE_SIZE)
        self.assertEqual(names["Paper Mario.eep"], 2048)

    def test_the_reply_carries_the_protocol_number(self) -> None:
        """The main program refuses a mismatch rather than misreading it."""
        _, result, _ = self.run_cli("list", "--folder", str(self.phone))
        self.assertEqual(result["protocol"], PROTOCOL)
        self.assertEqual(PROTOCOL, addons.PROTOCOL)

    def test_a_folder_that_is_not_there_is_a_sentence_not_a_traceback(self) -> None:
        code, result, _ = self.run_cli("list", "--folder", str(self.root / "nope"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("is not a folder", result["error"])

    def test_progress_is_reported_before_the_result(self) -> None:
        events = self.run_cli("list", "--folder", str(self.phone))[2]
        self.assertEqual(events[0]["type"], "progress")
        self.assertEqual(events[-1]["type"], "result")


class ArgumentOrderTests(Base):
    """``prog list --folder X`` and ``prog --folder X list`` must both work.

    The second is what argparse allows; the first is what people type. Getting
    this wrong the obvious way does not error -- the subcommand's unset
    ``--folder`` overwrites the one given before it, and the program quietly
    goes looking for a phone.
    """

    def test_the_option_works_after_the_subcommand(self) -> None:
        self.a_bundle()
        _, result, _ = self.run_cli("list", "--folder", str(self.phone))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["count"], 1)

    def test_the_option_works_before_the_subcommand(self) -> None:
        self.a_bundle()
        _, result, _ = self.run_cli("--folder", str(self.phone), "list")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["count"], 1)

    def test_a_subcommand_does_not_wipe_an_option_given_before_it(self) -> None:
        """The silent one. Without SUPPRESS this looks for a phone instead."""
        self.a_bundle()
        _, result, _ = self.run_cli("--folder", str(self.phone), "list")
        self.assertIn("folder", result["source"])
        self.assertNotIn("device", result["source"])


class PullTests(Base):
    def test_pak_files_are_copied_and_nothing_else_is(self) -> None:
        self.a_bundle()
        (self.phone / "Paper Mario.eep").write_bytes(b"\x00" * 2048)
        (self.phone / "notes.txt").write_text("hello")
        into = self.root / "pulled"

        code, result, _ = self.run_cli(
            "pull", "--folder", str(self.phone), "--into", str(into)
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["files"], ["Paper Mario.mpk"])
        self.assertEqual([p.name for p in into.iterdir()], ["Paper Mario.mpk"])

    def test_a_folder_with_no_paks_says_why_that_is_normal(self) -> None:
        """Delta writes these only once an N64 game has been played, which is
        the likeliest reason for an empty folder and not obvious."""
        code, result, _ = self.run_cli(
            "pull", "--folder", str(self.phone), "--into", str(self.root / "x")
        )
        self.assertEqual(code, 1)
        self.assertIn("once an N64 game has been played", result["error"])

    def test_separate_per_controller_files_are_taken_too(self) -> None:
        """The other shape. Which one Delta writes is unmeasured, so both are
        handled rather than one being assumed."""
        for index in range(1, 5):
            (self.phone / f"Paper Mario.mpk{index}").write_bytes(a_used_pak(index))
        into = self.root / "pulled"
        _, result, _ = self.run_cli(
            "pull", "--folder", str(self.phone), "--into", str(into)
        )
        self.assertEqual(result["count"], 4)


class PushTests(Base):
    def test_files_go_back_the_other_way(self) -> None:
        source = self.root / "outgoing"
        source.mkdir()
        (source / "Paper Mario.mpk1").write_bytes(a_used_pak(1))

        code, result, _ = self.run_cli(
            "push", "--folder", str(self.phone), "--from", str(source)
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["files"], ["Paper Mario.mpk1"])
        self.assertEqual(
            (self.phone / "Paper Mario.mpk1").read_bytes(), a_used_pak(1)
        )

    def test_pushing_from_a_folder_that_is_not_there_is_refused(self) -> None:
        code, result, _ = self.run_cli(
            "push", "--folder", str(self.phone), "--from", str(self.root / "nope")
        )
        self.assertEqual(code, 1)
        self.assertIn("is not a folder", result["error"])


class ProbeTests(Base):
    """The probe answers "what is stopping me", so it reports every check
    rather than stopping at the first failure -- and it does not call itself a
    failure just because there is no phone."""

    def test_a_machine_without_the_library_still_gets_a_useful_answer(
        self,
    ) -> None:
        code, result, _ = self.run_cli("probe")
        self.assertEqual(code, 0, "a probe with nothing plugged in is not a failure")
        self.assertTrue(result["ok"])
        self.assertIn("ready", result)
        self.assertTrue(result["checks"], "a probe that checks nothing is useless")

    def test_every_check_says_what_it_is_and_whether_it_passed(self) -> None:
        _, result, _ = self.run_cli("probe")
        for check in result["checks"]:
            self.assertIn("check", check)
            self.assertIn("ok", check)
            self.assertIn("detail", check)


class AlwaysAnswersTests(Base):
    """A run that prints no result line is a crash as far as the main program
    is concerned, so every path has to reach one."""

    def test_an_unexpected_failure_still_produces_a_result(self) -> None:
        def explode(self):  # noqa: ANN001
            raise RuntimeError("the USB stack fell over")

        original = FolderSource.listing
        FolderSource.listing = explode  # type: ignore[assignment]
        try:
            code, result, _ = self.run_cli("list", "--folder", str(self.phone))
        finally:
            FolderSource.listing = original  # type: ignore[assignment]

        self.assertEqual(code, 1)
        self.assertEqual(result.get("type"), "result")
        self.assertIn("USB stack fell over", result["error"])

    def test_no_command_in_json_mode_is_an_answer_not_a_window(self) -> None:
        """Guards a genuinely bad outcome: the main program runs this without a
        console, and opening a window there would hang the call until somebody
        found and closed it."""
        code, result, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("no command", result["error"])


class AsyncSurfaceTests(unittest.TestCase):
    """pymobiledevice3 11.x is async throughout, and does not look it.

    Its AFC methods carry a ``@path_to_str()`` decorator, so
    ``inspect.iscoroutinefunction`` reports them as ordinary functions. The
    first version of this code called them as such and failed with
    "'coroutine' object is not iterable" -- found by installing the library and
    probing with no phone attached. These pin the awaits in place without one.
    """

    def test_listing_devices_awaits_the_coroutine(self) -> None:
        class FakeUsbmux:
            @staticmethod
            async def list_devices():
                return ["one iPhone"]

        original = DeviceSource.library
        DeviceSource.library = staticmethod(lambda: FakeUsbmux)  # type: ignore[assignment]
        try:
            self.assertEqual(DeviceSource.devices(), ["one iPhone"])
        finally:
            DeviceSource.library = original  # type: ignore[assignment]

    def test_a_refused_connection_is_named_as_the_missing_service(self) -> None:
        """usbmux missing and no phone plugged in are different problems with
        different fixes, and only the exception type tells them apart."""

        class FakeUsbmux:
            @staticmethod
            async def list_devices():
                raise ConnectionRefusedError(61, "nope")

        original = DeviceSource.library
        DeviceSource.library = staticmethod(lambda: FakeUsbmux)  # type: ignore[assignment]
        try:
            with self.assertRaises(SourceError) as caught:
                DeviceSource.devices()
        finally:
            DeviceSource.library = original  # type: ignore[assignment]
        self.assertIn("iTunes or the Apple Devices app", str(caught.exception))

    def test_Delta_is_matched_rather_than_hard_coded(self) -> None:
        """App Store, AltStore and sideloaded builds carry different bundle
        identifiers, and a wrong guess fails as "no such app"."""
        for bundle in (
            "com.rileytestut.Delta",
            "com.rileytestut.Delta.AltStore",
            "uk.example.DeltaBeta",
        ):
            with self.subTest(bundle=bundle):
                self.assertEqual(
                    DeviceSource.find_delta({bundle: {}, "com.other.app": {}}),
                    bundle,
                )

    def test_a_device_without_Delta_gives_none(self) -> None:
        self.assertIsNone(DeviceSource.find_delta({"com.other.app": {}}))


class OneSessionTests(Base):
    """Reading four paks must not open four connections.

    Against a device every call is a fresh usbmux handshake and container vend.
    Doing that per file is invisible on a folder and slow enough to look broken
    on a phone, so it is asserted where it can be.
    """

    def test_pull_reads_every_file_in_a_single_call(self) -> None:
        for index in range(1, 5):
            (self.phone / f"Paper Mario.mpk{index}").write_bytes(a_used_pak(index))

        calls: list[list[str]] = []
        original = FolderSource.read_many

        def counting(self, names):  # noqa: ANN001
            calls.append(list(names))
            return original(self, names)

        FolderSource.read_many = counting  # type: ignore[assignment]
        try:
            _, result, _ = self.run_cli(
                "pull", "--folder", str(self.phone), "--into", str(self.root / "out")
            )
        finally:
            FolderSource.read_many = original  # type: ignore[assignment]

        self.assertEqual(result["count"], 4)
        self.assertEqual(len(calls), 1, f"opened {len(calls)} sessions for 4 files")
        self.assertEqual(len(calls[0]), 4)

    def test_push_writes_every_file_in_a_single_call(self) -> None:
        outgoing = self.root / "outgoing"
        outgoing.mkdir()
        for index in range(1, 5):
            (outgoing / f"Paper Mario.mpk{index}").write_bytes(a_used_pak(index))

        calls: list[dict] = []
        original = FolderSource.write_many

        def counting(self, files):  # noqa: ANN001
            calls.append(dict(files))
            return original(self, files)

        FolderSource.write_many = counting  # type: ignore[assignment]
        try:
            _, result, _ = self.run_cli(
                "push", "--folder", str(self.phone), "--from", str(outgoing)
            )
        finally:
            FolderSource.write_many = original  # type: ignore[assignment]

        self.assertEqual(result["count"], 4)
        self.assertEqual(len(calls), 1, f"opened {len(calls)} sessions for 4 files")


class RealSubprocessTests(Base):
    """One test that crosses the actual process boundary.

    Everything above calls ``main`` in process, which is fast and exercises the
    logic. This one runs the program the way the main program runs it and reads
    it back with the real client, because the contract is a pipe -- and the last
    bug at this boundary was invisible from either side alone.
    """

    def test_the_main_program_can_read_a_real_run(self) -> None:
        self.a_bundle()
        source = Path(__file__).resolve().parents[1] / "src"

        finished = subprocess.run(
            [
                sys.executable,
                "-m",
                "delta_retroarch_controller_pak",
                "--json",
                "list",
                "--folder",
                str(self.phone),
            ],
            capture_output=True,
            text=True,
            cwd=str(source),
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)

        lines = [
            json.loads(line)
            for line in finished.stdout.splitlines()
            if line.strip()
        ]
        result = [event for event in lines if event.get("type") == "result"][-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["size"], paks.BUNDLE_SIZE)


class TheWholeJourneyTests(Base):
    """Phone folder to RetroArch save, through both programs.

    The add-on moves files and the main program writes the save. This is the
    seam between them, and it is the whole feature in one test.
    """

    def test_paks_pulled_from_a_phone_land_in_RetroArchs_save(self) -> None:
        self.a_bundle()
        pulled = self.root / "pulled"
        self.run_cli("pull", "--folder", str(self.phone), "--into", str(pulled))

        found = paks.read_folder(pulled)
        self.assertEqual(found.slots, (0, 1, 2, 3))

        # Not "saves": the fixture's phone folder is "Saves", and Windows
        # would hand back the same directory.
        target = self.root / "RetroArch" / "Paper Mario.srm"
        target.parent.mkdir()
        plan = paks.plan_install(found, target)
        paks.install(plan, self.root / "backups")

        written = target.read_bytes()
        self.assertEqual(len(written), n64.SRM_SIZE)
        for index in range(4):
            self.assertEqual(
                n64.controller_paks(written)[index], a_used_pak(index + 1)
            )

    def test_the_journey_is_reversible(self) -> None:
        """Out of RetroArch, back to a folder, back to the phone."""
        srm = self.root / "Paper Mario.srm"
        srm.write_bytes(
            n64.with_controller_paks(
                n64.blank_srm(), {i: a_used_pak(i + 1) for i in range(4)}
            )
        )
        outgoing = self.root / "outgoing"
        paks.export(srm, outgoing)

        self.run_cli(
            "push", "--folder", str(self.phone), "--from", str(outgoing)
        )
        landed = sorted(p.name for p in self.phone.iterdir())
        self.assertEqual(
            landed, ["mempak1.mpk", "mempak2.mpk", "mempak3.mpk", "mempak4.mpk"]
        )
        self.assertEqual(
            (self.phone / "mempak2.mpk").read_bytes(), a_used_pak(2)
        )


if __name__ == "__main__":
    unittest.main()


class SavesPathTests(unittest.TestCase):
    """The path measured against a real device on 2026-09-11.

    The add-on shipped with ``Cores/Mupen64Plus/Saves``, written on the
    assumption that ``documents_only=True`` vends the app's Documents folder as
    the root -- which is what the flag's name says. It does not. The vend is the
    container, with ``Documents`` as a subfolder, so both spellings the add-on
    tried were wrong in the same way.

    The only symptom was "that folder does not exist yet. If no N64 game has
    ever been played in Delta" -- which was wrong, and sent the reader looking
    at their phone instead of at the path.
    """

    def test_the_documents_prefix_is_required(self):
        self.assertEqual(
            addon.DELTA_SAVES_PATH, "Documents/Cores/Mupen64Plus/Saves"
        )

    def test_the_measured_spelling_is_tried_first(self):
        self.assertEqual(addon.DELTA_SAVES_CANDIDATES[0], addon.DELTA_SAVES_PATH)

    def test_the_older_spellings_are_still_tried(self):
        """A different pymobiledevice3 may yet re-root the vend."""
        self.assertIn("Cores/Mupen64Plus/Saves", addon.DELTA_SAVES_CANDIDATES)
        self.assertIn("/Cores/Mupen64Plus/Saves", addon.DELTA_SAVES_CANDIDATES)
