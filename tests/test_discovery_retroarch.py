"""Tests for locating a RetroArch that the installer never registered.

The gap this closes, found on 2026-09-06: the uninstall registry entry named
C:\\RetroArch-Win64, a folder that had since been deleted, while the RetroArch
actually in use sat under C:\\Media\\Games\\Emulators after being extracted from
the portable zip. Discovery reported nothing and the path had to be set by hand.

MuiCache is the signal that closes it -- Windows records the full path of every
executable the user has actually run, wherever it lives.
"""

from __future__ import annotations

from pathlib import Path

from delta_retroarch_synchronizer.discovery import retroarch_dirs_from_muicache


def test_finds_a_portable_install():
    names = [
        r"C:\Media\Games\Emulators\RetroArch\retroarch.exe.FriendlyAppName",
        r"C:\Windows\system32\notepad.exe.FriendlyAppName",
    ]

    assert retroarch_dirs_from_muicache(names) == [
        Path(r"C:\Media\Games\Emulators\RetroArch")
    ]


def test_both_value_suffixes_resolve_to_one_directory():
    """Windows writes several values per executable; they are the same install."""
    names = [
        r"C:\Games\RetroArch\retroarch.exe.FriendlyAppName",
        r"C:\Games\RetroArch\retroarch.exe.ApplicationCompany",
    ]

    assert retroarch_dirs_from_muicache(names) == [Path(r"C:\Games\RetroArch")]


def test_several_installs_are_all_returned_in_order():
    """Caller filters by which one actually has a retroarch.cfg."""
    names = [
        r"D:\Portable\RetroArch\retroarch.exe.FriendlyAppName",
        r"C:\RetroArch-Win64\retroarch.exe.FriendlyAppName",
    ]

    assert retroarch_dirs_from_muicache(names) == [
        Path(r"D:\Portable\RetroArch"),
        Path(r"C:\RetroArch-Win64"),
    ]


def test_case_is_not_load_bearing():
    names = [r"C:\Games\RetroArch\RetroArch.EXE.FriendlyAppName"]

    assert retroarch_dirs_from_muicache(names) == [Path(r"C:\Games\RetroArch")]


def test_the_installer_itself_is_not_an_install():
    """Running the setup exe leaves an entry too, and its folder is Downloads."""
    names = [r"C:\Users\me\Downloads\RetroArch-Win64-setup.exe.FriendlyAppName"]

    assert retroarch_dirs_from_muicache(names) == []


def test_values_without_a_known_suffix_are_ignored():
    names = [r"C:\Games\RetroArch\retroarch.exe", "LangID", ""]

    assert retroarch_dirs_from_muicache(names) == []


def test_unrelated_executables_are_ignored():
    names = [
        r"C:\Program Files\Firefox\firefox.exe.FriendlyAppName",
        r"C:\thing\dolphin.exe.ApplicationCompany",
    ]

    assert retroarch_dirs_from_muicache(names) == []


def test_empty_registry():
    assert retroarch_dirs_from_muicache([]) == []
