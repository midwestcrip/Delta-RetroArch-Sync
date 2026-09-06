"""Converting Delta's cheats into RetroArch `.cht` files.

The two sides store the same codes in different shapes rather than different
encodings, so this is a reformat, not a decode:

- Delta stores a code as text laid out by the core's ``CheatFormat`` -- for GBA,
  ``"XXXXXXXX YYYYYYYY"`` (Action Replay, GameShark) or ``"XXXXXXXX YYYY"``
  (Code Breaker), one such line per code line.
- RetroArch writes the same hex words joined with ``+``:
  ``cheat0_code = "00000000+18002C02+0000E01A+00000000"``.

So the conversion is: take every hex digit in order, then re-group it by the
word sizes that cheat type uses. Working from the digits rather than from
Delta's whitespace means it does not matter whether Delta stored the code
formatted or bare.

Direction: Delta -> RetroArch only. Going the other way would mean creating new
``Cheat-<uuid>`` records in Delta's Dropbox folder, and a newly created file has
no Dropbox property groups -- which Harmony requires to see a record at all, and
which only Delta's own app can write (see ``docs/research.md``). A cheat added on
the RetroArch side is therefore invisible to Delta, and the sync does not pretend
otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import naming

_HEX = re.compile(r"[0-9A-Fa-f]+")

#: Hex-word sizes per Delta cheat type, from each core's ``supportedCheatFormats``
#: (e.g. ``GBADeltaCore/GBA.swift``). The pattern repeats for multi-line codes.
CODE_WORD_SIZES: dict[str, tuple[int, ...]] = {
    # GBA (GBADeltaCore/Types/GBATypes.m)
    "ActionReplay": (8, 8),
    "GameShark": (8, 8),
    "CodeBreaker": (8, 4),
    # GBC / NES / SNES
    "GameGenie": (8,),
    "ProActionReplay": (8,),
}

#: RetroArch groups cheat files by full system name, matching libretro-database.
DEFAULT_WORD_SIZES = (8, 8)


@dataclass
class Cheat:
    name: str
    code: str
    type: str


def code_words(code: str, cheat_type: str) -> list[str]:
    """Split a Delta code into the hex words RetroArch expects.

    Regrouping from the raw digits is deliberate. Delta may store the code with
    its display formatting or without, and a multi-line code arrives as one
    string either way; the word sizes are what actually define the boundaries.
    """
    digits = "".join(_HEX.findall(code)).upper()
    sizes = CODE_WORD_SIZES.get(cheat_type, DEFAULT_WORD_SIZES)

    words: list[str] = []
    index = 0
    position = 0
    while index < len(digits):
        size = sizes[position % len(sizes)]
        words.append(digits[index : index + size])
        index += size
        position += 1
    return [word for word in words if word]


def to_retroarch_code(code: str, cheat_type: str) -> str:
    return "+".join(code_words(code, cheat_type))


def _escape(text: str) -> str:
    """RetroArch's parser reads a quoted value, so a quote must not appear."""
    return text.replace('"', "'").replace("\n", " ").strip()


def render(cheats: list[Cheat]) -> str:
    """Render a complete `.cht` file.

    Cheats are written disabled. Enabling one is a decision about a save's
    contents, and a sync that silently switched cheats on would be making that
    decision on the user's behalf.
    """
    lines = [f"cheats = {len(cheats)}", ""]
    for index, cheat in enumerate(cheats):
        label = _escape(cheat.name) or f"Cheat {index + 1}"
        if cheat.type:
            label = f"{label} ({cheat.type})"
        lines.append(f'cheat{index}_desc = "{label}"')
        lines.append(f'cheat{index}_code = "{to_retroarch_code(cheat.code, cheat.type)}"')
        lines.append(f"cheat{index}_enable = false")
        lines.append("")
    return "\n".join(lines)


def cheat_file_path(cheat_dir: Path, database_name: str, game_name: str) -> Path:
    """Where RetroArch looks for a game's cheats.

    Mirrors libretro-database's layout, ``<cheats>/<System>/<ROM name>.cht``, so
    the file shows up where RetroArch's own cheat browser already points.
    """
    return cheat_dir / database_name / f"{naming.safe_filename(game_name)}.cht"


def write_cheat_file(path: Path, cheats: list[Cheat]) -> bool:
    """Write the file, returning whether anything changed.

    Skipping an identical write matters: RetroArch reads these files, and
    rewriting one during a sync pass would churn its mtime for no reason.
    """
    content = render(cheats)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return True
