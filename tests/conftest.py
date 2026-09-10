"""Put ``src`` on the path once, for the whole suite.

Most test files do this themselves with a ``sys.path.insert`` at the top, but
not all of them -- ``test_theme.py`` has none, and worked only because some
other file that happened to run first had already patched the path. The whole
suite passed and ``pytest tests/test_theme.py`` failed to collect, which is the
worst version of this: it looks fine until the one time you want to run a
single file, usually while chasing something else.

The per-file inserts are left where they are. They are harmless, and several of
them are load-bearing when a file is run directly as a script
(``python tests/test_paks.py``), which does not go through conftest at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
