"""Build the downloadable releases.

    python tools/build_release.py            both
    python tools/build_release.py --exe      standalone executable only
    python tools/build_release.py --zip      source zip only

Two artefacts, because they suit different people:

- **A standalone .exe** for someone who just wants the tool. No Python, no pip,
  no PATH. PyInstaller bundles the interpreter and tkinter alongside the code.
- **A source zip** for anyone who would rather read what they are running, or
  who is on a Python that PyInstaller does not support yet. It needs Python but
  nothing else -- the tool itself has no third-party dependencies.

PyInstaller is a build dependency only. Nothing it produces is imported by the
tool, and the zip build does not use it at all.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "Delta-RetroArch Synchronizer"

#: Files a source download needs. Deliberately a list rather than "everything
#: except gitignored": a release should never accidentally carry someone's
#: config.toml, Dropbox token, save backups or manifest.
SOURCE_INCLUDES = [
    "src",
    "assets",
    "docs",
    "tools",
    "tests",
    "README.md",
    "pyproject.toml",
    "config.example.toml",
    "launch_gui.pyw",
    "Delta-RetroArch Synchronizer.bat",
]

#: Never ship these even if they sit inside an included directory.
EXCLUDE_NAMES = {
    "config.toml",
    "dropbox-token.json",
    ".dropbox-auth-pending.json",
    "manifest.json",
    "backups",
    "__pycache__",
    ".pytest_cache",
}


def build_exe() -> Path | None:
    """One-file windowed build. Returns the executable, or None if it failed."""
    icon = ROOT / "assets" / "synchronizer.ico"
    work = ROOT / "build"

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # No console window: this is a GUI tool, and a black rectangle behind it
        # looks like something went wrong.
        "--windowed",
        "--name", NAME,
        "--icon", str(icon),
        # The icon is read at runtime for the window, so it has to be inside the
        # bundle as well as compiled into the exe's resources.
        "--add-data", f"{icon}{';' if sys.platform == 'win32' else ':'}assets",
        "--paths", str(ROOT / "src"),
        "--distpath", str(DIST),
        "--workpath", str(work),
        "--specpath", str(work),
        str(ROOT / "launch_gui.pyw"),
    ]

    print("Building executable…")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"PyInstaller failed (exit {result.returncode}).")
        return None

    produced = DIST / f"{NAME}.exe"
    return produced if produced.is_file() else None


def _should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_NAMES for part in path.parts)


def build_zip() -> Path:
    """Zip the source, excluding anything personal."""
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / f"{NAME} (source).zip"

    print("Building source zip…")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in SOURCE_INCLUDES:
            source = ROOT / entry
            if not source.exists():
                print(f"  skipped missing {entry}")
                continue
            if source.is_file():
                archive.write(source, Path(NAME) / entry)
                continue
            for path in sorted(source.rglob("*")):
                if path.is_dir() or _should_skip(path.relative_to(ROOT)):
                    continue
                archive.write(path, Path(NAME) / path.relative_to(ROOT))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the downloadable releases.")
    parser.add_argument("--exe", action="store_true", help="build only the executable")
    parser.add_argument("--zip", action="store_true", help="build only the source zip")
    args = parser.parse_args()

    both = not (args.exe or args.zip)
    DIST.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []

    if both or args.zip:
        built.append(build_zip())

    if both or args.exe:
        exe = build_exe()
        if exe is None:
            print("\nExecutable build failed. The source zip is unaffected.")
            if not both:
                return 1
        else:
            built.append(exe)

    print()
    for path in built:
        print(f"  {path.name}  ({path.stat().st_size / 1_048_576:.1f} MB)")
    print(f"\nIn {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
