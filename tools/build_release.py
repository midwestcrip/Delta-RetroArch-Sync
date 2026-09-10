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
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "Delta-RetroArch Synchronizer"

#: The optional second download. Its own executable so its dependency --
#: pymobiledevice3, and Apple's usbmux service behind it -- never reaches
#: anyone who does not want Controller Pak support.
ADDON_NAME = "Delta-RetroArch Controller Pak"

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
    "launch_controller_pak.pyw",
    "Delta-RetroArch Synchronizer.bat",
]

#: Never ship these even if they sit inside an included directory.
EXCLUDE_NAMES = {
    "config.toml",
    "dropbox-token.json",
    ".dropbox-auth-pending.json",
    "manifest.json",
    "backups",
    "recovered",
    "__pycache__",
    ".pytest_cache",
}


# Finding PyInstaller's bootloader directory is fiddly enough to be worth one
# implementation rather than two: an install can carry several platform
# directories, so the right one has to be derived from the running interpreter.
# build_bootloader.py owns that, and both scripts live in tools/, which is on
# sys.path whenever either is run directly. Guarded so that a checkout missing
# build_bootloader.py can still produce a release, just without the warning.
try:
    from build_bootloader import MARKER_NAME as BOOTLOADER_MARKER
    from build_bootloader import bootloader_dir
except ImportError:  # pragma: no cover -- only when tools/ is incomplete
    BOOTLOADER_MARKER = "LOCALLY_BUILT.txt"

    def bootloader_dir() -> Path | None:
        return None


def _report_bootloader(windowed: bool = True) -> None:
    """Say which bootloader is about to be baked into the executable.

    Not a gate, deliberately -- a release can still be built without this, and
    on a machine with no compiler that may be the only option. But it must never
    happen *silently*, because the stock bootloader is the known cause of
    Trojan:Win32/Wacatac.C!ml and the failure is invisible until a stranger's
    Defender deletes the download.
    """
    directory = bootloader_dir()
    if directory is None:
        print("Bootloader   : could not locate PyInstaller's bootloader directory")
        return

    binary = directory / ("runw.exe" if windowed else "run.exe")
    if not binary.is_file():
        print(f"Bootloader   : expected {binary.name} in {directory}, not found")
        return

    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    local = (directory / BOOTLOADER_MARKER).is_file()
    print(f"Bootloader   : {binary.name} {digest[:16]}...")
    if local:
        print("               locally compiled")
    else:
        print("               *** STOCK BOOTLOADER -- shipped with the PyInstaller wheel.")
        print("               *** Defender flags these as Trojan:Win32/Wacatac.C!ml.")
        print("               *** Run tools/build_bootloader.py before releasing.")


def build_exe(onefile: bool = False) -> Path | None:
    """Windowed build. Returns the executable or its folder, None on failure.

    Defaults to a **one-directory** build. A one-file build unpacks itself into
    a temporary folder on every launch, which is behaviourally what a packer or
    dropper does. One-directory performs no self-extraction and starts faster
    for the same reason, so it remains the default.

    It does **not**, however, avoid Microsoft Defender. An earlier version of
    this docstring claimed it "sidesteps the heuristic"; the naive-user test on
    2026-09-06 falsified that. A one-directory build downloaded onto a clean
    Windows account was quarantined on execution as Trojan:Win32/Wacatac.C!ml
    (severity Severe) and the executable deleted. Note the C variant: VirusTotal's
    Microsoft engine predicts B!ml, the shipping product fires C!ml, and only the
    latter is what users actually meet.

    What does clear it is compiling PyInstaller's bootloader locally, because the
    stock bootloader ships byte-identical to everyone and is shared with a great
    deal of real malware. See tools/build_bootloader.py, which must be run before
    a release build; this script does not do it, it only reports which bootloader
    it used.

    The cost of one-directory is that it is a folder rather than one file, which
    is why it ships zipped.
    """
    icon = ROOT / "assets" / "synchronizer.ico"
    work = ROOT / "build"

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        # UPX compression is an antivirus trigger in its own right. It is not
        # installed here, but this stops a machine that happens to have it from
        # silently producing a more-flagged binary.
        "--noupx",
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

    _report_bootloader(windowed=True)

    print("Building executable...")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"PyInstaller failed (exit {result.returncode}).")
        return None

    if onefile:
        produced = DIST / f"{NAME}.exe"
        return produced if produced.is_file() else None

    folder = DIST / NAME
    return folder if (folder / f"{NAME}.exe").is_file() else None


