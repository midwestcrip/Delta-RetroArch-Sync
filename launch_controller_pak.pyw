"""Entry point for the Controller Pak add-on executable.

A ``.pyw`` like the main launcher, and for the same reason: no console flashes
up when somebody double-clicks it. That it is *also* the machine interface, the
one the main program drives with ``--json``, is not a compromise -- a windowed
PyInstaller build writes to a piped stdout perfectly well, which was measured
on 2026-09-09 before this file existed rather than assumed. Had it not, this
would need to be two executables: a console one for the protocol and a windowed
one for the window.

With no arguments it opens its own window. With a command it prints, for a
person or (with ``--json``) for the main program.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from delta_retroarch_controller_pak.__main__ import main  # noqa: E402

raise SystemExit(main())
