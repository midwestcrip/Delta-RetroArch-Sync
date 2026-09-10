"""The standalone window, for using this without the main program.

Kept deliberately small. Somebody running this on its own wants three
questions answered -- is everything plugged in, what is on the phone, and get
it onto my PC -- and the third is a folder copy. Merging into RetroArch's save
is the main program's job and is not duplicated here; this window says so and
names the folder it wrote to.

The same three commands the JSON interface exposes, so there is one
implementation of each and the window is a caller like any other.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__
from .sources import DeviceSource, FolderSource, SourceError

TITLE = "Delta-RetroArch Controller Pak"


class Window:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.folder: Path | None = None
        root.title(f"{TITLE} {__version__}")
        root.minsize(560, 380)

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=(
                "Delta does not sync Nintendo 64 Controller Paks, so they only "
                "travel over a cable. Plug the phone in, unlock it, and answer "
                "Trust if it asks."
            ),
            wraplength=520,
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="Check setup", command=self.on_probe).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(buttons, text="What is on the phone", command=self.on_list).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="Copy to this PC…", command=self.on_pull).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="Use a folder instead…", command=self.on_folder).pack(
            side="left", padx=6
        )

        self.log = scrolledtext.ScrolledText(frame, height=14, wrap="word")
        self.log.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.log.configure(state="disabled")

        self.say(
            "Press Check setup to see whether everything needed is in place."
        )

    # -- plumbing ----------------------------------------------------------

    def say(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.root.update_idletasks()

    def source(self):
        return FolderSource(self.folder) if self.folder else DeviceSource()

    # -- the buttons -------------------------------------------------------

    def on_folder(self) -> None:
        """Point at a copy of Delta's Saves folder taken off by hand.

        The escape hatch for no iTunes, no cable, or a phone that will not
        pair: Delta sets ``UIFileSharingEnabled``, so the folder is reachable
        in Explorer and everything after this point is identical.
        """
        chosen = filedialog.askdirectory(
            parent=self.root, title="Delta's Cores/Mupen64Plus/Saves folder"
        )
        if not chosen:
            return
        self.folder = Path(chosen)
        self.say(f"Reading a folder instead of a phone: {self.folder}")

    def on_probe(self) -> None:
        self.say("")
        self.say("--- Checking ---")
        if self.folder:
            self.say(f"Using a folder, so nothing else is needed: {self.folder}")
            return
        try:
            DeviceSource.library()
            self.say("  pymobiledevice3: installed")
            devices = DeviceSource.devices()
            self.say("  Apple device service: running")
        except SourceError as error:
            self.say(f"  {error}")
            return

        if not devices:
            self.say(
                "  iPhone: none connected. Plug one in, unlock it, and answer "
                "Trust if it asks."
            )
            return
        self.say(f"  iPhone: {len(devices)} connected")

        try:
            bundle = DeviceSource.find_delta(DeviceSource.installed_apps())
        except Exception as error:  # noqa: BLE001
            self.say(f"  Delta: could not list installed apps ({error})")
            return
        self.say(
            f"  Delta: found as {bundle}" if bundle else "  Delta: not installed"
        )

    def on_list(self) -> None:
        self.say("")
        self.say("--- What is there ---")
        try:
            files = self.source().listing()
        except SourceError as error:
            self.say(f"  {error}")
            return
        if not files:
            self.say("  Nothing. Delta writes these once an N64 game is played.")
            return
        for item in files:
            self.say(f"  {item.name}  ({item.size:,} B)")

    def on_pull(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, title="Where should the pak files go?"
        )
        if not chosen:
            return
        destination = Path(chosen)

        self.say("")
        self.say(f"--- Copying to {destination} ---")
        try:
            source = self.source()
            files = [
                item
                for item in source.listing()
                if item.name.lower().endswith((".mpk", ".mpk1", ".mpk2", ".mpk3", ".mpk4"))
            ]
            if not files:
                self.say("  No Controller Pak files to copy.")
                return
            destination.mkdir(parents=True, exist_ok=True)
            for item in files:
                (destination / item.name).write_bytes(source.read(item.name))
                self.say(f"  {item.name}  ({item.size:,} B)")
        except (SourceError, OSError) as error:
            self.say(f"  {error}")
            messagebox.showerror(TITLE, str(error), parent=self.root)
            return

        self.say("")
        self.say(
            "Done. Open Delta-RetroArch Synchronizer and use its Controller "
            "Pak tab to merge these into RetroArch's save — it takes a backup "
            "first and will not let a blank pak overwrite one with notes on it."
        )


def run_window() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover -- no display
        print(f"cannot open a window: {error}", file=sys.stderr)
        return 1
    Window(root)
    root.mainloop()
    return 0
