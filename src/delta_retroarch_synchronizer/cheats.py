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

Direction, and the one distinction that matters:

- **Editing a cheat that already exists** works in both directions. Its record
  is already in Delta's folder carrying the Dropbox property groups Harmony
  needs, so rewriting its contents is the same operation a save push is -- and
  simpler, because a cheat record has no attached files and therefore no Dropbox
  revision to resolve. That means it needs no Dropbox authorisation at all.
- **Creating a new cheat** on the RetroArch side is impossible, not merely
  unimplemented. It would mean a new ``Cheat-<uuid>`` file, and a file we create
  has no property groups -- which only Delta's own app can write (see
  ``docs/research.md``). Harmony's listing drops it silently, so it would look
  like it worked and never appear on the phone.

So a `.cht` entry that matches a Delta cheat by name can be edited home; one that
does not is reported as unpushable rather than half-attempted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import naming

_HEX = re.compile(r"[0-9A-Fa-f]+")

#: A rendered line: ``cheat0_desc = "Walk Through Walls"``.
_CHT_LINE = re.compile(r'^\s*cheat(?P<index>\d+)_(?P<key>desc|code)\s*=\s*"(?P<value>.*)"\s*$')

#: The ``(ActionReplay)`` suffix ``render`` appends to a description.
_TYPE_SUFFIX = re.compile(r"\s*\(([A-Za-z ]+)\)\s*$")

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


def canonical_code(code: str) -> str:
    """The shape both sides reduce to, for deciding whether a code changed.

    Just the hex digits, uppercased. Deliberately *not* the grouped form: the
    word sizes come from the cheat's type, so grouping two codes with different
    assumed types would make identical codes compare unequal. The digits are the
    cheat; the grouping is presentation, and each side formats it its own way.

    This is what stops a sync reporting an edit merely because Delta stores a
    code with the core's display spacing and RetroArch stores it joined with
    plus signs.
    """
    return "".join(_HEX.findall(code)).upper()


def to_delta_code(code: str, cheat_type: str) -> str:
    """Render a code back into the layout Delta stores.

    Delta keeps a cheat formatted the way its core displays it -- for a GBA
    Action Replay code, two eight-digit words per line separated by a space, with
    a newline between lines. One line is one full cycle through the type's word
    sizes, which is what makes this the exact inverse of ``code_words``.

    Verified as an exact round trip against a real Fire Red cheat record, which
    is the only reason to believe the line structure rather than merely the word
    sizes. Writing the wrong shape here would not corrupt anything -- Delta reads
    the digits too -- but it would rewrite every cheat's formatting on the phone
    the first time this ran, which is a change nobody asked for.
    """
    words = code_words(code, cheat_type)
    sizes = CODE_WORD_SIZES.get(cheat_type, DEFAULT_WORD_SIZES)
    lines = [
        " ".join(words[start : start + len(sizes)])
        for start in range(0, len(words), len(sizes))
    ]
    return "\n".join(lines)


def strip_type_suffix(description: str) -> str:
    """Recover a cheat's name from the description ``render`` wrote.

    ``render`` appends the cheat type in brackets so RetroArch's cheat list says
    what kind of code it is. Matching back to Delta has to undo that, and only
    for a bracketed word that really is a cheat type -- a cheat legitimately
    named "Infinite HP (Japan)" must keep its brackets.
    """
    match = _TYPE_SUFFIX.search(description)
    if match is None:
        return description.strip()
    if match.group(1).replace(" ", "") not in CODE_WORD_SIZES:
        return description.strip()
    return description[: match.start()].strip()


def parse_cheat_file(text: str) -> list[Cheat]:
    """Read a `.cht` back into cheats, keyed the way Delta names them.

    RetroArch's own cheat editor writes these files too, and it does not promise
    the ordering or spacing this tool emits, so this parses by index rather than
    by position and tolerates missing entries. The ``type`` is left empty: it is
    not recoverable from a code, it lives in Delta's record as an archived plist
    we never rewrite, and the description's bracketed hint is a label rather than
    a source of truth.
    """
    found: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        match = _CHT_LINE.match(line)
        if match is None:
            continue
        entry = found.setdefault(int(match.group("index")), {})
        entry[match.group("key")] = match.group("value")

    cheats: list[Cheat] = []
    for index in sorted(found):
        entry = found[index]
        if "code" not in entry:
            continue
        cheats.append(
            Cheat(
                name=strip_type_suffix(entry.get("desc", "")),
                code=entry["code"],
                type="",
            )
        )
    return cheats


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