def build_addon() -> Path | None:
    """Build the Controller Pak add-on: the optional separate download.

    A second executable rather than a feature of the first, because reaching
    Delta's Controller Pak files needs ``pymobiledevice3``, Apple's usbmux
    service, a cable and a trust pairing -- and the main program's dependency
    list is the standard library. Shipping it separately puts that cost on the
    people who want the feature and nobody else.

    **One executable, windowed, doing both jobs.** It opens a window when
    double-clicked and speaks JSON Lines when the main program runs it with
    ``--json``. That works because a windowed PyInstaller build still writes to
    a stdout its parent hands it -- measured on 2026-09-09 with a throwaway
    build, not assumed. Had it been false this would be two executables, a
    console one for the protocol and a windowed one for the window.

    ``--collect-all`` rather than ``--hidden-import``: pymobiledevice3 is
    imported lazily inside functions so nothing static finds it, and it carries
    data files besides.
    """
    work = ROOT / "build-addon"
    icon = ROOT / "assets" / "synchronizer.ico"
    separator = ";" if sys.platform == "win32" else ":"

    try:
        import pymobiledevice3  # noqa: F401

        has_library = True
    except ImportError:
        has_library = False

    if not has_library:
        print("  *** pymobiledevice3 is not installed in this environment.")
        print("  *** The add-on will build, and will report it is missing when")
        print("  *** asked to talk to a phone -- which makes it useless as a")
        print("  *** release. Install it before building one to ship:")
        print("  ***     pip install pymobiledevice3")

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noupx",
        "--windowed",
        "--name", ADDON_NAME,
        "--icon", str(icon),
        "--add-data", f"{icon}{separator}assets",
        "--paths", str(ROOT / "src"),
        "--distpath", str(DIST),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    if has_library:
        command += ["--collect-all", "pymobiledevice3"]
    command.append(str(ROOT / "launch_controller_pak.pyw"))

    print("Building the Controller Pak add-on...")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"PyInstaller failed (exit {result.returncode}).")
        return None

    folder = DIST / ADDON_NAME
    if not (folder / f"{ADDON_NAME}.exe").is_file():
        return None

    write_addon_manifest(folder)
    return folder


def write_addon_manifest(folder: Path) -> Path:
    """The file that makes the main program notice this download.

    ``executable`` is deliberately a bare filename. The main program refuses a
    manifest naming a path, because a manifest is a text file on disk that says
    which program to run and there is no reason for it to be able to point
    outside its own folder.

    ``protocol`` is read from the add-on's own source, so the number in the
    shipped manifest cannot drift from the number the program answers with.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from delta_retroarch_controller_pak import PROTOCOL, __version__

    manifest = folder / "addon.json"
    manifest.write_text(
        json.dumps(
            {
                "addon": "controller-pak",
                "name": "Controller Pak",
                "version": __version__,
                "protocol": PROTOCOL,
                "executable": f"{ADDON_NAME}.exe",
                "provides": ["controller-pak"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def zip_folder(folder: Path) -> Path:
    """Zip a one-directory build so it remains a single download."""
    target = DIST / f"{folder.name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))
    return target


def _should_skip(path: Path) -> bool:
    return any(part in EXCLUDE_NAMES for part in path.parts)


def build_zip() -> Path:
    """Zip the source, excluding anything personal."""
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / f"{NAME} (source).zip"

    print("Building source zip...")
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
    parser.add_argument(
        "--addon",
        action="store_true",
        help="build only the Controller Pak add-on (the separate download)",
    )
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="single self-extracting exe; convenient, but slower to start",
    )
    args = parser.parse_args()

    both = not (args.exe or args.zip or args.addon)
    DIST.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []

    if both or args.zip:
        built.append(build_zip())

    if both or args.exe:
        exe = build_exe(onefile=args.onefile)
        if exe is None:
            print("\nExecutable build failed. The source zip is unaffected.")
            if not both:
                return 1
        elif exe.is_dir():
            built.append(zip_folder(exe))
        else:
            built.append(exe)

    if both or args.addon:
        addon = build_addon()
        if addon is None:
            print("\nAdd-on build failed. The other downloads are unaffected.")
            if not both:
                return 1
        else:
            built.append(zip_folder(addon))

    print()
    for path in built:
        print(f"  {path.name}  ({path.stat().st_size / 1_048_576:.1f} MB)")
    print(f"\nIn {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
