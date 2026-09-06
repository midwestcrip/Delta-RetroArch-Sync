"""Where the tool keeps its files, running from source or as a frozen exe.

These have to be separated, because in a PyInstaller build they are not the same
place and getting it wrong fails quietly:

- **Resources** (the icon) are unpacked into a temporary directory that is
  deleted when the process exits. ``sys._MEIPASS`` points at it.
- **State** (config.toml, the Dropbox token, the manifest, backups) must outlive
  the process. Writing it beside the resources would mean losing the manifest
  every run -- and losing the manifest means losing the record of what both
  sides last agreed on, which is what conflict detection is built from. The
  tool would silently start reporting conflicts on every sync.

Running from source both are the project directory, which is why this only
became a real distinction once there was something to package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Delta-RetroArch Synchronizer"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Read-only files shipped with the tool."""
    if is_frozen():
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return Path(bundled)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _is_writable(directory: Path) -> bool:
    probe = directory / ".write-test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def state_dir() -> Path:
    """Somewhere durable for config, credentials, the manifest and backups.

    Beside the executable when that is writable, which is what someone
    unzipping a portable build expects and keeps everything together. Program
    Files and similar are not writable, so fall back to LocalAppData rather
    than failing at the first save.
    """
    if not is_frozen():
        return Path(__file__).resolve().parents[2]

    beside = Path(sys.executable).resolve().parent
    if _is_writable(beside):
        return beside

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    fallback = Path(base) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback
