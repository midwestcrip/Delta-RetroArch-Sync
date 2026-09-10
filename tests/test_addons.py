"""Finding an add-on that arrived as its own download, and talking to it.

The add-on mechanism exists so the Controller Pak feature can need
``pymobiledevice3``, Apple's usbmux service, a cable and a trust pairing without
any of that reaching a program whose dependency list is the standard library.
It is a **process** boundary rather than an import for a reason this file keeps
honest: everything an add-on can do wrong -- crash, hang, print rubbish, answer
in a protocol we do not speak -- has to come back as a sentence a button can
show, never as an exception out of the launcher.

The add-ons here are real scripts run in a real subprocess. Stubbing
``subprocess`` would test the stub; the entire point of the design is what
happens at that boundary.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import addons  # noqa: E402


def a_script(body: str) -> str:
    """A Python add-on, launched through this interpreter.

    A real add-on is a frozen exe. A ``.cmd`` shim that runs Python is the same
    thing as far as this side is concerned -- a program on disk that prints JSON
    Lines -- and it keeps the test from needing a compiler.
    """
    return body


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def install(
        self,
        body: str,
        *,
        identifier: str = "controller-pak",
        folder: str = "controller-pak",
        protocol: int = addons.PROTOCOL,
        executable: str | None = None,
        version: str = "0.1.0",
        root: Path | None = None,
    ) -> Path:
        """Write an add-on out the way a download would leave one."""
        base = (root or self.root) / addons.ADDON_DIRNAME / folder
        base.mkdir(parents=True, exist_ok=True)

        script = base / "addon_main.py"
        script.write_text(body, encoding="utf-8")
        launcher = base / "run.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )

        (base / addons.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "addon": identifier,
                    "name": "Controller Pak",
                    "version": version,
                    "protocol": protocol,
                    "executable": executable if executable is not None else "run.cmd",
                    "provides": ["controller-pak"],
                }
            ),
            encoding="utf-8",
        )
        return base


ANSWERS_WELL = a_script(
    """
import json, sys
print(json.dumps({"type": "progress", "message": "looking for a device"}))
print(json.dumps({"type": "result", "ok": True, "files": ["mempak0.mpk"]}))
"""
)

FAILS_CLEANLY = a_script(
    """
import json, sys
print(json.dumps({"type": "result", "ok": False, "error": "no device is plugged in"}))
sys.exit(1)
"""
)

SAYS_NOTHING = a_script("import sys\nsys.exit(3)\n")

PRINTS_RUBBISH = a_script(
    """
