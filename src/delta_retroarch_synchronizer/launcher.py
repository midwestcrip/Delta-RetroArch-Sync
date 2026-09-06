"""A small Windows launcher window.

The point of the whole project is that playing on the desktop should be one
double-click, so this wraps RetroArch's lifecycle: sync, launch, wait, sync
again. Running it by hand from a terminal was never the deliverable.

Built on tkinter/ttk because it ships with Python and renders with native
Windows widgets -- no dependency for a tool that otherwise needs none.

Two things shape the design:

- **The work runs on a thread.** A sync hashes a 16MB ROM and may wait on
  Dropbox; doing that on the UI thread freezes the window, which looks exactly
  like a crash. Results come back through a queue that the UI polls.
- **The log is the main element, not a detail.** This tool moves save files
  around. When something is wrong you need to see what it did, and a progress
  bar that says "done" tells you nothing worth knowing.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import webbrowser
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import config as config_module
from . import discovery, dropbox_api, health
from . import inspect as inspect_module
from . import sync as sync_module

WINDOW_TITLE = "Delta-RetroArch Synchronizer"


def _state_dir() -> Path:
    return Path(__file__).resolve().parents[2]


class LauncherWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.busy = False

        self.config = config_module.load()
        self._fill_in_discovered_paths()

        root.title(WINDOW_TITLE)
        root.resizable(False, False)

        # Gives the window its own taskbar and title-bar icon instead of the
        # generic Python feather.
        icon = _state_dir() / "assets" / "synchronizer.ico"
        if icon.is_file():
            try:
                root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

        self.delta_var = tk.StringVar(value=str(self.config.delta_folder or ""))
        self.exe_var = tk.StringVar(value=str(self.config.retroarch_exe or ""))
        self.rom_var = tk.StringVar(value=str(self.config.retroarch_rom_dir or ""))
        self.push_var = tk.BooleanVar(value=self.config.push_enabled)
        self.roms_var = tk.BooleanVar(value=self.config.sync_roms)
        self.cheats_var = tk.BooleanVar(value=self.config.sync_cheats)
        self.open_sync_var = tk.BooleanVar(value=self.config.sync_on_open)

        self._build()
        self._report_startup()
        self.root.after(100, self._drain)

    # ---------------------------------------------------------------- layout

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.grid(sticky="nsew")

        self.log = tk.Text(outer, height=11, width=78, wrap="word")
        self.log.configure(state="disabled", relief="sunken", borderwidth=1)
        self.log.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        paths = ttk.LabelFrame(outer, text="Paths", padding=6)
        paths.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        paths.columnconfigure(1, weight=1)

        self._path_row(paths, 0, "Delta folder (Dropbox)", self.delta_var, directory=True)
        self._path_row(paths, 1, "RetroArch", self.exe_var, directory=False)
        self._path_row(paths, 2, "ROM folder", self.rom_var, directory=True)

        options = ttk.LabelFrame(outer, text="Sync options", padding=6)
        options.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        ttk.Checkbutton(
            options, text="Sync as soon as this window opens", variable=self.open_sync_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            options, text="Copy ROMs from Delta", variable=self.roms_var
        ).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(
            options, text="Export cheats as .cht files", variable=self.cheats_var
        ).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(
            options,
            text="Send desktop saves back to Delta (experimental)",
            variable=self.push_var,
            command=self._push_toggled,
        ).grid(row=3, column=0, sticky="w")

        dropbox_row = ttk.Frame(options)
        dropbox_row.grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.dropbox_label = ttk.Label(dropbox_row, text="", foreground="#666666")
        self.dropbox_label.grid(row=0, column=0, sticky="w")
        self.auth_button = ttk.Button(
            dropbox_row, text="Authorise Dropbox…", command=self.on_authorise
        )
        self.auth_button.grid(row=0, column=1, padx=(10, 0))
        ttk.Button(
            dropbox_row, text="Use own app key…", command=self.on_change_app_key
        ).grid(row=0, column=2, padx=(6, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="Check status", command=self.on_status).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(actions, text="Sync now", command=self.on_sync).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(actions, text="Save settings", command=self.on_save).grid(
            row=0, column=3, padx=4
        )

        self.play_button = ttk.Button(
            outer, text="Sync and Play", command=self.on_play, padding=8
        )
        self.play_button.grid(row=4, column=0, sticky="ew")

    def _path_row(
        self, parent: ttk.LabelFrame, row: int, label: str, var: tk.StringVar,
        *, directory: bool
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(parent, textvariable=var, width=52).grid(
            row=row, column=1, sticky="ew"
        )
        ttk.Button(
            parent,
            text="Browse…",
            command=lambda: self._browse(var, directory=directory),
        ).grid(row=row, column=2, padx=(6, 0))

    def _browse(self, var: tk.StringVar, *, directory: bool) -> None:
        current = var.get() or str(Path.home())
        if directory:
            chosen = filedialog.askdirectory(initialdir=current, parent=self.root)
        else:
            chosen = filedialog.askopenfilename(
                initialdir=str(Path(current).parent),
                filetypes=[("RetroArch", "retroarch.exe"), ("Programs", "*.exe")],
                parent=self.root,
            )
        if chosen:
            var.set(str(Path(chosen)))

    # ----------------------------------------------------------- discovery

    def _fill_in_discovered_paths(self) -> None:
        """Prefill anything the user has not configured.

        Discovery is good enough that a first run usually needs no setup at
        all; the fields exist for the installs it cannot guess.
        """
        if self.config.delta_folder is None:
            found = discovery.find_delta_folder()
            if found.path is not None:
                self.config = replace(self.config, delta_folder=found.path)

        if self.config.retroarch_config is None:
            found = discovery.find_retroarch_config()
            if found.path is not None:
                self.config = replace(self.config, retroarch_config=found.path)

        if self.config.retroarch_exe is None:
            exe = discovery.find_retroarch_exe(self.config.retroarch_config)
            if exe is not None:
                self.config = replace(self.config, retroarch_exe=exe)

        if self.config.retroarch_rom_dir is None and self.config.retroarch_config:
            self.config = replace(
                self.config,
                retroarch_rom_dir=self.config.retroarch_config.parent / "roms",
            )

    # ----------------------------------------------------------------- log

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _report_startup(self) -> None:
        self.write(f"{WINDOW_TITLE}")
        delta = self.config.delta_folder
        exe = self.config.retroarch_exe
        self.write(f"Delta folder : {delta or 'not found — set it below'}")
        self.write(f"RetroArch    : {exe or 'not found — set it below'}")
        self._refresh_dropbox_label()
        if delta and exe:
            self.write("Ready. Press Sync and Play.")
            if self.config.sync_on_open:
                # Pull straight away: by the time you have read this line the
                # desktop already has whatever you did on your phone.
                self.root.after(200, self.on_sync)
        else:
            self.write("Set the missing paths above, then Save settings.")

    def _refresh_dropbox_label(self) -> None:
        token = _state_dir() / dropbox_api.TOKEN_FILENAME
        own = " (your app)" if self.config.dropbox_app_key else ""
        if token.is_file():
            self.dropbox_label.configure(text=f"Dropbox authorised{own}")
        else:
            self.dropbox_label.configure(text=f"Dropbox not authorised{own}")

    def _push_toggled(self) -> None:
        if not self.push_var.get():
            return
        token = _state_dir() / dropbox_api.TOKEN_FILENAME
        if not token.is_file():
            messagebox.showinfo(
                WINDOW_TITLE,
                "Sending saves back to Delta needs Dropbox authorisation first.\n\n"
                "Press “Authorise Dropbox…” just below, then turn this on.",
                parent=self.root,
            )
            self.push_var.set(False)
            return
        messagebox.showwarning(
            WINDOW_TITLE,
            "Sending saves back to Delta is experimental.\n\n"
            "Delta assumes it is the only thing writing to its Dropbox folder, "
            "so a send can occasionally leave it needing a manual fix on your "
            "phone. Pulling from Delta is unaffected.\n\n"
            "Your saves are backed up before every write either way.",
            parent=self.root,
        )

    # -------------------------------------------------------------- actions

    def _current_config(self) -> config_module.Config:
        def path_or_none(value: str) -> Path | None:
            value = value.strip()
            return Path(value) if value else None

        return replace(
            self.config,
            delta_folder=path_or_none(self.delta_var.get()),
            retroarch_exe=path_or_none(self.exe_var.get()),
            retroarch_rom_dir=path_or_none(self.rom_var.get()),
            push_enabled=self.push_var.get(),
            sync_on_open=self.open_sync_var.get(),
            sync_roms=self.roms_var.get(),
            sync_cheats=self.cheats_var.get(),
        )

    def on_authorise(self) -> None:
        """Run the Dropbox PKCE flow from inside the window.

        Two steps with a browser in between, so it cannot be a single blocking
        call: open the consent page, then take the code the user pastes back.
        """
        app_key = self.config.dropbox_app_key or dropbox_api.DEFAULT_APP_KEY
        if not app_key:
            app_key = simpledialog.askstring(
                WINDOW_TITLE,
                "No Dropbox app key is configured.\n\n"
                "Create an app at dropbox.com/developers/apps (Scoped access, "
                "Full Dropbox, permission files.metadata.read) and paste its "
                "App key here.",
                parent=self.root,
            )
            if not app_key:
                return
            app_key = app_key.strip()
            self.config = replace(self.config, dropbox_app_key=app_key)
            config_module.save(self.config)

        verifier = dropbox_api.make_verifier()
        url = dropbox_api.build_authorize_url(app_key, verifier)
        webbrowser.open(url)
        self.write("Opened Dropbox in your browser. Approve access, then paste the code.")

        code = simpledialog.askstring(
            WINDOW_TITLE,
            "Paste the code Dropbox showed you:",
            parent=self.root,
        )
        if not code:
            self.write("Authorisation cancelled.")
            return

        try:
            credentials = dropbox_api.exchange_code(app_key, verifier, code.strip())
        except dropbox_api.DropboxError as error:
            self.write(f"Authorisation failed: {error}")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        credentials.save(_state_dir() / dropbox_api.TOKEN_FILENAME)
        self.write("Dropbox authorised. Sending saves back to Delta is now available.")
        self._refresh_dropbox_label()

    def on_change_app_key(self) -> None:
        """Swap the bundled Dropbox app registration for the user's own.

        For anyone who would rather not authorise against someone else's app, or
        who hits the 50-user limit the bundled one carries while it is in
        development status. Clearing the field restores the bundled key.
        """
        entered = simpledialog.askstring(
            WINDOW_TITLE,
            "Dropbox App key (leave blank to use the bundled one).\n\n"
            "docs/dropbox-app.md explains how to register your own.\n"
            "This is the App key, never the App secret.",
            initialvalue=self.config.dropbox_app_key,
            parent=self.root,
        )
        if entered is None:
            return

        self.config = replace(self.config, dropbox_app_key=entered.strip())
        config_module.save(self.config)
        which = "your own" if entered.strip() else "the bundled"
        self.write(f"Using {which} Dropbox app key. Authorise again to apply it.")
        self._refresh_dropbox_label()

    def on_save(self) -> None:
        self.config = self._current_config()
        path = config_module.save(self.config)
        self.write(f"Settings saved to {path.name}.")

    def on_status(self) -> None:
        self._run(self._status_work)

    def on_sync(self) -> None:
        self._run(self._sync_work)

    def on_play(self) -> None:
        self._run(self._play_work)

    def _run(self, work) -> None:
        """Run work on a thread so the window keeps repainting.

        Buttons are disabled for the duration rather than queueing: two syncs
        racing each other over the same save files is not a state worth
        supporting.
        """
        if self.busy:
            return
        self.busy = True
        self.play_button.configure(state="disabled")
        self.config = self._current_config()

        def runner() -> None:
            try:
                work()
            except Exception as error:  # surfaced in the log, never a silent stop
                self.messages.put(("log", f"ERROR: {error}"))
            finally:
                self.messages.put(("done", ""))

        threading.Thread(target=runner, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                kind, text = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.write(text)
            elif kind == "done":
                self.busy = False
                self.play_button.configure(state="normal")
        self.root.after(100, self._drain)

    # ----------------------------------------------------------------- work

    def _say(self, text: str) -> None:
        self.messages.put(("log", text))

    def _prepare(self) -> tuple[sync_module.Paths, list, str, bool, Path] | None:
        config = self.config
        if config.delta_folder is None or not config.delta_folder.is_dir():
            self._say("Delta folder not set or missing.")
            return None

        retroarch_config = config.retroarch_config
        if retroarch_config is None or not retroarch_config.is_file():
            self._say("retroarch.cfg not found. Set the RetroArch path.")
            return None

        settings = discovery.parse_retroarch_config(retroarch_config)
        save_dir = config.retroarch_save_dir or discovery.resolve_retroarch_dir(
            settings, "savefile_directory", retroarch_config, "saves"
        )
        cheat_dir = discovery.resolve_retroarch_dir(
            settings, "cheat_database_path", retroarch_config, "cheats"
        )
        sorted_by_core = discovery.truthy(settings, "sort_savefiles_enable")
        installed = discovery.installed_cores(retroarch_config, settings)

        entries = inspect_module.collect_games(config.delta_folder)
        if not entries:
            self._say("Delta has not synced any games yet.")
            return None

        paths = sync_module.Paths(
            delta_folder=config.delta_folder,
            retroarch_config=retroarch_config,
            save_dir=save_dir,
            state_dir=_state_dir(),
        )
        return paths, entries, installed, sorted_by_core, cheat_dir

    def _status_work(self) -> None:
        prepared = self._prepare()
        if prepared is None:
            return
        _, entries, installed, _, _ = prepared
        self._say(f"{len(entries)} game(s) in Delta:")
        for entry in entries:
            mark = " " if entry.supported else "!"
            self._say(f"  {mark} {entry.name} [{entry.system_label}]")
        self._say(f"Installed cores: {', '.join(sorted(installed)) or 'none'}")

        # Only meaningful once something has been pushed, but cheap, and it is
        # the check that would have surfaced the 2026-09-05 breakage instead of
        # leaving it to be found by hand.
        credentials = dropbox_api.Credentials.load(
            _state_dir() / dropbox_api.TOKEN_FILENAME
        )
        client = dropbox_api.DropboxClient(credentials) if credentials else None
        for entry in entries:
            if entry.save_path is None:
                continue
            report = health.check_game(
                self.config.delta_folder, entry.identifier, client
            )
            if report.ok:
                self._say(f"  {entry.name}: sync state healthy")
            else:
                for problem in report.problems:
                    self._say(f"  {entry.name}: {problem.name} — {problem.detail}")

    def _sync_once(self, label: str) -> None:
        prepared = self._prepare()
        if prepared is None:
            return
        paths, entries, installed, sorted_by_core, cheat_dir = prepared
        config = self.config

        dropbox = None
        if config.push_enabled:
            credentials = dropbox_api.Credentials.load(
                _state_dir() / dropbox_api.TOKEN_FILENAME
            )
            if credentials is not None:
                dropbox = dropbox_api.DropboxClient(credentials)

        cheats_by_game = (
            inspect_module.collect_cheats(config.delta_folder)
            if config.sync_cheats
            else None
        )

        self._say(f"--- {label} ---")
        for entry in entries:
            if entry.system is None:
                continue
            core = next(
                (name for name in entry.system.retroarch_cores if name in installed),
                None,
            )
            if entry.supported and core is None:
                self._say(f"  {entry.name}: no core installed for {entry.system.name}")
                continue

            report = sync_module.run_sync(
                paths,
                [entry],
                core or "",
                sorted_by_core,
                allow_push=config.push_enabled,
                rom_dir=config.retroarch_rom_dir if config.sync_roms else None,
                dropbox=dropbox,
                cheat_dir=cheat_dir if config.sync_cheats else None,
                cheats_by_game=cheats_by_game,
            )
            for outcome in report.outcomes:
                if outcome.action is sync_module.Action.NOTHING:
                    continue
                self._say(f"  {outcome.game}: {outcome.action.value}")
                self._say(f"      {outcome.detail}")
            if not any(o.action is not sync_module.Action.NOTHING for o in report.outcomes):
                self._say(f"  {entry.name}: already up to date")

    def _sync_work(self) -> None:
        self._sync_once("Sync")

    def _play_work(self) -> None:
        exe = self.config.retroarch_exe
        if exe is None or not exe.is_file():
            self._say("RetroArch executable not found. Set the path above.")
            return

        self._sync_once("Sync before playing")

        self._say("Launching RetroArch…")
        try:
            process = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except OSError as error:
            self._say(f"Could not launch RetroArch: {error}")
            return

        process.wait()
        self._say(f"RetroArch closed (exit code {process.returncode}).")

        # Sync after the process exits, not on a timer: mGBA flushes its save on
        # clean exit, so syncing earlier would copy a stale file.
        self._sync_once("Sync after playing")
        self._say("Done.")


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    LauncherWindow(root)
    root.mainloop()
    return 0
