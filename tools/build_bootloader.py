"""Compile PyInstaller's bootloader locally, so Defender stops deleting the app.

    python tools/build_bootloader.py             rebuild, then verify it changed
    python tools/build_bootloader.py --check     report what is installed now
    python tools/build_bootloader.py --workdir C:\\somewhere\\short

Run this **before** tools/build_release.py. That script reports which bootloader
it baked in but does not build one, so a release made without this step ships
the stock binary and is quarantined on strangers' machines.

Why this exists
---------------
Every PyInstaller wheel ships the same prebuilt bootloader -- the small C program
that unpacks and starts a frozen app. Because those bytes are identical for
everyone, they also sit inside a great deal of real malware, and Microsoft's
machine-learning models have learned them. On 2026-09-06 a one-directory release
build of this tool, downloaded onto a clean Windows account, was quarantined on
execution as Trojan:Win32/Wacatac.C!ml (severity Severe) and the executable was
deleted with no way forward that a normal person would find.

Compiling the bootloader here produces bytes nobody has seen before. Measured on
one machine a minute apart, with the same signature version: the stock build
scanned as "found 1 threats", the locally compiled build as "found no threats",
and it stayed clean with mark-of-the-web attached.

That is not a permanent guarantee. These verdicts are assigned after Microsoft's
cloud sees a file, not on first sight -- the flagged build also scanned clean for
its first ten hours. Treat a clean scan as the absence of a known problem, never
as proof of safety, and re-test on a clean account before each release.

Two traps this script exists to prevent
---------------------------------------
1. ``pip install --no-binary pyinstaller`` does **nothing**. It reports building
   a wheel from source and succeeds, but PyInstaller's sdist *contains* the
   prebuilt run.exe/runw.exe, so the build backend simply repackages them. The
   result is byte-identical and no compiler ever runs. This script therefore
   hashes the bootloader before and after and fails loudly if it did not change.

2. waf fails with ``LNK1104: cannot open file ... .manifest`` under a long path.
   That reads like a broken toolchain but is MAX_PATH: waf adds hash-named
   directories, and the link step tips past 260 characters. Build from a short
   root. The default below is deliberately near the drive root.

Requirements: Visual Studio Build Tools with the C++ workload (the MSVC compiler
is found automatically by waf; no vcvars shell is needed) and network access to
fetch the matching PyInstaller sdist.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

#: Anything longer risks the MAX_PATH failure described above. waf's deepest
#: intermediate path ran roughly 150 characters past the working directory, so
#: this leaves comfortable headroom under the 260-character limit.
MAX_WORKDIR_LEN = 60

#: Overridable because the right short path differs per machine; a drive other
#: than C: is fine, depth is what matters.
DEFAULT_WORKDIR = Path(os.environ.get("PYI_BOOTLOADER_WORKDIR", r"C:\pyi-build"))

#: Dropped beside the compiled binaries. tools/build_release.py checks for it,
#: which is how a release build can tell a compiled bootloader from a stock one
#: without hardcoding hashes that change with every PyInstaller version.
MARKER_NAME = "LOCALLY_BUILT.txt"


def fail(message: str) -> int:
    print(f"\nERROR: {message}")
    return 1


def platform_dir_name() -> str:
    """The bootloader directory name PyInstaller uses for this machine.

    PyInstaller names these '<System>-<bits>bit[-<arch>]' -- Windows-64bit-intel,
    Darwin-64bit, and so on. An install can carry several: a wheel built for one
    platform still ships others, and a source tree carries every directory it has
    ever built. So picking "the only subdirectory" is wrong; the name has to be
    derived from the interpreter actually running.
    """
    system = platform.system()
    bits = 8 * struct.calcsize("P")
    machine = platform.machine().lower()
    arch = "arm" if ("arm" in machine or "aarch" in machine) else "intel"
    return f"{system}-{bits}bit-{arch}"


def bootloader_dir() -> Path | None:
    """PyInstaller's bootloader directory for this interpreter, if importable."""
    try:
        import PyInstaller
    except ImportError:
        return None

    root = Path(PyInstaller.__file__).resolve().parent / "bootloader"
    if not root.is_dir():
        return None

    wanted = platform_dir_name()
    exact = root / wanted
    if exact.is_dir():
        return exact

    # macOS omits the architecture suffix, and older layouts may differ again.
    # Fall back to a prefix match on system and word size before giving up.
    prefix = wanted.rsplit("-", 1)[0]
    candidates = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    return candidates[0] if len(candidates) == 1 else None


def installed_pyinstaller() -> tuple[str, Path] | None:
    """Version and bootloader directory of the PyInstaller in this interpreter."""
    try:
        import PyInstaller
    except ImportError:
        return None

    directory = bootloader_dir()
    return (PyInstaller.__version__, directory) if directory is not None else None


def hash_bootloaders(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.exe"))
    }


def report(directory: Path, version: str) -> None:
    marker = directory / MARKER_NAME
    print(f"PyInstaller {version}")
    print(f"  {directory}")
    for name, digest in hash_bootloaders(directory).items():
        print(f"    {name:<12} {digest}")
    print()
    if marker.is_file():
        print("  Locally compiled:")
        for line in marker.read_text(encoding="utf-8").splitlines():
            print(f"    {line}")
    else:
        print("  STOCK -- shipped with the wheel. Defender flags these.")


def fetch_source(version: str, workdir: Path, fresh: bool) -> Path | None:
    """Download and unpack the matching sdist, reusing an existing tree."""
    tree = workdir / f"pyinstaller-{version}"

    if tree.is_dir() and not fresh:
        print(f"Reusing source tree at {tree}")
        return tree
    if tree.is_dir():
        print(f"Removing {tree}")
        shutil.rmtree(tree, ignore_errors=True)

    workdir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading pyinstaller=={version} sdist...")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "download",
            f"pyinstaller=={version}",
            "--no-binary", ":all:",
            "--no-deps",
            "-d", str(workdir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        return None

    archives = sorted(workdir.glob(f"pyinstaller-{version}.tar.gz"))
    if not archives:
        return None

    print(f"Unpacking {archives[0].name}...")
    with tarfile.open(archives[0]) as archive:
        # filter="data" is the safe extraction mode; without it Python 3.14
        # warns, and older tarfile would happily write outside the directory.
        archive.extractall(workdir, filter="data")

    return tree if tree.is_dir() else None


def run_waf(tree: Path) -> bool:
    """Configure and build every bootloader variant."""
    bootloader = tree / "bootloader"
    if not (bootloader / "waf").is_file():
        print(f"No waf script in {bootloader}")
        return False

    # A stale build directory keeps waf's cached configuration, including the
    # failed MAX_PATH probe from a previous location.
    shutil.rmtree(bootloader / "build", ignore_errors=True)

    print("Compiling bootloader (waf all)...")
    result = subprocess.run(
        [sys.executable, "./waf", "all"],
        cwd=bootloader,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
        print("\n".join(tail))
        if "LNK1104" in result.stdout + result.stderr:
            print("\nThat LNK1104 is MAX_PATH. Use a shorter --workdir.")
        return False

    print("Compiled.")
    return True


def install_built(tree: Path, target: Path) -> list[str]:
    """Copy freshly compiled bootloaders over the installed ones."""
    source = tree / "PyInstaller" / "bootloader" / target.name
    if not source.is_dir():
        return []

    copied = []
    for built in sorted(source.glob("*.exe")):
        shutil.copy2(built, target / built.name)
        copied.append(built.name)
    return copied


def write_marker(target: Path, version: str, tree: Path) -> None:
    lines = [
        f"PyInstaller {version}",
        f"built from {tree}",
        f"on {__import__('datetime').datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    lines += [f"{name}  {digest}" for name, digest in hash_bootloaders(target).items()]
    (target / MARKER_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile PyInstaller's bootloader locally.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the installed bootloader and exit",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=DEFAULT_WORKDIR,
        help=f"short build path (default {DEFAULT_WORKDIR})",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="re-download the sdist instead of reusing an unpacked tree",
    )
    args = parser.parse_args()

    found = installed_pyinstaller()
    if found is None:
        return fail(
            "PyInstaller is not importable here, or its bootloader directory is "
            "not laid out as expected. Install PyInstaller into this interpreter."
        )
    version, target = found

    if args.check:
        report(target, version)
        return 0

    if len(str(args.workdir)) > MAX_WORKDIR_LEN:
        return fail(
            f"--workdir is {len(str(args.workdir))} characters; keep it under "
            f"{MAX_WORKDIR_LEN} or the link step fails with LNK1104 (MAX_PATH)."
        )

    before = hash_bootloaders(target)
    if not before:
        return fail(f"No bootloader binaries in {target}")

    print(f"PyInstaller {version}")
    print(f"Installed bootloader: {target}")
    for name, digest in before.items():
        print(f"  before  {name:<12} {digest[:16]}...")
    print()

    tree = fetch_source(version, args.workdir, args.fresh)
    if tree is None:
        return fail(f"Could not obtain the pyinstaller=={version} source.")

    if not run_waf(tree):
        return fail("The bootloader did not compile. Nothing was changed.")

    copied = install_built(tree, target)
    if not copied:
        return fail(f"waf reported success but produced nothing for {target.name}.")

    after = hash_bootloaders(target)
    print()
    for name, digest in after.items():
        changed = "changed" if before.get(name) != digest else "UNCHANGED"
        print(f"  after   {name:<12} {digest[:16]}...  {changed}")

    # The whole point of the exercise. If the bytes match what shipped with the
    # wheel then nothing was really rebuilt -- most likely the sdist's own
    # prebuilt binaries were copied back over themselves -- and a release built
    # now would carry the flagged bootloader while appearing to have been fixed.
    if all(before.get(name) == digest for name, digest in after.items()):
        return fail(
            "Every bootloader is byte-identical to the one that was already "
            "installed. Nothing was recompiled; do not cut a release from this."
        )

    write_marker(target, version, tree)
    print(f"\nWrote {target / MARKER_NAME}")
    print("Now run: python tools/build_release.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
