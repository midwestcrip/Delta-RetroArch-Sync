"""Delta-RetroArch Controller Pak — the optional second program.

Delta does not sync Controller Pak data. ``GameSave.syncableFiles`` declares
``gameSave`` and, for Game Boy Color only, ``gameTimeSave``; there is no mempak
entry, so the ``.mpk`` files mupen64plus writes on the phone never reach
Dropbox and the main program can never see them. Everything else this project
does works against a folder on disk whether or not a phone is nearby. This does
not, and that is why it is a separate download.

**What it costs, and who pays it.** Reaching those files means
``pymobiledevice3``, Apple's usbmux service (which arrives with iTunes or the
Apple Devices app), a USB cable and a trust pairing. The main program's entire
dependency list is the standard library, and it stays that way: this ships as
its own executable so the cost lands only on the people who want the feature.

**It runs two ways, and neither is a lesser version of the other.**

- On its own, with its own window or from the command line.
- As part of the main program, which finds it beside itself and grows a
  Controller Pak tab. That is a *process* boundary -- the main program runs this
  one and reads JSON Lines back. It never imports it, because a frozen build
  cannot import another build's compiled wheels, and because a USB stall waiting
  on a trust dialog must not be able to freeze somebody's launcher.

**This program moves files. It does not write saves.** Pak data it fetches goes
into a folder, and the main program's own N64 code merges it into RetroArch's
combined ``.srm`` -- with the rolling backup, the size guard, and the rule that
a blank pak never overwrites one with notes on it. Putting that here would mean
a second implementation of save-writing living in the component with the
third-party dependency in it.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Must match ``addons.PROTOCOL`` in the main program. The main program refuses
#: to run an add-on whose number differs rather than risk reading its answer
#: wrongly, so this is the one constant that may not drift.
PROTOCOL = 1

#: Delta's own folder, inside its app container. Read from Delta's source:
#: ``Delta.coresDirectoryURL`` is the app's Documents plus ``Cores/``, the N64
#: core's ``directoryURL`` appends its name ``Mupen64Plus``, and
#: ``gameSaveDirectoryURL`` appends ``Saves/``. The same path the iOS Files app
#: shows at On My iPhone -> Delta -> Cores -> Mupen64Plus -> Saves.
DELTA_SAVES_PATH = "Cores/Mupen64Plus/Saves"

#: Delta's bundle identifier is *not* hard-coded as a certainty. Sideloaded and
#: AltStore copies differ, and a wrong one fails as "no such app" with nothing
#: to go on. ``probe`` lists what is actually installed and matches on this, so
#: the answer comes from the device rather than from here.
BUNDLE_HINTS = ("delta",)
