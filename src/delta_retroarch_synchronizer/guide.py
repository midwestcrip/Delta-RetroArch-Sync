"""The setup instructions, as a checklist that knows how far you have got.

The third naive-user test judged the empty state "still a little undercooked":
the log said to install Dropbox and sign in, and to install RetroArch and run it
once, and pressing **Check status** answered only "Delta folder is not set or
missing". Two problems in one. The advice was a paragraph in a scrolling log,
which is the wrong shape for a procedure -- you cannot tell which part you have
already done -- and the button that exists to answer "where am I?" restated the
symptom instead.

So the steps live here, in order, each carrying whether it is already done. Not
a wall of text that has to be read from the top every time: on a machine where
Dropbox is signed in and RetroArch has never been opened, the one step that is
not yet done is the one thing worth reading.

Kept out of the launcher so it can be tested without a display, and so the same
list can answer both the window and ``doctor``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Marks a step in the rendered list. Deliberately redundant with the wording
#: and with the colour, so the list still reads correctly in a screenshot, in
#: monochrome, or to someone who cannot separate the two greys.
DONE = "✓"
TODO = "→"


@dataclass(frozen=True)
class Step:
    """One thing to do, and whether it has been done."""

    title: str
    body: str
    done: bool

    @property
    def marker(self) -> str:
        return DONE if self.done else TODO


@dataclass(frozen=True)
class Progress:
    """What is actually true about this machine right now.

    Every field is something discovery can answer without the user telling us
    anything, which is the point: a checklist that has to be filled in by hand
    is just a longer version of the paragraph it replaces.
    """

    dropbox_installed: bool = False
    dropbox_signed_in: bool = False
    delta_folder_found: bool = False
    retroarch_installed: bool = False
    retroarch_launched: bool = False
    cores_installed: int = 0


def setup_steps(progress: Progress) -> list[Step]:
    """The setup procedure in order, each step marked done or not.

    Ordered by dependency rather than by importance, because that is the order
    someone has to do them in: Delta cannot sync to a Dropbox that is not signed
    in, and RetroArch does not write the config we read until it has been opened
    once.
    """
    return [
        Step(
            "Install Dropbox and sign in",
            "Delta syncs through Dropbox, and this program reads the folder "
            "Dropbox keeps on this PC. Sign into the same account your phone "
            "uses. Google Drive will not work: Delta hides those files where "
            "nothing but Delta can read them.",
            progress.dropbox_signed_in,
        ),
        Step(
            "Turn on Delta Sync on your phone",
            "In Delta: Settings → Delta Sync → Dropbox. Let one full "
            "sync finish before coming back — a “Delta Emulator” "
            "folder appears in your Dropbox when it has.",
            progress.delta_folder_found,
        ),
        Step(
            "Install RetroArch and open it once",
            "RetroArch writes its settings file the first time it runs, and "
            "that file is how this program finds your saves. Install it, open "
            "it, close it. The download page carries advertisements dressed up "
            "as download buttons — the real one is the small link, and an "
            "ad blocker helps.",
            progress.retroarch_launched,
        ),
        Step(
            "Add the cores for the systems you play",
            "A core is RetroArch's emulator for one system. In RetroArch: Main "
            "Menu → Load Core → Download a Core. This program names "
            "the exact core it wants for each game you have.",
            progress.cores_installed > 0,
        ),
        Step(
            "Press Sync and Play",
            "That copies everything Delta has onto this PC, opens RetroArch, "
            "waits for you to finish, and copies your saves back out when you "
            "close it.",
            False,
        ),
    ]


def next_step(steps: list[Step]) -> Step | None:
    """The first thing still to do, which is the only one worth interrupting for."""
    return next((step for step in steps if not step.done), None)


#: Shown under the steps. The honest limits, in the place someone reads before
#: they start rather than after something surprises them.
NOTES = [
    (
        "Saves travel in both directions, but sending them back to Delta is "
        "off until you turn it on, and it is the only part of this that writes "
        "to Delta at all."
    ),
    (
        "Save states are not synced and cannot be. Delta records the exact "
        "version of the emulator that made each one, so a state from your phone "
        "would not load here anyway. Battery saves — the ones the game "
        "itself writes — are what travels."
    ),
    (
        "Nothing is ever overwritten without a copy being kept first. The "
        "Backups tab puts any of them back."
    ),
]
