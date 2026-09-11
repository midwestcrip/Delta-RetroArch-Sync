"""Where Controller Pak files come from, and the two places that can be.

One interface, two implementations, and the split is what makes this program
testable at all:

- :class:`FolderSource` reads a folder on disk. Not a stub -- it is a real way
  to use this. Delta sets ``UIFileSharingEnabled``, so the Saves folder can be
  dragged off in Explorer or the iOS Files app by hand, and everything
  downstream works identically on the result.
- :class:`DeviceSource` reaches into Delta's container over USB. This is the
  part that needs ``pymobiledevice3``, Apple's usbmux service, a cable and a
  trust pairing.

Because both satisfy the same surface, every command, every refusal and every
byte of the merge logic is exercised without a phone in the room. What a phone
is genuinely required for is one thing: whether ``house_arrest`` opens Delta's
container on a real device, and what the files in it are actually called.
Nothing here guesses at that -- ``listing`` reports what it finds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from . import BUNDLE_HINTS, DELTA_SAVES_CANDIDATES, DELTA_SAVES_PATH


class SourceError(Exception):
    """The pak files could not be reached, with the reason and the fix."""


def _run(coroutine):
    """Run one piece of async work from this program's synchronous world.

    pymobiledevice3 is async all the way down; this program is not. It runs one
    command and exits, or drives a tkinter loop that is not asyncio's. A fresh
    event loop per operation is the honest translation between the two, and at
    one operation per command it costs nothing.
    """
    return asyncio.run(coroutine)


@dataclass(frozen=True)
class RemoteFile:
    """One file wherever the paks live."""

    name: str
    size: int

    def as_json(self) -> dict:
        return {"name": self.name, "size": self.size}


class FolderSource:
    """Pak files in a folder on disk.

    The manual route, and the fallback whenever the USB one will not work: copy
    ``Delta/Cores/Mupen64Plus/Saves`` off the phone by hand and point this at
    it. Also how every test in this program runs.
    """

    kind = "folder"

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def describe(self) -> str:
        return f"folder {self.folder}"

    def listing(self) -> list[RemoteFile]:
        if not self.folder.is_dir():
            raise SourceError(f"{self.folder} is not a folder")
        return sorted(
            (
                RemoteFile(path.name, path.stat().st_size)
                for path in self.folder.iterdir()
                if path.is_file()
            ),
            key=lambda item: item.name.lower(),
        )

    def read_many(self, names: list[str]) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        for name in names:
            path = self.folder / Path(name).name
            if not path.is_file():
                raise SourceError(f"{name} is not in {self.folder}")
            found[name] = path.read_bytes()
        return found

    def write_many(self, files: dict[str, bytes]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (self.folder / Path(name).name).write_bytes(data)

    def read(self, name: str) -> bytes:
        return self.read_many([name])[name]

    def write(self, name: str, data: bytes) -> None:
        self.write_many({name: data})


class DeviceSource:
    """Delta's own Saves folder, over USB.

    pymobiledevice3 11.x is **async throughout** -- ``list_devices``,
    ``create_using_usbmux``, ``get_apps``, and every AFC call. That is not
    visible from the outside: the AFC methods carry a ``@path_to_str()``
    decorator, so ``inspect.iscoroutinefunction`` reports them as ordinary
    functions, and the first version of this file called them as such. It
    failed with "'coroutine' object is not iterable" -- found by installing the
    library and running ``probe`` with nothing plugged in, which is precisely
    why it was installed before the cable was available.

    The async work is kept behind a synchronous surface, and each operation
    runs in **one** session: opening lockdown and vending the container per
    file would pay the whole handshake once per pak.

    Every failure gets its own sentence on purpose. The library, Apple's
    service, a cable, a trust pairing and Delta itself can each be the missing
    piece, and "connection failed" sends someone to a forum for an hour.
    """

    kind = "device"

    def __init__(self, bundle_id: str | None = None) -> None:
        self.bundle_id = bundle_id

    def describe(self) -> str:
        return f"device ({self.bundle_id or 'Delta'})"

    # -- the pieces, each with its own failure -----------------------------

    @staticmethod
    def library():
        """``pymobiledevice3``, or a sentence saying how to get it."""
        try:
            from pymobiledevice3 import usbmux
        except ImportError as error:
            raise SourceError(
                "pymobiledevice3 is not installed. This program was built "
                "without it, which should not happen in a release build - "
                "reinstall the Controller Pak download."
            ) from error
        return usbmux

    @classmethod
    async def _devices(cls) -> list:
        usbmux = cls.library()
        try:
            return list(await usbmux.list_devices())
        except ConnectionRefusedError as error:
            raise SourceError(
                "Apple's device service is not running. It comes with iTunes "
                "or the Apple Devices app - install one of those, then plug "
                "the phone in again."
            ) from error
        except SourceError:
            raise
        except Exception as error:
            raise SourceError(
                f"could not ask Apple's device service: {error}"
            ) from error

    @classmethod
    def devices(cls) -> list:
        """Every connected iPhone or iPad, or why there are none."""
        return _run(cls._devices())

    @classmethod
    async def _installed_apps(cls, lockdown=None) -> dict:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import (
            InstallationProxyService,
        )

        lockdown = lockdown or await create_using_usbmux()
        apps = await InstallationProxyService(lockdown=lockdown).get_apps(
            application_type="User"
        )
        return apps or {}

    @classmethod
    def installed_apps(cls) -> dict:
        """Every user app on the device, so Delta is found rather than assumed.

        Delta's bundle identifier differs between the App Store build and a
        sideloaded or AltStore one, and guessing wrong fails as "no such app"
        with nothing to go on. Asking is cheap.
        """
        return _run(cls._installed_apps())

    @classmethod
    def find_delta(cls, apps: dict) -> str | None:
        """Delta's bundle id, matched on the hints rather than hard-coded."""
        for bundle_id in apps:
            lowered = str(bundle_id).lower()
            if any(hint in lowered for hint in BUNDLE_HINTS):
                return bundle_id
        return None

    async def _session(self, work):
        """Open Delta's documents, run ``work(service)``, close it again.

        ``documents_only`` is what makes this need no jailbreak: it vends only
        the app's Documents subtree -- exactly where the paks are -- and leaves
        the rest of the container unreachable.
        """
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.house_arrest import HouseArrestService

        if not await self._devices():
            raise SourceError(
                "no iPhone or iPad is connected. Plug it in with a cable, unlock it, "
                "and answer Trust if it asks."
            )

        try:
            lockdown = await create_using_usbmux()
        except Exception as error:
            raise SourceError(
                f"could not talk to the phone: {error}. If it is asking you to "
                f"trust this computer, answer that first."
            ) from error

        bundle_id = self.bundle_id
        if not bundle_id:
            bundle_id = self.find_delta(await self._installed_apps(lockdown))
        if not bundle_id:
            raise SourceError(
                "Delta was not found on this device. If you sideloaded it, "
                "pass its bundle identifier with --bundle."
            )
        self.bundle_id = bundle_id

        try:
            service = await HouseArrestService.create(
                lockdown, bundle_id, documents_only=True
            )
        except Exception as error:
            raise SourceError(
                f"could not open {bundle_id}'s documents: {error}"
            ) from error

        try:
            return await work(service)
        finally:
            try:
                await service.close()
            except Exception:
                pass

    @staticmethod
    async def _saves_root(service) -> str:
        """Where the paks live, tried in measured-first order.

        ``VendDocuments`` does *not* root the session at the app's Documents
        folder, which is what this used to assume and what the flag's name
        implies. Measured on a real device: the vend is the container, with
        ``Documents`` as a subfolder. See ``DELTA_SAVES_CANDIDATES``.
        """
        from pymobiledevice3.exceptions import AfcException

        last: Exception | None = None
        for candidate in DELTA_SAVES_CANDIDATES:
            try:
                await service.listdir(candidate)
                return candidate
            except AfcException as error:
                last = error
        raise SourceError(
            f"could not open {DELTA_SAVES_PATH} inside Delta. If no N64 game "
            f"has ever been played in Delta, that folder does not exist yet. "
            f"({last})"
        )

    # -- the same surface FolderSource has ---------------------------------

    def listing(self) -> list[RemoteFile]:
        async def work(service) -> list[RemoteFile]:
            root = await self._saves_root(service)
            found: list[RemoteFile] = []
            for name in await service.listdir(root):
                if name in (".", ".."):
                    continue
                try:
                    info = await service.stat(f"{root}/{name}")
                    size = int(info.get("st_size", 0))
                except Exception:
                    size = 0
                found.append(RemoteFile(name, size))
            return sorted(found, key=lambda item: item.name.lower())

        return _run(self._session(work))

    def read_many(self, names: list[str]) -> dict[str, bytes]:
        async def work(service) -> dict[str, bytes]:
            root = await self._saves_root(service)
            found: dict[str, bytes] = {}
            for name in names:
                safe = Path(name).name
                try:
                    found[name] = await service.get_file_contents(f"{root}/{safe}")
                except Exception as error:
                    raise SourceError(
                        f"could not read {safe} from the phone: {error}"
                    ) from error
            return found

        return _run(self._session(work))

    def write_many(self, files: dict[str, bytes]) -> None:
        async def work(service) -> None:
            root = await self._saves_root(service)
            for name, data in files.items():
                safe = Path(name).name
                try:
                    await service.set_file_contents(f"{root}/{safe}", data)
                except Exception as error:
                    raise SourceError(
                        f"could not write {safe} to the phone: {error}"
                    ) from error

        _run(self._session(work))

    def read(self, name: str) -> bytes:
        return self.read_many([name])[name]

    def write(self, name: str, data: bytes) -> None:
        self.write_many({name: data})
