"""Entry point for the Start menu shortcut and the .bat.

A .pyw file rather than a .py: Windows runs it with pythonw, so no console
window flashes up behind the launcher. Kept at the project root so the shortcut
can point at one stable path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from delta_retroarch_synchronizer import launcher  # noqa: E402

raise SystemExit(launcher.main())
