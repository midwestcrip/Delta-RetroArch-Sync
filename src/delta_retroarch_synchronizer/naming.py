"""Turning Delta's game names into filenames RetroArch can use.

This is load-bearing rather than cosmetic. RetroArch names a battery save after
the *content file*, so whatever we call the ROM on disk decides what the save is
called. Change the ROM name later and RetroArch stops finding the save.

Delta stores display names, not filenames, and they contain characters Windows
forbids: the first real game synced here is "Pokemon: Fire Red Version", whose
colon cannot appear in a Windows path at all.
"""

from __future__ import annotations

import re

#: Characters Windows forbids anywhere in a filename.
_ILLEGAL = r'<>:"/\|?*'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")

#: Control characters are equally invalid and can arrive via odd metadata.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_WHITESPACE_RE = re.compile(r"\s+")

#: Device names Windows reserves regardless of extension.
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)


def safe_filename(name: str, *, fallback: str = "untitled") -> str:
    """Make ``name`` usable as a Windows filename, preserving readability.

    Accented characters are kept -- NTFS handles them and RetroArch displays
    them correctly, so stripping them would only make names worse. Only what
    Windows actually rejects is removed.

    A colon becomes " -" rather than vanishing, so "Pokemon: Fire Red Version"
    reads as "Pokemon - Fire Red Version" instead of running the words together.
    """
    cleaned = name.replace(":", " -")
    cleaned = _ILLEGAL_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    # Windows silently drops trailing dots and spaces, so a name ending in one
    # would not round-trip: we would write "Game." and later look for it by a
    # name that no longer matches what is on disk.
    cleaned = cleaned.rstrip(". ")

    if not cleaned:
        return fallback
    if cleaned.upper() in _RESERVED or cleaned.split(".")[0].upper() in _RESERVED:
        return f"{cleaned}_"
    return cleaned


def rom_filename(name: str, extension: str) -> str:
    """Filename for a ROM copied out of Delta, e.g. 'Pokemon - Fire Red.gba'."""
    return f"{safe_filename(name)}.{extension.lstrip('.')}"


def save_filename(name: str, extension: str) -> str:
    """Filename RetroArch will use for that ROM's battery save.

    Derived from the same sanitised stem as the ROM on purpose -- these two must
    agree or RetroArch will not associate the save with the game.
    """
    return f"{safe_filename(name)}.{extension.lstrip('.')}"
