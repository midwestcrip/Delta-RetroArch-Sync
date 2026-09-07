"""The gate that keeps a conversion away from a core it was not written for.

`n64.py` implements *Mupen64Plus-Next's* save layout, taken from
ra_mp64_srm_convert and checked against six real saves. Nothing has verified
that ParaLLEl N64 lays its `.srm` out the same way, and if it does not, writing
this layout into its file would corrupt a save silently rather than fail.

This is deliberately the same shape as ``clock_cores``, which exists because the
Game Boy real-time clock has four incompatible on-disk formats and only
Gambatte's has been checked against a real file. Same trap, same answer: a core
that has not been verified plays normally and gets no sync.

A plain-copy system is unaffected. RetroArch's frontend owns writing SRAM to
disk, so the file has the same shape whichever core reads it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_synchronizer import inspect as inspect_module  # noqa: E402
from delta_retroarch_synchronizer import sync, systems  # noqa: E402


def entry_for(key: str) -> inspect_module.GameEntry:
    system = systems.SYSTEMS[key]
    return inspect_module.GameEntry(
        identifier=key * 8,
        name=f"Test {system.name}",
        delta_type=system.delta_type,
        system=system,
        rom_path=None,
        save_path=None,
        extra_paths={},
    )


class GateTests(unittest.TestCase):
    def test_the_verified_core_syncs(self) -> None:
        self.assertIsNone(
            sync.unverified_save_core(entry_for("n64"), "Mupen64Plus-Next")
        )

    def test_an_unverified_core_is_refused_and_told_why(self) -> None:
        message = sync.unverified_save_core(entry_for("n64"), "ParaLLEl N64")

        assert message is not None
        self.assertIn("ParaLLEl N64", message)
        # Names the core to switch to, rather than leaving a dead end.
        self.assertIn("Mupen64Plus-Next", message)
        self.assertIn("Nothing was written", message)

    def test_a_plain_copy_system_is_never_gated(self) -> None:
        """The frontend owns the file for these, so the core cannot change it."""
        for key in ("gba", "snes", "gbc", "nes", "ds"):
            for core in systems.SYSTEMS[key].retroarch_cores:
                with self.subTest(system=key, core=core):
                    self.assertIsNone(
                        sync.unverified_save_core(entry_for(key), core)
                    )


class ConfigurationTests(unittest.TestCase):
    def test_every_converted_system_names_the_cores_it_was_checked_against(self) -> None:
        """Leaving this empty would silently re-open the gate for every core."""
        for system in systems.SYSTEMS.values():
            if system.converted:
                self.assertTrue(system.converted_cores, system.key)

    def test_the_verified_core_is_the_preferred_one(self) -> None:
        """Or the tool would prefer a core it then refuses to sync."""
        for system in systems.SYSTEMS.values():
            if system.converted_cores:
                self.assertEqual(
                    system.retroarch_cores[0], system.converted_cores[0], system.key
                )

    def test_verified_cores_are_a_subset_of_playable_ones(self) -> None:
        for system in systems.SYSTEMS.values():
            for core in system.converted_cores:
                self.assertIn(core, system.retroarch_cores, system.key)
            for core in system.clock_cores:
                self.assertIn(core, system.retroarch_cores, system.key)

    def test_n64_is_gated_to_mupen64plus_next_only(self) -> None:
        n64 = systems.SYSTEMS["n64"]
        self.assertEqual(n64.converted_cores, ("Mupen64Plus-Next",))
        self.assertIn("ParaLLEl N64", n64.retroarch_cores)


if __name__ == "__main__":
    unittest.main()