import json
print("Warning: some library felt chatty")
print("not json at all")
print(json.dumps({"type": "result", "ok": True, "files": []}))
"""
)


class DiscoveryTests(Base):
    def test_an_installed_addon_is_found(self) -> None:
        self.install(ANSWERS_WELL)
        found = addons.find_addons([self.root / addons.ADDON_DIRNAME])
        self.assertEqual([a.identifier for a in found], ["controller-pak"])
        self.assertTrue(found[0].usable)

    def test_an_addon_unzipped_loose_in_the_folder_is_found_too(self) -> None:
        """Someone who extracts the zip into the app folder rather than into
        addons/ has not done anything wrong."""
        base = self.install(ANSWERS_WELL)
        loose = self.root / "loose"
        loose.mkdir()
        for item in base.iterdir():
            shutil.copy2(item, loose / item.name)
        found = addons.find_addons([loose])
        self.assertEqual(len(found), 1)

    def test_nothing_installed_is_an_empty_list_not_an_error(self) -> None:
        self.assertEqual(addons.find_addons([self.root]), [])

    def test_a_folder_that_does_not_exist_is_skipped(self) -> None:
        self.assertEqual(addons.find_addons([self.root / "nope"]), [])

    def test_a_broken_manifest_does_not_hide_a_working_neighbour(self) -> None:
        self.install(ANSWERS_WELL)
        broken = self.root / addons.ADDON_DIRNAME / "junk"
        broken.mkdir(parents=True)
        (broken / addons.MANIFEST_NAME).write_text("{ not json", encoding="utf-8")

        found = addons.find_addons([self.root / addons.ADDON_DIRNAME])
        self.assertEqual([a.identifier for a in found], ["controller-pak"])

    def test_find_by_name_returns_none_when_it_is_not_installed(self) -> None:
        self.assertIsNone(addons.find("controller-pak", [self.root]))

    def test_the_nearest_root_wins(self) -> None:
        """An add-on beside the program beats a stale copy in the state
        folder, so upgrading by unzipping over the top works."""
        near = self.root / "near"
        far = self.root / "far"
        self.install(ANSWERS_WELL, version="2.0", root=near)
        self.install(ANSWERS_WELL, version="1.0", root=far)
        found = addons.find_addons(
            [near / addons.ADDON_DIRNAME, far / addons.ADDON_DIRNAME]
        )
        self.assertEqual([a.version for a in found], ["2.0"])


class ManifestTests(Base):
    """A manifest is a text file that says which program to run, so what it is
    not allowed to say matters."""

    def test_an_executable_with_a_path_in_it_is_refused(self) -> None:
        base = self.install(ANSWERS_WELL, executable="../../evil.exe")
        with self.assertRaises(addons.AddonError) as caught:
            addons.read_manifest(base / addons.MANIFEST_NAME)
        message = str(caught.exception)
        self.assertIn("path rather than a filename", message)
        self.assertIn("its own folder", message)

    def test_an_absolute_executable_is_refused(self) -> None:
        base = self.install(ANSWERS_WELL, executable=r"C:\Windows\System32\cmd.exe")
        with self.assertRaises(addons.AddonError):
            addons.read_manifest(base / addons.MANIFEST_NAME)

    def test_a_manifest_with_no_protocol_is_refused(self) -> None:
        base = self.install(ANSWERS_WELL)
        (base / addons.MANIFEST_NAME).write_text(
            json.dumps({"addon": "x", "executable": "run.cmd"}), encoding="utf-8"
        )
        with self.assertRaises(addons.AddonError) as caught:
            addons.read_manifest(base / addons.MANIFEST_NAME)
        self.assertIn("protocol", str(caught.exception))

    def test_a_manifest_that_is_not_an_object_is_refused(self) -> None:
        base = self.install(ANSWERS_WELL)
        (base / addons.MANIFEST_NAME).write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(addons.AddonError):
            addons.read_manifest(base / addons.MANIFEST_NAME)


class ProtocolTests(Base):
    def test_a_good_answer_comes_back_with_its_result(self) -> None:
        self.install(ANSWERS_WELL)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["list"])
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.result["files"], ["mempak0.mpk"])

    def test_progress_lines_are_kept_for_the_log(self) -> None:
        self.install(ANSWERS_WELL)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["list"])
        self.assertEqual(reply.progress(), ("looking for a device",), reply.error)

    def test_a_clean_refusal_arrives_as_its_own_sentence(self) -> None:
        self.install(FAILS_CLEANLY)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["pull"])
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, "no device is plugged in")

    def test_an_addon_that_dies_without_a_word_is_still_a_sentence(self) -> None:
        """The one that must not raise. A crashed add-on is a message."""
        self.install(SAYS_NOTHING)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["pull"])
        self.assertFalse(reply.ok)
        assert reply.error is not None
        self.assertIn("did not report a result", reply.error)

    def test_chatter_on_stdout_does_not_fail_a_good_run(self) -> None:
        """Libraries print warnings. That is not the add-on failing."""
        self.install(PRINTS_RUBBISH)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["list"])
        self.assertTrue(reply.ok, reply.error)

    def test_a_hang_is_cut_off_and_explained(self) -> None:
        """The timeout has to be a wall-clock guarantee, not a message.

        It was not, and the clock is how that showed. ``kill`` on Windows kills
        only the process named -- the add-on's own helper kept the pipes open,
        so a one-second timeout against a thirty-second sleep returned after
        the full thirty. The error text was right and the window would have
        been frozen for half a minute, which is what the timeout exists to
        prevent. Whole tree now, so this is timed rather than trusted.
        """
        self.install("import time\ntime.sleep(30)\n")
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None

        started = time.monotonic()
        reply = addons.run(addon, ["pull"], timeout=1.0)
        took = time.monotonic() - started

        self.assertFalse(reply.ok)
        assert reply.error is not None
        self.assertIn("did not answer", reply.error)
        self.assertIn("trust this computer", reply.error)
        self.assertLess(
            took, 10.0, f"the timeout did not cut it off: took {took:.1f}s"
        )

    def test_an_addon_that_reads_stdin_gets_end_of_file_not_a_hang(self) -> None:
        """stdin is closed deliberately, and the reason is not tidiness.

        Redirecting any standard handle makes Windows require all three, and a
        windowed build has no console -- so an inherited stdin is not a valid
        handle and the call fails with "[WinError 6] The handle is invalid"
        before the add-on starts. That would have shipped as "add-ons work from
        source and never in the packaged app". Closing it also means an add-on
        that waits for input gets EOF instead of running out the clock.
        """
        self.install(
            'import json, sys\n'
            'data = sys.stdin.read()\n'
            'print(json.dumps({"type": "result", "ok": True, "stdin": data}))\n'
        )
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        reply = addons.run(addon, ["list"], timeout=20.0)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.result["stdin"], "")

    def test_a_missing_program_is_reported_rather_than_launched(self) -> None:
        base = self.install(ANSWERS_WELL)
        (base / "run.cmd").unlink()
        addon = addons.read_manifest(base / addons.MANIFEST_NAME)
        self.assertFalse(addon.usable)
        reply = addons.run(addon, ["list"])
        self.assertFalse(reply.ok)
        assert reply.error is not None
        self.assertIn("is not there", reply.error)


class VersionSkewTests(Base):
    """An add-on built against a different protocol is listed and not run.

    Running it and interpreting the answer wrongly is the failure this avoids,
    and it is worse than not running it at all.
    """

    def test_a_newer_addon_is_visible_but_refused(self) -> None:
        self.install(ANSWERS_WELL, protocol=addons.PROTOCOL + 1)
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        self.assertFalse(addon.usable)
        self.assertIn("different version", addon.describe())

        reply = addons.run(addon, ["list"])
        self.assertFalse(reply.ok)
        assert reply.error is not None
        self.assertIn("protocol", reply.error)

    def test_the_add_on_is_not_started_at_all_when_the_protocol_differs(
        self,
    ) -> None:
        """Written so it would leave a mark if it ran."""
        marker = self.root / "it-ran"
        self.install(
            f"from pathlib import Path\nPath(r{str(marker)!r}).write_text('x')\n",
            protocol=addons.PROTOCOL + 1,
        )
        addon = addons.find("controller-pak", [self.root / addons.ADDON_DIRNAME])
        assert addon is not None
        addons.run(addon, ["list"])
        self.assertFalse(marker.exists())


class SearchRootTests(unittest.TestCase):
    def test_state_dir_and_its_addons_folder_are_both_searched(self) -> None:
        state = Path("C:/state") if sys.platform == "win32" else Path("/state")
        roots = addons.search_roots(state)
        self.assertIn(state / addons.ADDON_DIRNAME, roots)
        self.assertIn(state, roots)

    def test_the_addons_folder_is_searched_before_the_root(self) -> None:
        state = Path("C:/state") if sys.platform == "win32" else Path("/state")
        roots = addons.search_roots(state)
        self.assertLess(
            roots.index(state / addons.ADDON_DIRNAME), roots.index(state)
        )


if __name__ == "__main__":
    unittest.main()
