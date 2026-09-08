"""The Game Boy real-time clock, which Delta and RetroArch store differently.

Pokemon Crystal and Gold/Silver keep a real-time clock: day and night, the
Bug-Catching Contest, berry regrowth and Lapras are all gated on it. The clock
is *not* part of the battery save; it lives in a file of its own.

**What the stored value is, because it is easy to read as the wrong thing.** It
is a *base instant*: the moment the cartridge's clock read zero. Gambatte
derives the live reading as ``std::time(0) - baseTime_`` every time the game
latches the clock (``Rtc::doLatch``), so the file holds the starting point and
the clock is computed from it. It is **not** a play time, not a last-saved time,
and not a clock reading.

The base normally never moves for the life of a save. It changes only when the
*game* writes the MBC3 clock registers, or on the 511-day overflow -- and
Crystal does not write them; it keeps its own time reference inside the battery
save. Two consequences that have each caused a false alarm here: fast-forward
cannot advance the in-game clock, because it counts real seconds; and setting
the in-game time does not move the base either.

That is why the clock file still belongs with its save. Both machines must count
from the same instant, or they derive different in-game clocks from the same
cartridge -- and Gambatte's constructor starts ``baseTime_`` at zero, so a
machine with no clock file at all would compute decades of elapsed time.

Both formats were confirmed against real files on 2026-09-06, not inferred:

- **Delta** writes ``GameSave-<sha1>-gameTimeSave``: four bytes, big-endian.
  The Crystal save on this machine held ``6a 9d cb 79`` -> 1788726137 ->
  2026-09-06 20:22 UTC. Read little-endian the same bytes give 2034, so the
  byte order is evidence rather than a guess.
- **Gambatte** writes ``<content>.rtc`` beside the save: eight bytes,
  little-endian. A *freshly started* game under RetroArch held
  ``a6 0d 9e 6a 00 00 00 00`` -> 1788743078, 83 seconds before the file was
  inspected.

**Why both of those looked like play times, and are not.** A new game's clock
starts at zero *now*, so its base is ~now -- which for a fresh save is
indistinguishable from "when I last played". Both readings above were taken on
fresh saves, and the coincidence is what the earlier version of this docstring
mistook for the definition.

Settled against real state on 2026-09-08 rather than argued from source, because
source-derived answers have been wrong here before. Eight consecutive versions
of Crystal's Delta record, spanning 2026-09-07 01:05 to 15:08 UTC, show the
``gameSave`` hash changing six times while ``gameTimeSave`` stays byte-identical
throughout. The game was played and saved repeatedly; the clock value never
moved. A last-played timestamp would have moved every time.

So the conversion is a width-and-byte-order swap and nothing else: both sides
hold the same quantity, in different widths and byte orders. Verified on the
live files the same day -- Delta's ``6a 9d cb 79`` and RetroArch's
``79 cb 9d 6a 00 00 00 00`` decode to the identical 1788726137, and ``doctor``
reports both sides agreeing. It is lossless in both directions until the 32-bit
timestamp overflows in 2106, and ``to_delta`` refuses rather than truncating if
it ever sees a value that large.

**Gambatte only.** mGBA's libretro core writes a ``.rtc`` of the same name that
is a 48-byte ``GBMBCRTCSaveBuffer`` struct -- a different thing entirely, and
writing eight bytes over it would corrupt it. Which cores are cleared is held in
``System.clock_cores``, and nothing here decides that.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

#: Delta's format: four bytes, big-endian, seconds since the Unix epoch.
DELTA_BYTES = 4
#: Gambatte's format: eight bytes, little-endian, the same epoch.
RETROARCH_BYTES = 8

#: The largest timestamp Delta's four bytes can hold: 2106-02-07.
MAX_DELTA_TIMESTAMP = 0xFFFFFFFF


def delta_timestamp(data: bytes) -> int:
    """Read Delta's four-byte clock file."""
    if len(data) != DELTA_BYTES:
        raise ValueError(
            f"Delta's clock file should be {DELTA_BYTES} bytes, got {len(data)}"
        )
    return struct.unpack(">I", data)[0]


