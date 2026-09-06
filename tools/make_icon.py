"""Generate the launcher's icon.

Build-time only. Pillow is needed to run this, but not to run the tool -- the
generated .ico is committed, so a normal install still needs nothing but the
standard library. Re-run it only if the icon should change.

    python tools/make_icon.py

Design notes: the icon has to survive being drawn at 16x16 in the Start menu and
the taskbar, which is where most icons fall apart. So it is one bold shape (a
delta) on a solid field, with the sync ring kept low-contrast enough to read as
texture rather than turning into noise when it is four pixels wide. Everything
is drawn at 8x and downsampled per size, because Pillow's polygon fill is not
antialiased and a hard-edged triangle looks visibly jagged at 32px and below.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SUPERSAMPLE = 8
#: Sizes Windows actually asks for: Start menu and taskbar use the small ones,
#: the file browser's large view uses 256.
SIZES = (256, 128, 64, 48, 32, 16)

DELTA_PURPLE = (124, 77, 217, 255)
DELTA_PURPLE_DARK = (86, 50, 160, 255)
RING = (255, 255, 255, 128)
WHITE = (255, 255, 255, 255)


def rounded_square(draw: ImageDraw.ImageDraw, size: int) -> None:
    """A flat field. A two-tone split was tried and read as a seam, not depth."""
    radius = int(size * 0.22)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=DELTA_PURPLE)


def sync_ring(draw: ImageDraw.ImageDraw, size: int) -> None:
    """A broken circle with two arrowheads: the usual 'in sync' motif."""
    inset = size * 0.16
    box = [inset, inset, size - inset, size - inset]
    width = max(2, int(size * 0.045))

    draw.arc(box, start=205, end=335, fill=RING, width=width)
    draw.arc(box, start=25, end=155, fill=RING, width=width)

    centre = size / 2
    radius = (size - 2 * inset) / 2
    head = size * 0.055
    for angle_deg, direction in ((335, 1), (155, -1)):
        angle = math.radians(angle_deg)
        x = centre + radius * math.cos(angle)
        y = centre + radius * math.sin(angle)
        draw.polygon(
            [
                (x - head * direction, y - head),
                (x + head * direction, y),
                (x - head * direction, y + head),
            ],
            fill=RING,
        )


def delta(draw: ImageDraw.ImageDraw, size: int, *, bold: bool) -> None:
    """The Greek capital delta -- Delta's initial, and the icon's anchor shape.

    ``bold`` widens and enlarges it for the sizes that carry no ring, so the
    triangle fills the space the ring vacated instead of floating in it.
    """
    centre = size / 2
    half_width = size * (0.315 if bold else 0.235)
    top = size * (0.235 if bold else 0.30)
    bottom = size * (0.775 if bold else 0.715)
    stroke = max(2, int(size * (0.105 if bold else 0.075)))

    outer = [(centre, top), (centre + half_width, bottom), (centre - half_width, bottom)]
    draw.polygon(outer, fill=WHITE)

    # Hollow it out so the shape stays open at small sizes; a solid triangle
    # turns into an indistinct blob once it is 16px wide.
    shrink = stroke * 1.6
    inner = [
        (centre, top + shrink * 1.5),
        (centre + half_width - shrink, bottom - shrink * 0.72),
        (centre - half_width + shrink, bottom - shrink * 0.72),
    ]
    draw.polygon(inner, fill=DELTA_PURPLE)


#: Below this, the sync ring stops being a ring and becomes grey mush around
#: the delta. Checked by rendering the real .ico entries magnified: at 16px the
#: two shapes merge into one indistinct blob, which is precisely the size the
#: Start menu and taskbar use most.
RING_MIN_SIZE = 24


def render(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    with_ring = size >= RING_MIN_SIZE
    rounded_square(draw, big)
    if with_ring:
        sync_ring(draw, big)
    delta(draw, big, bold=not with_ring)

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    target = root / "assets" / "synchronizer.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    frames = [render(size) for size in SIZES]
    # Pillow writes every supplied size into the .ico when they are passed as
    # append_images, so Windows can pick the right one instead of rescaling.
    frames[0].save(
        target,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=frames[1:],
    )
    render(256).save(root / "assets" / "synchronizer.png")
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