def retroarch_timestamp(data: bytes) -> int:
    """Read Gambatte's eight-byte clock file."""
    if len(data) != RETROARCH_BYTES:
        raise ValueError(
            f"Gambatte's clock file should be {RETROARCH_BYTES} bytes, "
            f"got {len(data)}"
        )
    return struct.unpack("<Q", data)[0]


def to_retroarch(data: bytes) -> bytes:
    """Delta's four big-endian bytes -> Gambatte's eight little-endian ones."""
    return struct.pack("<Q", delta_timestamp(data))


def to_delta(data: bytes) -> bytes:
    """Gambatte's eight little-endian bytes -> Delta's four big-endian ones.

    Refuses a value too large for four bytes rather than truncating it. That
    cannot happen before 2106, but truncating would silently move the clock by
    a hundred and thirty-six years, and a save is not the place to find out.
    """
    value = retroarch_timestamp(data)
    if value > MAX_DELTA_TIMESTAMP:
        raise ValueError(
            f"clock reads {value}, which does not fit in Delta's "
            f"{DELTA_BYTES} bytes; refusing to truncate it"
        )
    return struct.pack(">I", value)


def describe(timestamp: int) -> str:
    """A clock value as something a person can check against their memory.

    Local time, because the question being answered is about a moment in the
    reader's own day and nobody remembers what they were doing in UTC.

    Note what this value *is*, since it is easy to read it as the wrong thing:
    it is the instant the cartridge's clock read zero, not the last time the
    game was played. Gambatte derives the in-game clock from it as
    ``std::time(0) - baseTime`` (``Rtc::doLatch``), so it normally stays put for
    the life of a save and only moves when the game itself sets the clock.
    """
    try:
        moment = datetime.fromtimestamp(timestamp, timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError):
        return f"an unreadable time ({timestamp})"
    return moment.strftime("%Y-%m-%d %H:%M")


#: Gambatte's day counter is nine bits, so it wraps here and sets a carry flag
#: (``Rtc::doLatch``'s ``while (tmp > 0x1FF * 86400)`` loop). Worth reporting
#: rather than printing an implausible number of days as if it were normal.
DAY_OVERFLOW = 0x1FF


def cartridge_clock(base: int, now: float) -> tuple[int, int, int, int]:
    """What the cartridge's clock reads, as days/hours/minutes/seconds.

    This is the whole of Gambatte's model: ``std::time(0) - baseTime_``. The
    clock is never stored as a value anywhere -- it is derived, every time, from
    how long ago the base was. That is what keeps it running while nothing is
    running, and what makes it agree across two machines that share a base.
    """
    elapsed = max(0, int(now) - base)
    return (
        elapsed // 86400,
        elapsed % 86400 // 3600,
        elapsed % 3600 // 60,
        elapsed % 60,
    )


def describe_cartridge_clock(base: int, now: float) -> str:
    """The cartridge clock as a line someone can sanity-check."""
    days, hours, minutes, _ = cartridge_clock(base, now)
    reading = f"{days}d {hours:02d}:{minutes:02d}"
    if days > DAY_OVERFLOW:
        return f"{reading} (past the {DAY_OVERFLOW}-day counter, so it has wrapped)"
    return reading


def retroarch_clock_path(save_path: Path, extension: str) -> Path:
    """Where the core keeps the clock for a given save.

    RetroArch names it after the content, exactly as it does the save, so this
    is the save's path with a different extension. Confirmed on disk:
    ``Pokemon - Crystal Version.srm`` sits beside ``Pokemon - Crystal
    Version.rtc`` in the Gambatte save folder.
    """
    return save_path.with_name(f"{save_path.stem}.{extension.lstrip('.')}")
