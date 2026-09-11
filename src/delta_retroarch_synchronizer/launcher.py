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

import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Any

from . import config as config_module
from . import delta_writer, discovery, display, dropbox_api, guide, health, naming, paths
from . import addons, n64, paks, processes, restore, savestate
from . import tray as tray_module
from . import shortcut as shortcut_module
from . import theme
from . import emulators as emulators_module
from . import inspect as inspect_module
from . import manifest as manifest_module
from . import sync as sync_module
from . import systems

WINDOW_TITLE = "Delta-RetroArch Synchronizer"

#: Where a recovered battery save goes when the user does not choose. Never
#: Delta's folder: that would put a new file into another app's storage and
#: Dropbox would sync it to every device.
RECOVERED_DIRNAME = "recovered"


def _state_dir() -> Path:
    return paths.state_dir()


def resolve_paths(
    config: config_module.Config,
) -> tuple[config_module.Config, list[str]]:
    """Fill in anything the user has not configured, and say what is still missing.

    Discovery is good enough that a first run usually needs no setup at all; the
    fields in the window exist for the installs it cannot guess.

    When it cannot guess, the Discovery it returns carries a ``detail`` that
    explains why in terms the person at the window can act on -- which Dropbox
    root was actually looked in, or that Delta has not finished a first sync.
    Those are returned alongside the config and shown at startup. Discarding
    them is what made a first run report only that a path was missing, which
    both naive-user tests landed on as the worst thing about the tool: the CLI
    has always printed these, and the window is what people actually run.

    Kept out of the window class so it can be tested without a Tk display.
    """
    notes: list[str] = []

    if config.delta_folder is None:
        found = discovery.find_delta_folder()
        if found.path is not None:
            config = replace(config, delta_folder=found.path)
        elif found.detail:
            notes.append(found.detail)

    if config.retroarch_config is None:
        found = discovery.find_retroarch_config()
        if found.path is not None:
            config = replace(config, retroarch_config=found.path)
        elif found.detail:
            notes.append(found.detail)

    if config.retroarch_exe is None:
        exe = discovery.find_retroarch_exe(config.retroarch_config)
        if exe is not None:
            config = replace(config, retroarch_exe=exe)

    if config.retroarch_rom_dir is None and config.retroarch_config:
        config = replace(
            config,
            retroarch_rom_dir=config.retroarch_config.parent / "roms",
        )

    return config, notes


def summarise_changes(applied: list["sync_module.Outcome"]) -> str:
    """The headline for the end-of-sync summary."""
    count = len(applied)
    return f"{count} change{'' if count == 1 else 's'}:"


def change_lines(applied: list["sync_module.Outcome"]) -> list[str]:
    """One short line per thing that actually changed.

    Deliberately not the full detail again -- that is already above, and
    repeating a path that wraps over three lines would recreate the problem this
    summary exists to solve. Direction is the useful part: what someone wants
    from the bottom of the log is "did my thing reach the other side".
    """
    lines = []
    for outcome in applied:
        if outcome.action is sync_module.Action.PUSH:
            direction = "sent to Delta"
        elif outcome.action is sync_module.Action.PULL:
            direction = f"brought to {outcome.target}"
        else:
            direction = outcome.description
        lines.append(f"{outcome.game} — {direction}")
    return lines


def sync_emulators(
    paths: "sync_module.Paths",
    entries: list,
    config: "config_module.Config",
    dropbox: "dropbox_api.DropboxClient | None",
) -> list["sync_module.Outcome"]:
    """Reconcile every enabled standalone emulator, in one manifest pass.

    Module level, like ``backup_row``, so the window's sync loop stays readable
    and so what it does can be checked without a Tk display.

    One manifest for the whole pass: each emulator keeps its own agreed
    state inside it, so loading and saving per emulator would be the same
    result with several times the I/O.
    """
    chosen = set(config.emulators_enabled)
    if not chosen:
        return []

    extra = tuple(
        raw.parent if raw.is_file() else raw
        for raw in config.emulator_paths.values()
    )
    outcomes: list[sync_module.Outcome] = []
    state = manifest_module.Manifest.load(paths.manifest_path)
    for installed in emulators_module.find_installed(extra):
        if installed.emulator.key not in chosen:
            continue
        for entry in entries:
            if entry.system is None or not installed.emulator.handles(
                entry.system.key
            ):
                continue
            outcomes.extend(
                sync_module.sync_emulator(
                    paths,
                    entry,
                    installed,
                    rom_dir=config.retroarch_rom_dir,
                    override=config.emulator_save_dirs.get(installed.emulator.key),
                    allow_push=config.push_enabled,
                    dropbox=dropbox,
                    state=state,
                )
            )
    state.save()
    return outcomes


def backup_row(point: "restore.RestorePoint") -> tuple[str, str, str, str]:
    """One row of the Backups table.

    Module level, like ``resolve_paths``, so what the table actually says can be
    tested without a Tk display -- the window itself needs one, and the wording
    is the part worth pinning.
    """
    return (
        point.label,
        f"{point.backup.side} {point.backup.kind}",
        point.backup.when,
        f"{point.backup.size:,} B",
    )


class Confirmation:
    """Reports success on the button that was pressed.

    The third naive-user test found the gap: pressing "Save settings" on the
    Settings tab wrote "Settings saved to config.toml" to the log, which lives
    on the *Sync* tab. From where the user was standing the button did nothing
    at all. The log entry is still written -- it is the running record -- but
    the button now answers for itself.
    """

    #: Long enough to read without watching for it, short enough that the
    #: button is back to its own name before anyone reaches for it again.
    MILLISECONDS = 1800

    def __init__(self, button: ttk.Button) -> None:
        self.button = button
        self.resting_text = str(button.cget("text"))
        self.resting_style = str(button.cget("style")) or "TButton"
        self._pending: str | None = None

    def show(self, text: str, *, style: str = "Success.TButton") -> None:
        # Cancel first: a second press during the flash would otherwise let the
        # first timer fire and restore over the second message.
        self._cancel()
        self.button.configure(text=text, style=style)
        self._pending = self.button.after(self.MILLISECONDS, self._restore)

    def settle(self, text: str) -> None:
        """Rename the button itself, without interrupting a flash in progress.

        The Start menu button's own name alternates between adding and
        removing, so its resting label is not fixed at construction the way
        "Save settings" is.
        """
        self.resting_text = text
        if self._pending is None:
            self.button.configure(text=text, style=self.resting_style)

    def _restore(self) -> None:
        self._pending = None
        try:
            self.button.configure(text=self.resting_text, style=self.resting_style)
        except tk.TclError:  # the window closed while the flash was pending
            pass

    def _cancel(self) -> None:
        if self._pending is None:
            return
        try:
            self.button.after_cancel(self._pending)
        except tk.TclError:
            pass
        self._pending = None


class Tooltip:
    """A hover description for one widget.

    A checkbox label has room for a setting's name and not its reason, and the
    reasons here are not guessable: "Export cheats" does not tell you that they
    travel one way only, and "Send desktop saves back" does not tell you it is
    the one operation that can leave Delta needing manual repair. Both naive-user
    tests said the app explains nothing, and this is the cheapest place to fix
    that -- the text is there when wanted and invisible when not.
    """

    DELAY_MS = 450
    #: A 96-DPI width, so it goes through display.px like every other pixel
    #: dimension -- unscaled, a tooltip on a 150% display wraps at two thirds
    #: the intended line length and comes out a tall narrow ribbon.
    WRAP_PIXELS = 340

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        self.after_id: str | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        # A click means they have decided; the explanation is only in the way.
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _show(self) -> None:
        if self.tip is not None:
            return
        try:
            x = self.widget.winfo_pointerx() + display.px(14)
            y = self.widget.winfo_pointery() + display.px(20)
            screen_w = self.widget.winfo_screenwidth()
            screen_h = self.widget.winfo_screenheight()
        except tk.TclError:
            return

        palette = theme.current()
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        # Placed off-screen first so the unpositioned window never flashes in
        # the corner while its size is being measured.
        tip.wm_geometry("+-2000+-2000")
        tk.Label(
            tip,
            text=self.text,
            justify="left",
            wraplength=display.px(self.WRAP_PIXELS),
            background=palette.tip_bg,
            foreground=palette.tip_fg,
            relief="solid",
            borderwidth=1,
            padx=display.px(8),
            pady=display.px(6),
        ).pack()

        # A tooltip opened near the right or bottom edge would otherwise be cut
        # off by the screen, which is where the longest descriptions live.
        tip.update_idletasks()
        width = tip.winfo_reqwidth()
        height = tip.winfo_reqheight()
        margin = display.px(8)
        x = min(x, screen_w - width - margin)
        if y + height > screen_h - margin:
            y = self.widget.winfo_pointery() - height - display.px(12)
        tip.wm_geometry(f"+{max(margin, x)}+{max(margin, y)}")
        self.tip = tip

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


#: Written to be read by someone who has never used this before. Each says what
#: the setting does and, where it matters, why it is set the way it is.
SETTING_HELP: dict[str, str] = {
    "delta_folder": (
        "The 'Delta Emulator' folder inside your Dropbox, which is where Delta "
        "keeps its synced saves, games and cheats.\n\n"
        "Found automatically once the Dropbox desktop app is installed and "
        "signed in to the same account Delta syncs to."
    ),
    "retroarch_exe": (
        "Path to retroarch.exe.\n\n"
        "Used to launch RetroArch when you press Sync and Play, and to locate "
        "its save and cheat folders by reading the retroarch.cfg beside it."
    ),
    "rom_dir": (
        "Where games copied out of Delta are written, so RetroArch has "
        "something to load.\n\n"
        "Defaults to a 'roms' folder next to retroarch.cfg."
    ),
    "sync_on_open": (
        "Pull from Delta the moment this window opens, so your phone's progress "
        "is already on the desktop before you press anything.\n\n"
        "Turn this off if you would rather sync only when you ask."
    ),
    "sync_roms": (
        "Copy game files out of Delta's folder into your ROM folder, so a game "
        "you added on your phone simply appears on the desktop ready to play.\n\n"
        "Turn this off if you manage your own ROM library and would rather this "
        "left it alone."
    ),
    "sync_cheats": (
        "Write Delta's cheats out as RetroArch .cht files.\n\n"
        "One direction only, permanently: a cheat made in RetroArch cannot "
        "travel back, because Delta's records need metadata that only Delta's "
        "own app is allowed to write. Make cheats on your phone."
    ),
    "push_enabled": (
        "Let desktop progress reach your phone.\n\n"
        "Off by default, and the only part of this tool that can leave Delta "
        "needing manual repair: Delta assumes it is the only thing writing to "
        "its Dropbox folder. Needs Dropbox authorisation first, because the "
        "record has to name the exact file revision Dropbox holds.\n\n"
        "Your saves are backed up before every write either way, and the last "
        "ten versions are kept."
    ),
}


class LauncherWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.busy = False

        self.config = config_module.load()
        self.discovery_notes: list[str] = []
        #: Shown only while RetroArch has the screen. Clicking it, or
        #: RetroArch closing, brings the window back.
        self.tray = tray_module.TrayIcon(
            paths.resource_dir() / "assets" / "synchronizer.ico",
            f"{WINDOW_TITLE} — RetroArch is running",
            self._come_back,
        )
        #: True while the window is hidden rather than merely minimised, so the
        #: two ways back (the icon, and RetroArch exiting) do not fight.
        self.in_tray = False

        #: The setup window, while it is open. One at a time.
        self.instructions: tk.Toplevel | None = None
        self.instructions_text: tk.Text | None = None
        self.backup_points: list[restore.RestorePoint] = []
        self._fill_in_discovered_paths()

        root.title(WINDOW_TITLE)
        root.resizable(False, False)

        # Before any widget is built: everything below is sized from this.
        display.adopt(root)

        # Gives the window its own taskbar and title-bar icon instead of the
        # generic Python feather.
        #
        # Two calls, not one. iconbitmap is what Tk offers and what the title
        # bar and Alt-Tab read, but it hands Windows the whole .ico and lets it
        # choose, which on a scaled display means taking the 32-pixel entry and
        # enlarging it to the 48 the taskbar wants. set_window_icon then asks
        # for each size explicitly, so the matching entry is used and nothing is
        # stretched.
        icon = paths.resource_dir() / "assets" / "synchronizer.ico"
        if icon.is_file():
            try:
                root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
            root.update_idletasks()  # the window must exist to receive WM_SETICON
            display.set_window_icon(root, icon)

        self.delta_var = tk.StringVar(value=str(self.config.delta_folder or ""))
        self.exe_var = tk.StringVar(value=str(self.config.retroarch_exe or ""))
        self.rom_var = tk.StringVar(value=str(self.config.retroarch_rom_dir or ""))
        self.push_var = tk.BooleanVar(value=self.config.push_enabled)
        self.roms_var = tk.BooleanVar(value=self.config.sync_roms)
        self.cheats_var = tk.BooleanVar(value=self.config.sync_cheats)
        self.open_sync_var = tk.BooleanVar(value=self.config.sync_on_open)

        self._build()

        # Applied after the widgets exist, then rechecked on a timer so that
        # changing the OS theme restyles this window without restarting it.
        self.appearance = ""
        self._follow_system_appearance()

        self._report_startup()
        self.root.after(100, self._drain)
        # After the window is on screen, so the question arrives over something
        # that already explains what this program is.
        self.root.after(400, self._offer_start_menu)

    # ---------------------------------------------------------------- layout

    # ---------------------------------------------------------------- theme

    def _follow_system_appearance(self) -> None:
        """Restyle when the OS light/dark setting changes, then check again.

        Polled rather than event-driven because Tk has no notification for it
        and the Windows read is a single registry lookup. Nothing is redrawn
        unless the value actually changed.
        """
        current = theme.system_appearance()
        if current != self.appearance:
            self.appearance = current
            palette = theme.palette_for(current)
            theme.apply(self.root, palette)
            theme.apply_to_log(self.log, palette)
            self._style_instructions()
        self.root.after(theme.POLL_MS, self._follow_system_appearance)

    # --------------------------------------------------------------- layout

    def _build(self) -> None:
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=display.px(8))
        outer.grid(sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        # Sync and Play stays outside the tabs. It is the reason the program
        # exists, and burying the primary action behind a tab someone might be
        # sitting on is how a tool acquires a reputation for being confusing.
        notebook = self.tabs = ttk.Notebook(outer)
        notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        sync_tab = ttk.Frame(notebook, padding=display.px(8))
        backups_tab = ttk.Frame(notebook, padding=display.px(8))
        states_tab = ttk.Frame(notebook, padding=display.px(8))
        paks_tab = ttk.Frame(notebook, padding=display.px(8))
        settings_tab = ttk.Frame(notebook, padding=display.px(8))
        notebook.add(sync_tab, text="   Sync   ")
        notebook.add(backups_tab, text="   Backups   ")
        notebook.add(states_tab, text="   Save states   ")
        notebook.add(paks_tab, text="   Controller Paks   ")
        notebook.add(settings_tab, text="   Settings   ")

        self._build_sync_tab(sync_tab)
        self._build_backups_tab(backups_tab)
        self._build_states_tab(states_tab)
        self._build_paks_tab(paks_tab)
        self._build_settings_tab(settings_tab)

        self.play_button = ttk.Button(
            outer, text="Sync and Play", command=self.on_play,
            style="Primary.TButton",
        )
        self.play_button.grid(row=1, column=0, sticky="ew")

    def _build_sync_tab(self, parent: ttk.Frame) -> None:
        # A notebook is as tall as its tallest tab, so without this the log
        # keeps its requested height and leaves dead space under the buttons
        # whenever the Settings tab is the taller of the two.
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self.log = tk.Text(
            parent, height=14, width=82, wrap="word",
            font=("Consolas", 10), padx=display.px(8), pady=display.px(6),
        )
        self.log.configure(state="disabled", relief="flat")
        self.log.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        # Bottom left, under the log, where the third naive-user test asked for
        # it. Away from the three buttons that do something, because this one
        # only explains.
        instructions_button = ttk.Button(
            actions, text="Instructions", command=self.on_instructions
        )
        instructions_button.grid(row=0, column=0, sticky="w")
        Tooltip(
            instructions_button,
            "Everything needed to set this up, in order, with each step marked "
            "according to whether this PC has it already.",
        )

        ttk.Button(actions, text="Check status", command=self.on_status).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(actions, text="Sync now", command=self.on_sync).grid(
            row=0, column=2, padx=4
        )

    def _build_backups_tab(self, parent: ttk.Frame) -> None:
        """Somewhere to see the rolling backups and put one back.

        A list rather than log lines, because choosing a version is a comparison
        between rows -- which one, from when, how big -- and that is what a table
        is for. The log is the right shape for what happened; this is the right
        shape for what could happen.
        """
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        ttk.Label(
            parent,
            text=(
                "A copy is kept before anything is overwritten. Whatever you "
                "restore is itself backed up first, so this is undoable."
            ),
            style="Muted.TLabel",
            wraplength=display.px(620),
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        columns = ("game", "what", "when", "size")
        self.backup_list = ttk.Treeview(
            parent, columns=columns, show="headings", height=11, selectmode="browse"
        )
        for key, title, anchor in (
            ("game", "Game", "w"),
            ("what", "What", "w"),
            ("when", "When", "w"),
            ("size", "Size", "e"),
        ):
            self.backup_list.heading(key, text=title)
            self.backup_list.column(key, anchor=anchor, stretch=(key == "game"))
        self.backup_list.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            parent, orient="vertical", command=self.backup_list.yview
        )
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.backup_list.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="Refresh", command=self.on_refresh_backups).grid(
            row=0, column=1, padx=4
        )
        restore_button = ttk.Button(
            actions, text="Restore selected…", command=self.on_restore_selected
        )
        restore_button.grid(row=0, column=2, padx=4)
        Tooltip(
            restore_button,
            "Puts the selected version back, after showing you exactly which "
            "file it would overwrite.\n\n"
            "The next sync then carries it to the other side, or reports a "
            "conflict if that side also changed.",
        )

        self.on_refresh_backups()

    def _build_states_tab(self, parent: ttk.Frame) -> None:
        """Recovering a battery save out of a save state, without typing a path.

        A list rather than a file picker, for the same reason Backups is a list:
        the states are already known, their filenames are UUIDs nobody can read,
        and picking one is a comparison between rows. It also answers "which of
        these can I even do anything with" at a glance, which a file dialog
        cannot.
        """
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        ttk.Label(
            parent,
            text=(
                "Save states do not sync between Delta and RetroArch, but the "
                "battery save inside one can be pulled out — useful when a "
                "state is the only copy of your progress left. Every system "
                "but N64 can be read, and an N64 state genuinely holds no "
                "battery save to read. Recover writes it to a file you pick; "
                "Install puts it into RetroArch, backing up what it replaces."
            ),
            style="Muted.TLabel",
            wraplength=display.px(620),
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        columns = ("game", "slot", "system", "format", "size")
        self.state_list = ttk.Treeview(
            parent, columns=columns, show="headings", height=11, selectmode="browse"
        )
        for key, title, anchor in (
            ("game", "Game", "w"),
            ("slot", "Slot", "w"),
            ("system", "System", "w"),
            ("format", "Format", "w"),
            ("size", "Size", "e"),
        ):
            self.state_list.heading(key, text=title)
            self.state_list.column(key, anchor=anchor, stretch=(key == "game"))
        self.state_list.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            parent, orient="vertical", command=self.state_list.yview
        )
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.state_list.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="Refresh", command=self.on_refresh_states).grid(
            row=0, column=1, padx=4
        )
        recover_button = ttk.Button(
            actions, text="Recover save…", command=self.on_recover_state
        )
        recover_button.grid(row=0, column=2, padx=4)
        self.recover_confirmation = Confirmation(recover_button)
        Tooltip(
            recover_button,
            "Writes the battery save from the selected state to a file you "
            "choose.\n\n"
            "Nothing else is touched — not Delta's folder, not RetroArch's "
            "saves, not the manifest. When the state came from Delta, the "
            "result is checked against Delta's own save for that game.",
        )

        install_button = ttk.Button(
            actions, text="Install into RetroArch…", command=self.on_install_state
        )
        install_button.grid(row=0, column=3, padx=4)
        self.install_confirmation = Confirmation(install_button)
        Tooltip(
            install_button,
            "The same save, written straight over RetroArch's own save for "
            "that game.\n\n"
            "What it replaces is copied into the rolling backups first, so it "
            "is one button away on the Backups tab. Shows you the exact file "
            "and both sizes before writing anything, and refuses while "
            "RetroArch is running — RetroArch writes the save when it closes, "
            "which would undo this.\n\n"
            "Delta's folder is not touched. The next sync will carry the new "
            "save back to Delta, or report a conflict if Delta moved too.",
        )

        self.on_refresh_states()

    def on_refresh_states(self) -> None:
        """Re-scan Delta's folder for synced save states."""
        self.config = self._current_config()
        self.found_states: list[savestate.FoundState] = []

        self.state_list.delete(*self.state_list.get_children())

        folder = self.config.delta_folder
        if folder is None or not folder.is_dir():
            self.state_list.insert(
                "", "end", values=("Delta's folder not found", "", "", "", "")
            )
            return

        try:
            self.found_states = savestate.find_states(folder)
        except OSError as error:
            self.state_list.insert("", "end", values=(str(error), "", "", "", ""))
            return

        if not self.found_states:
            self.state_list.insert(
                "",
                "end",
                values=(
                    "No save states synced yet",
                    "",
                    "",
                    "make a manual one in Delta",
                    "",
                ),
            )
            return

        for index, found in enumerate(self.found_states):
            self.state_list.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    found.game_name or found.path.name,
                    found.slot_name or "",
                    found.system or "unknown",
                    found.describe_format(),
                    f"{found.size:,} B",
                ),
            )

    def _selected_state(self) -> "savestate.FoundState | None":
        selection = self.state_list.selection()
        if not selection:
            return None
        try:
            return self.found_states[int(selection[0])]
        except (ValueError, IndexError, AttributeError):
            return None

    def _extract_selected_state(
        self,
    ) -> "tuple[savestate.FoundState, savestate.ExtractedSave] | None":
        """The selected state's battery save, or ``None`` having said why not.

        Shared by both buttons on this tab. Everything up to holding the bytes
        is identical whether they are going to a file of the user's choosing or
        into RetroArch, and the three refusals below are the ones worth
        explaining rather than repeating.
        """
        found = self._selected_state()
        if found is None:
            messagebox.showinfo(
                WINDOW_TITLE, "Pick a save state from the list first.",
                parent=self.root,
            )
            return None

        if not found.recoverable and (
            found.delta_core in savestate.CORES_WITHOUT_SAVES_IN_STATES
        ):
            messagebox.showinfo(
                WINDOW_TITLE,
                f"There is no battery save inside an N64 save state to "
                f"recover.\n\n{found.delta_core} keeps the cartridge's save "
                "in separate files and writes only the flash controller's "
                "registers into a state. This is not something that can be "
                "added later.",
                parent=self.root,
            )
            return None

        if not found.recoverable:
            core = found.delta_core or "that system's emulator"
            readable = ", ".join(sorted(savestate.READABLE_CORES))
            messagebox.showinfo(
                WINDOW_TITLE,
                f"That state is in {core}'s own save state format, and this "
                f"tool cannot read that one.\n\nThe formats it can read are: "
                f"{readable}. Every emulator writes its own, so each is a "
                f"separate piece of work.",
                parent=self.root,
            )
            return None

        try:
            extracted = savestate.extract_battery_save(
                found.path.read_bytes(),
                expected_size=savestate.delta_save_size(found.path),
            )
        except (OSError, savestate.SaveStateError) as error:
            self._say(f"Could not read that save state: {error}", "error")
            messagebox.showerror(
                WINDOW_TITLE,
                f"Could not read that save state.\n\n{error}",
                parent=self.root,
            )
            return None

        return found, extracted

    def on_recover_state(self) -> None:
        """Extract the selected state's battery save to a file the user picks.

        Deliberately not on the worker thread: this reads one file and writes
        one file, touching neither side of the sync, so there is nothing for it
        to race with and nothing to leave half-done.
        """
        pair = self._extract_selected_state()
        if pair is None:
            return
        found, extracted = pair

        suggested = savestate.suggested_output(
            found.path, _state_dir() / RECOVERED_DIRNAME
        )
        try:
            suggested.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        chosen = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save the recovered battery save",
            initialfile=suggested.name,
            initialdir=str(suggested.parent),
            defaultextension=".sav",
            filetypes=[("Battery save", "*.sav"), ("RetroArch save", "*.srm"),
                       ("All files", "*.*")],
        )
        if not chosen:
            return

        destination = Path(chosen)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(extracted.data)
        except OSError as error:
            self._say(f"Could not write {destination}: {error}", "error")
            messagebox.showerror(
                WINDOW_TITLE, f"Could not write that file.\n\n{error}",
                parent=self.root,
            )
            return

        label = found.game_name or found.path.name
        self._say(
            f"Recovered {extracted.size:,} B ({extracted.save_type}) from "
            f"{label} to {destination}",
            "heading",
        )

        comparison = savestate.compare_with_delta(found.path, extracted.data)
        if comparison is None:
            self._say(
                "No cross-check: Delta has no battery save for that game to "
                "compare against.",
                "warn",
            )
            self.recover_confirmation.show("Recovered")
            return

        self._say(comparison.describe(), "" if comparison.identical else "warn")
        if comparison.identical:
            self.recover_confirmation.show("Matches Delta")
        else:
            messagebox.showwarning(
                WINDOW_TITLE,
                "The recovered save was written, but it does not match Delta's "
                "own save for this game byte for byte.\n\n"
                f"{comparison.describe()}\n\nThe Sync tab has the details.",
                parent=self.root,
            )

    def on_install_state(self) -> None:
        """Put the recovered save straight into RetroArch, backing up first.

        The manual alternative -- recover to a file, then copy it over
        RetroArch's save in Explorer -- was the one step in this flow with no
        backup behind it, performed on the file holding the progress being
        rescued. Here the previous save goes into the rolling backups first, so
        it is one button away on the Backups tab.
        """
        pair = self._extract_selected_state()
        if pair is None:
            return
        found, extracted = pair

        # Before anything is written: RetroArch dumps the loaded game's SRAM
        # when it closes, so a save installed underneath a running copy is
        # overwritten the moment the user quits -- silently, and looking for
        # all the world like the recovery failed.
        exe = self.config.retroarch_exe
        if exe is not None and processes.find_by_name(exe.name):
            messagebox.showwarning(
                WINDOW_TITLE,
                "RetroArch is running, so this would not stick.\n\nIt writes "
                "the loaded game's save when it closes, straight over anything "
                "installed underneath it. Close RetroArch and try again.",
                parent=self.root,
            )
            return

        prepared = self._prepare()
        if prepared is None:
            messagebox.showerror(
                WINDOW_TITLE,
                "RetroArch's settings could not be read, so there is no way to "
                "know where it keeps its saves.\n\nThe Sync tab has the "
                "details. Recover save… still writes to a file you pick.",
                parent=self.root,
            )
            return
        paths, entries, installed, sorted_by_core, _cheat_dir, _playlist_dir = prepared

        entry = next(
            (e for e in entries if e.identifier == found.game_identifier), None
        )
        if entry is None or entry.system is None:
            messagebox.showerror(
                WINDOW_TITLE,
                "That save state's game is not among the games Delta has "
                "synced, so there is nothing to say what RetroArch would call "
                "its save.",
                parent=self.root,
            )
            return

        core = next(
            (name for name in entry.system.retroarch_cores if name in installed), ""
        )

        try:
            plan = savestate.plan_install(
                paths.save_dir,
                entry.name,
                entry.system,
                core_name=core,
                sorted_by_core=sorted_by_core,
            )
        except savestate.InstallError as error:
            self._say(f"Cannot install that save: {error}", "error")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        if not self._confirm_install(found, extracted, plan):
            return

        try:
            result = savestate.install_save(
                extracted.data, plan, paths.backup_dir
            )
        except savestate.InstallError as error:
            self._say(f"Could not install that save: {error}", "error")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        self._say(result.describe(), "heading")
        if result.backup is not None:
            self._say(
                "  The save it replaced is on the Backups tab if you want it "
                "back.",
                "muted",
            )
        self._say(
            f"  {restore.RESTORE_IS_A_CHANGE.capitalize()}.", "muted"
        )
        self.install_confirmation.show("Installed")

    def _confirm_install(
        self,
        found: "savestate.FoundState",
        extracted: "savestate.ExtractedSave",
        plan: "savestate.InstallPlan",
    ) -> bool:
        """Ask before overwriting, showing what is actually about to happen.

        Spelled out rather than summarised, because the two things that make
        this go wrong are both visible here and nowhere else: a target that was
        worked out rather than found, and a size that does not match what is
        already there.
        """
        label = found.game_name or found.path.name
        lines = [
            f"Install the battery save recovered from {label} into RetroArch?",
            "",
            f"    From : {found.slot_name or 'save state'} "
            f"({extracted.core}, {extracted.size:,} B)",
            f"    To   : {plan.target}",
        ]

        if plan.replaces_a_save:
            assert plan.existing_size is not None
            lines.append(
                f"    Now  : {plan.existing_size:,} B, which will be backed up "
                f"first"
            )
            if not plan.size_matches(extracted.size):
                lines += [
                    "",
                    "The save already there is a different size. That usually "
                    "means the name matched a different game, so check the path "
                    "above before going ahead.",
                ]
        else:
            lines.append("    Now  : nothing — RetroArch has no save for it yet")

        if not plan.found:
            lines += [
                "",
                "No existing save was found, so this path is where RetroArch "
                "would look if the ROM is named the way this tool names it. If "
                "you added the ROM yourself under a different name, RetroArch "
                "will not read this file.",
            ]

        return bool(
            messagebox.askyesno(
                WINDOW_TITLE, "\n".join(lines), parent=self.root, default="no"
            )
        )

    # ------------------------------------------------------------------
    # Controller Paks
    #
    # The one thing Delta will not sync. ``GameSave.syncableFiles`` has no
    # mempak entry, so the .mpk files on the phone never reach Dropbox and
    # nothing this program can read will ever contain them.
    #
    # The tab is useful with or without the add-on, which is the point of
    # splitting the feature in two. Getting the files off the phone needs a
    # cable and a third-party library, and that is the add-on's job. Merging
    # them into RetroArch's save needs nothing, and is this program's -- so
    # someone who copied the folder off by hand in Explorer (Delta sets
    # UIFileSharingEnabled, so they can) gets the whole feature with no
    # download at all.
    # ------------------------------------------------------------------

    def _build_paks_tab(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)

        ttk.Label(
            parent,
            text=(
                "Delta does not sync Nintendo 64 Controller Paks — ghosts, "
                "extra save slots, anything a game writes to a pak. They only "
                "exist on the phone. This merges them into RetroArch's save, "
                "backing it up first, and never lets a blank pak overwrite one "
                "that has notes on it."
            ),
            style="Muted.TLabel",
            wraplength=display.px(620),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.pak_status = ttk.Label(parent, text="", style="Muted.TLabel",
                                    wraplength=display.px(620), justify="left")
        self.pak_status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        columns = ("game", "save", "paks")
        self.pak_list = ttk.Treeview(
            parent, columns=columns, show="headings", height=8, selectmode="browse"
        )
        for key, title, anchor in (
            ("game", "Nintendo 64 game", "w"),
            ("save", "RetroArch save", "w"),
            ("paks", "Controller Paks", "w"),
        ):
            self.pak_list.heading(key, text=title)
            self.pak_list.column(key, anchor=anchor, stretch=(key == "game"))
        self.pak_list.grid(row=2, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            parent, orient="vertical", command=self.pak_list.yview
        )
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.pak_list.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="Refresh", command=self.on_refresh_paks).grid(
            row=0, column=1, padx=4
        )

        check_button = ttk.Button(
            actions, text="Check the phone", command=self.on_check_paks
        )
        check_button.grid(row=0, column=2, padx=4)
        Tooltip(
            check_button,
            "Asks the Controller Pak add-on what it can see: whether Apple's "
            "device service is running, whether a phone is connected, and "
            "which pak files are on it.\n\n"
            "Needs the separate Controller Pak download. Nothing is written.",
        )

        folder_button = ttk.Button(
            actions, text="Merge from a folder…", command=self.on_merge_paks
        )
        folder_button.grid(row=0, column=3, padx=4)
        self.pak_confirmation = Confirmation(folder_button)
        Tooltip(
            folder_button,
            "Merges Controller Pak files from a folder into the selected "
            "game's RetroArch save.\n\n"
            "Works with no add-on at all: copy Delta → Cores → Mupen64Plus → "
            "Saves off the phone in Explorer and point this at it. The add-on "
            "just does that copying for you.\n\n"
            "RetroArch's save is backed up first, and a blank pak never "
            "overwrites one with saved notes.",
        )

        self.on_refresh_paks()

    def _pak_addon(self) -> "addons.Addon | None":
        return addons.find("controller-pak", addons.search_roots(_state_dir()))

    def on_refresh_paks(self) -> None:
        """List the N64 games, and say whether the add-on is installed."""
        self.config = self._current_config()
        self.pak_games: list[tuple[inspect_module.GameEntry, Path | None]] = []
        self.pak_list.delete(*self.pak_list.get_children())

        addon = self._pak_addon()
        if addon is None:
            self.pak_status.configure(
                text=(
                    "The Controller Pak add-on is not installed — that is the "
                    "separate download that talks to the phone over USB. "
                    "Merging from a folder works without it."
                )
            )
        else:
            self.pak_status.configure(text=f"Add-on: {addon.describe()}")

        prepared = self._prepare()
        if prepared is None:
            self.pak_list.insert(
                "", "end", values=("Delta or RetroArch not found", "", "")
            )
            return
        paths, entries, installed, sorted_by_core, _cheats, _playlists = prepared

        n64_games = [
            entry
            for entry in entries
            if entry.system is not None and entry.system.converted
        ]
        if not n64_games:
            self.pak_list.insert(
                "", "end", values=("No Nintendo 64 games in Delta", "", "")
            )
            return

        for index, entry in enumerate(n64_games):
            assert entry.system is not None
            filename = naming.save_filename(
                entry.name, entry.system.retroarch_save_ext
            )
            matches = sync_module.find_saves(paths.save_dir, filename)
            target = matches[0] if len(matches) == 1 else None

            if len(matches) > 1:
                where, state = "more than one — see the log", ""
            elif target is None:
                core = next(
                    (n for n in entry.system.retroarch_cores if n in installed), ""
                )
                target = sync_module.retroarch_save_path(
                    paths.save_dir, entry, core, sorted_by_core
                )
                where, state = "none yet", "would be created"
            else:
                where = str(target.parent.name or target.parent)
                try:
                    data = target.read_bytes()
                except OSError:
                    data = b""
                if len(data) != n64.SRM_SIZE:
                    state = "not a Mupen64Plus-Next save"
                else:
                    used = [
                        str(slot + 1)
                        for slot, pak in enumerate(n64.controller_paks(data))
                        if not n64.controller_pak_is_empty(pak)
                    ]
                    state = f"data in {', '.join(used)}" if used else "all blank"

            self.pak_games.append((entry, target))
            self.pak_list.insert(
                "", "end", iid=str(index), values=(entry.name, where, state)
            )

    def _selected_pak_game(self):
        selection = self.pak_list.selection()
        if not selection:
            return None
        try:
            return self.pak_games[int(selection[0])]
        except (ValueError, IndexError, AttributeError):
            return None

    def on_check_paks(self) -> None:
        """Ask the add-on what it can see. Reads nothing of ours."""
        addon = self._pak_addon()
        if addon is None:
            messagebox.showinfo(
                WINDOW_TITLE,
                "The Controller Pak add-on is not installed.\n\nIt is a "
                "separate download because it needs a USB cable, a trust "
                "pairing and Apple's device service — none of which the rest "
                "of this program requires.\n\nYou can still use "
                "“Merge from a folder”: copy Delta → Cores "
                "→ Mupen64Plus → Saves off the phone yourself.",
                parent=self.root,
            )
            return

        self._say("")
        self._say("--- Controller Pak add-on ---", "heading")
        reply = addons.run(addon, ["probe"], timeout=60.0)
        for line in reply.progress():
            self._say(f"  {line}", "muted")

        if not reply.ok:
            self._say(f"  {reply.error}", "error")
            return

        for check in reply.result.get("checks", []):
            level = "ok" if check.get("ok") else "warn"
            self._say(f"  {check.get('check')}: {check.get('detail')}", level)

        if not reply.result.get("ready"):
            self._say(
                "  Not ready yet — the lines above say what is missing.", "muted"
            )
            return

        listing = addons.run(addon, ["list"], timeout=60.0)
        if not listing.ok:
            self._say(f"  {listing.error}", "error")
            return
        files = listing.result.get("files", [])
        if not files:
            self._say(
                "  No files where Delta keeps them. Delta writes Controller "
                "Pak files only once an N64 game has been played.",
                "warn",
            )
            return
        self._say("  On the phone:")
        for item in files:
            self._say(f"    {item.get('name')}  ({item.get('size', 0):,} B)")

    def on_merge_paks(self) -> None:
        """Read pak files from a folder and merge them into RetroArch's save."""
        selected = self._selected_pak_game()
        if selected is None:
            messagebox.showinfo(
                WINDOW_TITLE,
                "Pick the Nintendo 64 game to merge into, from the list.",
                parent=self.root,
            )
            return
        entry, target = selected
        if target is None:
            messagebox.showerror(
                WINDOW_TITLE,
                f"RetroArch has more than one save named for {entry.name}, and "
                f"only one of them is the file it loads. Move or delete the "
                f"others first.",
                parent=self.root,
            )
            return

        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Folder holding the .mpk files (Delta/Cores/Mupen64Plus/Saves)",
        )
        if not chosen:
            return

        try:
            found = paks.read_folder(Path(chosen))
            plan = paks.plan_install(found, target)
        except paks.PakError as error:
            self._say(f"Cannot merge those Controller Paks: {error}", "error")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        if not plan.changes_anything:
            messagebox.showinfo(
                WINDOW_TITLE,
                f"Nothing to merge for {entry.name}.\n\n{plan.describe()}",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            WINDOW_TITLE,
            f"Merge Controller Pak data into {entry.name}?\n\n"
            f"{plan.describe()}\n\n"
            f"RetroArch's save is backed up first. The cartridge save inside "
            f"it is not touched.",
            parent=self.root,
            default="no",
        ):
            return

        prepared = self._prepare()
        if prepared is None:
            return
        try:
            said = paks.install(plan, prepared[0].backup_dir)
        except (paks.PakError, OSError) as error:
            self._say(f"Could not merge those Controller Paks: {error}", "error")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        self._say(said, "heading")
        for pak in plan.protecting:
            self._say(
                f"  Kept this PC's {pak.label} — the folder's copy is blank.",
                "muted",
            )
        self.pak_confirmation.show("Merged")
        self.on_refresh_paks()

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        ttk.Label(
            parent,
            text="Hover any setting for what it does.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        # Not named `paths`: that shadows the paths module this file imports.
        path_frame = ttk.LabelFrame(parent, text="Paths", padding=display.px(6))
        path_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        path_frame.columnconfigure(1, weight=1)

        self._path_row(
            path_frame, 0, "Delta folder (Dropbox)", self.delta_var,
            directory=True, help_key="delta_folder",
        )
        self._path_row(
            path_frame, 1, "RetroArch", self.exe_var,
            directory=False, help_key="retroarch_exe",
        )
        self._path_row(
            path_frame, 2, "ROM folder", self.rom_var,
            directory=True, help_key="rom_dir",
        )

        options = ttk.LabelFrame(
            parent, text="Sync options", padding=display.px(6)
        )
        options.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self._option_row(
            options, 0, "Sync as soon as this window opens",
            self.open_sync_var, "sync_on_open",
        )
        self._option_row(
            options, 1, "Copy ROMs from Delta", self.roms_var, "sync_roms",
        )
        self._option_row(
            options, 2, "Export cheats as .cht files", self.cheats_var, "sync_cheats",
        )
        self._option_row(
            options, 3, "Send desktop saves back to Delta (experimental)",
            self.push_var, "push_enabled", command=self._push_toggled,
        )

        dropbox_row = ttk.Frame(options)
        dropbox_row.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.dropbox_label = ttk.Label(dropbox_row, text="", style="Muted.TLabel")
        self.dropbox_label.grid(row=0, column=0, sticky="w")
        self.auth_button = ttk.Button(
            dropbox_row, text="Authorise Dropbox…", command=self.on_authorise
        )
        self.auth_button.grid(row=0, column=1, padx=(10, 0))
        ttk.Button(
            dropbox_row, text="Use own app key…", command=self.on_change_app_key
        ).grid(row=0, column=2, padx=(6, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(1, weight=1)

        if shortcut_module.supported():
            # Widths are fixed on both buttons below because their labels change
            # in place -- "Saved!", "Added!" -- and a button that resizes as it
            # answers you drags the whole row sideways.
            self.start_menu_button = ttk.Button(
                actions, width=19, command=self.on_start_menu
            )
            self.start_menu_button.grid(row=0, column=0, sticky="w")
            self.start_menu_confirm = Confirmation(self.start_menu_button)
            Tooltip(
                self.start_menu_button,
                "Puts this program in your Start menu and on your desktop, so "
                "you can find it again without going back to the folder you "
                "unzipped it into.\n\n"
                "Installs for you only and needs no administrator rights. "
                "Nothing else on your computer is changed, and the same button "
                "removes them again.",
            )
            self._refresh_start_menu_button()

        open_button = ttk.Button(
            actions, text="Open settings folder", command=self.on_open_settings_folder
        )
        open_button.grid(row=0, column=2, padx=4)
        Tooltip(
            open_button,
            "Opens the folder holding config.toml, the Dropbox token, the "
            "manifest and your save backups.\n\n"
            "Everything this program remembers lives there, beside the "
            "executable.",
        )
        self.save_button = ttk.Button(
            actions, text="Save settings", width=14, command=self.on_save
        )
        self.save_button.grid(row=0, column=3, padx=4)
        self.save_confirm = Confirmation(self.save_button)

    def _option_row(
        self, parent: ttk.LabelFrame, row: int, label: str,
        var: tk.BooleanVar, help_key: str, *, command: object = None,
    ) -> None:
        kwargs: dict[str, Any] = {"text": label, "variable": var}
        if command is not None:
            kwargs["command"] = command
        box = ttk.Checkbutton(parent, **kwargs)
        box.grid(row=row, column=0, sticky="w", pady=1)
        Tooltip(box, SETTING_HELP[help_key])

    def _path_row(
        self, parent: ttk.LabelFrame, row: int, label: str, var: tk.StringVar,
        *, directory: bool, help_key: str = "",
    ) -> None:
        name = ttk.Label(parent, text=label)
        name.grid(row=row, column=0, sticky="w", padx=(0, 6))
        entry = ttk.Entry(parent, textvariable=var, width=52)
        entry.grid(row=row, column=1, sticky="ew")
        if help_key:
            Tooltip(name, SETTING_HELP[help_key])
            Tooltip(entry, SETTING_HELP[help_key])
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
        self.config, self.discovery_notes = resolve_paths(self.config)

    # ----------------------------------------------------------------- log

    def write(self, text: str, level: str = "") -> None:
        """Append a line, optionally tagged with a severity.

        Colour is deliberately redundant with the wording: a line that reads
        "FAILED" or "skipped" says so whether or not the reader can tell red
        from amber. The colour makes the log scannable; it never carries
        meaning on its own.
        """
        self.log.configure(state="normal")
        start = self.log.index("end-1c")
        self.log.insert("end", text + "\n")
        if level:
            self.log.tag_add(level, start, "end-1c")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _report_startup(self) -> None:
        self.write(f"{WINDOW_TITLE}", "heading")
        delta = self.config.delta_folder
        exe = self.config.retroarch_exe
        self.write(f"Delta folder : {delta or 'not found'}")
        self.write(f"RetroArch    : {exe or 'not found'}")
        self._refresh_dropbox_label()
        # Only when it failed. Working correctly is not news, and a line about
        # display scaling in a log about saves would be noise every single run.
        if display.on_windows() and display.awareness() == "unavailable":
            self.write(
                "Note: this Windows could not be told about display scaling, "
                "so the window may look soft. Nothing else is affected.",
                "muted",
            )
        if delta and exe:
            self.write("Ready. Press Sync and Play.", "ok")
            if self.config.sync_on_open:
                # Pull straight away: by the time you have read this line the
                # desktop already has whatever you did on your phone.
                self.root.after(200, self.on_sync)
        else:
            # What to do comes before how to override it. Someone who has never
            # heard of Delta's Dropbox folder cannot act on "a path is missing",
            # and that was the single most repeated complaint from both
            # naive-user tests.
            for note in self.discovery_notes:
                self.write("")
                self.write(note, "warn")
            self.write("")
            self.write(
                "Press Instructions, bottom left, for the whole procedure with "
                "each step marked done or not.",
                "muted",
            )
            self.write(
                "Or set the paths on the Settings tab by hand, then press "
                "Save settings.",
                "muted",
            )

    # ------------------------------------------------------------ the guide

    def _progress(self) -> guide.Progress:
        """How far this machine has got, read fresh rather than from config.

        Fresh because the whole point of the window is to be looked at while
        the missing pieces are being installed: someone who signs into Dropbox
        and presses Instructions again should see that step tick over.
        """
        delta = self.config.delta_folder
        if delta is None or not delta.is_dir():
            found = discovery.find_delta_folder()
            delta = found.path

        retro_config = self.config.retroarch_config
        if retro_config is None or not retro_config.is_file():
            found = discovery.find_retroarch_config()
            retro_config = found.path

        cores = 0
        if retro_config is not None and retro_config.is_file():
            settings = discovery.parse_retroarch_config(retro_config)
            cores = len(discovery.installed_cores(retro_config, settings))

        return guide.Progress(
            dropbox_installed=discovery.dropbox_client() is not None,
            dropbox_signed_in=bool(discovery.dropbox_roots()),
            delta_folder_found=delta is not None and delta.is_dir(),
            retroarch_installed=discovery.find_retroarch_exe(retro_config) is not None,
            retroarch_launched=retro_config is not None and retro_config.is_file(),
            cores_installed=cores,
        )

    def on_instructions(self) -> None:
        """Open the setup checklist, or bring it forward if it is already up."""
        if self.instructions is not None and self.instructions.winfo_exists():
            self.instructions.lift()
            self.instructions.focus_set()
            self._fill_instructions()
            return

        window = tk.Toplevel(self.root)
        window.title("Setting this up")
        window.transient(self.root)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(0, weight=1)

        frame = ttk.Frame(window, padding=display.px(10))
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(
            frame, width=72, height=22, wrap="word",
            font=("Segoe UI", 10),
            padx=display.px(12), pady=display.px(10), relief="flat",
        )
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(
            buttons, text="Check again", command=self._fill_instructions
        ).grid(row=0, column=1, padx=4)
        ttk.Button(buttons, text="Close", command=window.destroy).grid(
            row=0, column=2, padx=4
        )

        self.instructions = window
        self.instructions_text = text
        self._style_instructions()
        self._fill_instructions()

    def _style_instructions(self) -> None:
        """Colour the setup window for the current theme.

        Its own tags rather than the log's: this is prose in a proportional
        face, and apply_to_log reads the family back out of the widget by
        splitting on whitespace, which turns "Segoe UI" into "Segoe".
        """
        text = self.instructions_text
        if text is None or not text.winfo_exists():
            return
        palette = theme.palette_for(self.appearance)
        text.configure(
            background=palette.log_bg,
            foreground=palette.log_fg,
            selectbackground=palette.select_bg,
            selectforeground=palette.select_fg,
            highlightthickness=0,
            borderwidth=0,
        )
        text.tag_configure(
            "step", foreground=palette.ink, font=("Segoe UI", 10, "bold"),
            spacing1=display.px(10), spacing3=display.px(2),
        )
        text.tag_configure("done", foreground=palette.log_ok)
        text.tag_configure("todo", foreground=palette.log_warn)
        indent = display.px(22)
        text.tag_configure(
            "body", foreground=palette.log_fg,
            lmargin1=indent, lmargin2=indent, spacing3=display.px(4),
        )
        note_indent = display.px(12)
        text.tag_configure(
            "note", foreground=palette.log_muted,
            lmargin1=note_indent, lmargin2=note_indent, spacing3=display.px(6),
        )
        text.tag_configure(
            "title", foreground=palette.ink, font=("Segoe UI", 12, "bold"),
            spacing3=display.px(8),
        )

    def _fill_instructions(self) -> None:
        """Rewrite the checklist against what is on this machine right now."""
        text = self.instructions_text
        if text is None or not text.winfo_exists():
            return

        steps = guide.setup_steps(self._progress())
        remaining = guide.next_step(steps)

        text.configure(state="normal")
        text.delete("1.0", "end")

        def line(body: str, *tags: str) -> None:
            start = text.index("end-1c")
            text.insert("end", body + "\n")
            for tag in tags:
                text.tag_add(tag, start, "end-1c")

        if remaining is None:
            line("Everything is set up.", "title")
        else:
            line(f"Next step — {remaining.title}", "title")

        for index, step in enumerate(steps, start=1):
            line(
                f"{step.marker} {index}. {step.title}",
                "step",
                "done" if step.done else "todo",
            )
            line(step.body, "body")

        line("")
        for note in guide.NOTES:
            line(note, "note")

        text.configure(state="disabled")
        text.see("1.0")

    def _refresh_dropbox_label(self) -> None:
        token = _state_dir() / dropbox_api.TOKEN_FILENAME
        own = " (your app)" if self.config.dropbox_app_key else ""
        if token.is_file():
            self.dropbox_label.configure(text=f"Dropbox authorised{own}")
        else:
            # "Dropbox not authorised" on its own reads as a fault, and the
            # third naive-user test read it that way -- while every save was
            # pulling correctly, because pulling never touches Dropbox's API.
            self.dropbox_label.configure(
                text=f"Dropbox not authorised{own} — only needed to send saves back"
            )

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

        # Opening the browser first was the third test's complaint: the
        # explanation went to the log, on a different tab, and Dropbox's consent
        # page had already taken over the screen before it could be read. Say it
        # here, in front of the person who pressed the button.
        if not messagebox.askokcancel(
            WINDOW_TITLE,
            "Authorising lets this program read one thing back from Dropbox: "
            "the version number Dropbox gives a save after your Dropbox app "
            "uploads it. Delta refuses a save whose version number is wrong, "
            "which is why sending saves back needs this.\n\n"
            "Pulling from Delta never needs it. That is why everything else "
            "works without authorising.\n\n"
            "There are three steps:\n"
            "  1. Your browser opens Dropbox's approval page.\n"
            "  2. You approve, and Dropbox shows you a code.\n"
            "  3. You come back to this window and paste the code in.\n\n"
            "Only permission to read file details is requested. This program "
            "never uploads through Dropbox.",
            parent=self.root,
        ):
            self.write("Authorisation cancelled.")
            return

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
        # Both, not either: the log is the running record of what happened, and
        # the button is what the person who pressed it is actually looking at.
        self.write(f"Settings saved to {path.name}.")
        self.save_confirm.show("Saved!")

    # ----------------------------------------------------------- start menu

    def _refresh_start_menu_button(self) -> None:
        label = (
            "Remove shortcuts" if shortcut_module.installed() else "Add shortcuts"
        )
        self.start_menu_confirm.settle(label)

    def on_start_menu(self) -> None:
        """Add or remove the shortcuts, whichever the button offers."""
        removing = shortcut_module.installed()
        try:
            changed = (
                shortcut_module.remove() if removing else shortcut_module.create()
            )
        except shortcut_module.ShortcutError as error:
            verb = "remove" if removing else "add"
            self.write(f"Could not {verb} the shortcuts: {error}", "error")
            messagebox.showerror(WINDOW_TITLE, str(error), parent=self.root)
            return

        # Named individually, because "added to your Start menu and desktop"
        # is a claim the user can check and should be able to.
        verb = "Removed from" if removing else "Added to"
        places = " and ".join(changed) or "nowhere"
        self.write(f"{verb} your {places}.", "" if removing else "ok")
        for place, link in changed.items():
            self.write(f"    {place}: {link}", "muted")
        self._refresh_start_menu_button()
        self.start_menu_confirm.show("Removed!" if removing else "Added!")

    def _offer_start_menu(self) -> None:
        """Ask once, on first run, whether to add shortcuts.

        Three separate checkpoints of the third naive-user test came back with
        the same sentence -- it never appears in the Start menu or in search --
        so a button on the Settings tab does not fix this on its own. That test
        never opened Settings; it had no reason to.

        Asked rather than done: a portable program that installs itself
        somewhere the user did not choose is the behaviour people unzip a
        portable build to avoid.
        """
        if self.config.shortcuts_offered or not shortcut_module.supported():
            return

        # Written before the dialog opens, so force-quitting inside it cannot
        # turn a once-ever question into one that returns on every launch.
        # Built from what is on disk rather than from self.config, which by now
        # holds discovered paths -- recording those would freeze a guess that is
        # meant to be made fresh each run.
        config_module.save(replace(config_module.load(), shortcuts_offered=True))
        self.config = replace(self.config, shortcuts_offered=True)

        if shortcut_module.installed():
            return

        wants = messagebox.askyesno(
            WINDOW_TITLE,
            "Add shortcuts to your Start menu and desktop?\n\n"
            "You would then find this again by searching for “Delta” "
            "or from your desktop, instead of going back to the folder you "
            "unzipped it into.\n\n"
            "They install for you only, need no administrator rights, and the "
            "Settings tab takes them back out. This is the only time you will "
            "be asked.",
            parent=self.root,
        )
        if not wants:
            self.write(
                "No shortcuts added. The Settings tab can add them "
                "whenever you like.",
                "muted",
            )
            return

        try:
            created = shortcut_module.create()
        except shortcut_module.ShortcutError as error:
            self.write(f"Could not add the shortcuts: {error}", "error")
            return
        self.write(f"Added to your {' and '.join(created)}.", "ok")
        self._refresh_start_menu_button()

    def on_open_settings_folder(self) -> None:
        """Show the folder holding config.toml, the token, manifest and backups.

        Asked for during the first naive-user test: the settings file is
        editable by hand and documented as such, and there was no way to reach
        it from the window that wrote it.
        """
        folder = _state_dir()
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606 -- a folder, not user input
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            self.write(f"Could not open {folder}: {error}")
            return
        self.write(f"Opened {folder}")

    # ------------------------------------------------------------- backups

    def _game_names(self) -> dict[str, str]:
        """Game SHA-1 to display name, for labelling Delta-side backups.

        Failure is not fatal here. The list is still useful without names --
        every row keeps its timestamp, side and size -- and a backup whose Delta
        folder has gone missing is exactly the moment someone needs to see it.
        """
        folder = self.config.delta_folder
        if folder is None or not folder.is_dir():
            return {}
        try:
            return {e.identifier: e.name for e in inspect_module.collect_games(folder)}
        except OSError:
            return {}

    def on_refresh_backups(self) -> None:
        self.config = self._current_config()
        backup_dir = _state_dir() / sync_module.BACKUP_DIRNAME
        self.backup_points = restore.restore_points(
            restore.scan(backup_dir), self._game_names()
        )

        self.backup_list.delete(*self.backup_list.get_children())
        for index, point in enumerate(self.backup_points):
            self.backup_list.insert("", "end", iid=str(index), values=backup_row(point))
        self._size_backup_columns()

    def _size_backup_columns(self) -> None:
        """Widen each column to fit what is actually in it.

        Fixed pixel widths were wrong here. Tk column widths are in pixels while
        the text is drawn in whatever the system font resolves to, so on a
        display running at 150% the timestamps lost their seconds and "RetroArch
        clock" became "RetroArch cl" -- and the seconds are the whole reason
        that column exists, since a push writes the save and the record two or
        three seconds apart.

        Measuring the font instead means the table fits on any display scale and
        any font size. Only Game is capped, because a game name has no bound and
        the window is not resizable; a long one ellipsizes, which is fine when
        the other three columns are what you compare on.
        """
        try:
            row_font = tkfont.nametofont("TkDefaultFont")
        except tk.TclError:  # pragma: no cover -- no font database
            return

        widths = {"game": 120, "what": 60, "when": 60, "size": 50}
        headings = {"game": "Game", "what": "What", "when": "When", "size": "Size"}
        for key, title in headings.items():
            widths[key] = max(widths[key], row_font.measure(title))
        for item in self.backup_list.get_children():
            for key, value in zip(headings, self.backup_list.item(item, "values")):
                widths[key] = max(widths[key], row_font.measure(str(value)))

        # Cell padding Tk adds either side of the text, plus a little air.
        # The measurements themselves already come back in real pixels, because
        # tkfont measures the font as drawn; only these two constants are
        # 96-DPI numbers.
        padding = display.px(18)
        for key in headings:
            width = widths[key] + padding
            if key == "game":
                width = min(width, display.px(260))
            self.backup_list.column(key, width=width, minwidth=width)

    def _selected_point(self) -> "restore.RestorePoint | None":
        selection = self.backup_list.selection()
        if not selection:
            return None
        try:
            return self.backup_points[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def on_restore_selected(self) -> None:
        """Confirm in a dialog, then restore on the worker thread.

        The dialog is this window's equivalent of the CLI's ``--yes``: it names
        the exact file about to be overwritten rather than asking "are you
        sure?", because the thing worth checking is *which version*, and no
        amount of general caution helps with that.
        """
        if self.busy:
            return
        point = self._selected_point()
        if point is None:
            messagebox.showinfo(
                WINDOW_TITLE, "Pick a version from the list first.", parent=self.root
            )
            return

        self.config = self._current_config()
        prepared = self._prepare()
        if prepared is None:
            return
        sync_paths = prepared[0]

        if point.backup.side == restore.RETROARCH:
            target = restore.find_retroarch_target(
                sync_paths.save_dir, point.backup.original_name
            )
            if target is None:
                messagebox.showerror(
                    WINDOW_TITLE,
                    f"Nothing named {point.backup.original_name} under "
                    f"{sync_paths.save_dir}.\n\nRetroArch may not have this game "
                    "any more, or it now sorts saves into a different folder.",
                    parent=self.root,
                )
                return
            destination = str(target)
        else:
            destination = f"Delta's folder — {point.backup.original_name}"

        confirmed = messagebox.askyesno(
            WINDOW_TITLE,
            f"Restore this version?\n\n"
            f"    {point.label}\n"
            f"    {point.backup.side} {point.backup.kind}, {point.backup.when}\n"
            f"    {point.backup.size:,} bytes\n\n"
            f"This overwrites:\n    {destination}\n\n"
            f"That file is backed up first, so this can be undone. "
            f"Afterwards, {restore.RESTORE_IS_A_CHANGE}.",
            parent=self.root,
        )
        if not confirmed:
            return

        self._run(lambda: self._restore_work(point))

    def _restore_work(self, point: "restore.RestorePoint") -> None:
        prepared = self._prepare()
        if prepared is None:
            return
        sync_paths, entries, _, _, _, _ = prepared

        self._say(f"Restoring {point.label} from {point.backup.when}", "heading")
        try:
            if point.backup.side == restore.RETROARCH:
                note = restore.restore_retroarch(
                    point, sync_paths.save_dir, sync_paths.backup_dir
                )
            elif point.backup.kind == "cheat":
                note = restore.restore_delta_cheat(
                    point, sync_paths.delta_folder, sync_paths.backup_dir
                )
            else:
                note = self._restore_into_delta(point, sync_paths, entries)
                if note is None:
                    return
        except (OSError, ValueError, dropbox_api.DropboxError) as error:
            self._say(f"  FAILED: {error}", "error")
            return

        self._say(f"  {note}", "ok")
        self._say(f"  {restore.RESTORE_IS_A_CHANGE}.", "muted")

    def _restore_into_delta(
        self, point: "restore.RestorePoint", sync_paths: sync_module.Paths, entries: list
    ) -> str | None:
        """The half that needs a record rewritten and a revision read back."""
        entry = next(
            (e for e in entries if e.identifier == point.backup.identifier), None
        )
        if entry is None:
            self._say(
                "  Delta no longer has this game, so there is no record to "
                "write the restored save into.",
                "error",
            )
            return None

        credentials = dropbox_api.Credentials.load(
            _state_dir() / dropbox_api.TOKEN_FILENAME
        )
        if credentials is None:
            self._say(f"  Not restored: {delta_writer.REVISION_MUST_BE_REAL}", "warn")
            return None

        system = entry.system
        file_identifier = (
            system.delta_clock_id
            if point.backup.kind == "clock" and system and system.delta_clock_id
            else delta_writer.PRIMARY_FILE
        )
        return restore.restore_delta_save(
            point,
            sync_paths,
            entry,
            dropbox_api.DropboxClient(credentials),
            file_identifier=file_identifier,
        )

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
                self.messages.put(("log", f"ERROR: {error}", "error"))
            finally:
                self.messages.put(("done", "", ""))

        threading.Thread(target=runner, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                kind, text, level = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.write(text, level)
            elif kind == "paths":
                self.delta_var.set(str(self.config.delta_folder or ""))
                self.exe_var.set(str(self.config.retroarch_exe or ""))
                self.rom_var.set(str(self.config.retroarch_rom_dir or ""))
            elif kind == "window":
                # Driven through the queue like everything else: this is asked
                # for from the worker thread and from the tray icon's own
                # thread, and Tk is not safe to touch from anywhere but the
                # thread that made it.
                if text == "hide":
                    self._hide_to_tray()
                else:
                    self._show_from_tray()
            elif kind == "done":
                self.busy = False
                self.play_button.configure(state="normal")
                # A sync takes backups of its own, so the list is stale the
                # moment any work finishes -- not only after a restore.
                self.on_refresh_backups()
        self.root.after(100, self._drain)

    # ----------------------------------------------------------------- work

    def _say(self, text: str, level: str = "") -> None:
        self.messages.put(("log", text, level))

    def _get_out_of_the_way(self) -> None:
        """Ask the window to leave the screen while RetroArch has it."""
        self.messages.put(("window", "hide", ""))

    def _come_back(self) -> None:
        """Ask for it back. Called from the worker thread when RetroArch
        exits, and from the tray icon's thread when the icon is clicked."""
        self.messages.put(("window", "show", ""))

    def _hide_to_tray(self) -> None:
        """Out of the way properly: off the taskbar, into the notification area.

        The first attempt minimised, and the user's objection was exact -- a
        minimised window still holds a taskbar button, which is the thing they
        wanted gone. ``withdraw`` removes it, but only the tray icon makes that
        safe: a withdrawn window with nothing to click is a program that cannot
        be reached.

        So the icon goes up first and the window is only hidden if that worked.
        Minimising is the fallback, because a taskbar button is a far better
        failure than a vanished program.
        """
        if self.tray.show():
            self.root.withdraw()
            self.in_tray = True
        else:
            self.root.iconify()
            self.in_tray = False

    def _show_from_tray(self) -> None:
        self.tray.hide()
        self.in_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    @staticmethod
    def _level_for(outcome: "sync_module.Outcome") -> str:
        """Severity of one sync outcome, from its action rather than its words.

        Taken from the structured result so the colouring cannot drift out of
        step with the message text.
        """
        if outcome.failed or outcome.action is sync_module.Action.CONFLICT:
            return "error"
        if outcome.action is sync_module.Action.SKIPPED:
            return "warn"
        if outcome.action is sync_module.Action.NOTHING:
            return "muted"
        # A pull or push that was decided but not carried out -- a dry run, or a
        # push blocked for want of Dropbox authorisation.
        return "ok" if outcome.applied else "warn"

    def _rediscover(self) -> bool:
        """Run discovery again, and tell the window if anything turned up.

        Without this, Check status could never recover from a problem being
        fixed: the paths were resolved once when the window opened, so someone
        who read "Dropbox is installed but not signed in", signed in, and
        pressed the button was told the same thing again -- and restarting the
        program was the only way through. That is exactly the sequence the
        empty state asks the user to follow.

        The entry fields have to be updated too, not just the config: the next
        action reads its paths back out of them, so leaving them empty would
        throw away what was just found.
        """
        before = (
            self.config.delta_folder,
            self.config.retroarch_config,
            self.config.retroarch_exe,
        )
        self._fill_in_discovered_paths()
        after = (
            self.config.delta_folder,
            self.config.retroarch_config,
            self.config.retroarch_exe,
        )
        if before == after:
            return False
        self.messages.put(("paths", "", ""))
        return True

    def _prepare(self) -> tuple[sync_module.Paths, list, str, bool, Path, Path] | None:
        config = self.config
        missing = (
            config.delta_folder is None
            or not config.delta_folder.is_dir()
            or config.retroarch_config is None
            or not config.retroarch_config.is_file()
        )
        if missing and self._rediscover():
            config = self.config
            self._say("Found something that was not there before:", "ok")
            if config.delta_folder is not None:
                self._say(f"  Delta folder : {config.delta_folder}", "ok")
            if config.retroarch_exe is not None:
                self._say(f"  RetroArch    : {config.retroarch_exe}", "ok")

        if config.delta_folder is None or not config.delta_folder.is_dir():
            # "Delta folder not set or missing" was all Check status said, which
            # the third naive-user test called out by name: the one button that
            # exists to answer "where am I?" restated the symptom. Discovery has
            # already worked out which of the three possible causes it is.
            self._say("Delta's Dropbox folder was not found.", "error")
            self._say(f"  {discovery.find_delta_folder().detail}", "warn")
            self._say("  Press Instructions for the whole procedure.", "muted")
            return None

        retroarch_config = config.retroarch_config
        if retroarch_config is None or not retroarch_config.is_file():
            self._say("RetroArch's settings file was not found.", "error")
            self._say(f"  {discovery.find_retroarch_config().detail}", "warn")
            self._say("  Press Instructions for the whole procedure.", "muted")
            return None

        settings = discovery.parse_retroarch_config(retroarch_config)
        save_dir = config.retroarch_save_dir or discovery.resolve_retroarch_dir(
            settings, "savefile_directory", retroarch_config, "saves"
        )
        cheat_dir = discovery.resolve_retroarch_dir(
            settings, "cheat_database_path", retroarch_config, "cheats"
        )
        playlist_dir = discovery.resolve_retroarch_dir(
            settings, "playlist_directory", retroarch_config, "playlists"
        )
        sorted_by_core = discovery.truthy(settings, "sort_savefiles_enable")
        installed = discovery.installed_cores(retroarch_config, settings)

        entries = inspect_module.collect_games(config.delta_folder)
        if not entries:
            self._say("Delta has not synced any games yet.", "warn")
            return None

        paths = sync_module.Paths(
            delta_folder=config.delta_folder,
            retroarch_config=retroarch_config,
            save_dir=save_dir,
            state_dir=_state_dir(),
        )
        return paths, entries, installed, sorted_by_core, cheat_dir, playlist_dir

    def _status_work(self) -> None:
        # A heading, because this is a new reading rather than more of the same
        # log. Without one, pressing Check status straight after opening the
        # window looks like the startup report simply repeating itself -- which
        # is what the third naive-user test saw.
        self._say("")
        self._say("--- Status ---", "heading")
        prepared = self._prepare()
        if prepared is None:
            return
        _, entries, installed, _, _, _ = prepared
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
        paths, entries, installed, sorted_by_core, cheat_dir, playlist_dir = prepared
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

        self._say(f"--- {label} ---", "heading")
        changed_anything = False
        applied: list[sync_module.Outcome] = []
        # Gathered rather than reported in the loop. The advice is about the
        # system, not the game, and printing it per game turned six N64 games
        # into the same four lines six times over -- "very unclear if multiple
        # games are found", in the third naive-user test's words.
        without_core: dict[str, list[str]] = {}
        for entry in entries:
            if entry.system is None:
                continue
            core = next(
                (name for name in entry.system.retroarch_cores if name in installed),
                None,
            )
            if entry.supported and core is None:
                without_core.setdefault(entry.system.key, []).append(entry.name)
                continue

            report = sync_module.run_sync(
                paths,
                [entry],
                core or "",
                sorted_by_core,
                allow_push=config.push_enabled,
                rom_dir=config.retroarch_rom_dir if config.sync_roms else None,
                playlist_dir=playlist_dir if config.sync_roms else None,
                dropbox=dropbox,
                cheat_dir=cheat_dir if config.sync_cheats else None,
                cheats_by_game=cheats_by_game,
            )
            for outcome in report.outcomes:
                if outcome.action is sync_module.Action.NOTHING:
                    continue
                changed_anything = True
                if outcome.applied:
                    applied.append(outcome)
                level = self._level_for(outcome)
                self._say(f"  {outcome.game}: {outcome.description}", level)
                self._say(f"      {outcome.detail}", level)
            if not any(o.action is not sync_module.Action.NOTHING for o in report.outcomes):
                self._say(f"  {entry.name}: already up to date", "muted")

        # Standalone emulators, for the ones turned on in config.toml. The same
        # pass the `sync` command makes -- kept in step with it deliberately,
        # because an emulator that syncs from the command line and silently does
        # nothing from this window is worse than one that is not supported.
        for outcome in sync_emulators(paths, entries, config, dropbox):
            if outcome.action is sync_module.Action.NOTHING and not outcome.applied:
                continue
            changed_anything = True
            if outcome.applied:
                applied.append(outcome)
            level = self._level_for(outcome)
            self._say(f"  {outcome.game}: {outcome.description}", level)
            self._say(f"      {outcome.detail}", level)

        # The log auto-scrolls, so the last thing written is the only thing
        # guaranteed to be on screen. That makes the bottom the right place for
        # what actually changed -- and it was the wrong place before: a cheat
        # push logged second was pushed off the top by the games after it, and
        # the sync looked like it had done nothing at all.
        if applied:
            self._say("")
            self._say(f"  {summarise_changes(applied)}", "heading")
            for line in change_lines(applied):
                self._say(f"    {line}", "ok")

        # "Already up to date" is the one message that looks identical whether
        # the sync worked perfectly or Delta has been writing to a different
        # Dropbox account for a week. Saying when Delta last wrote is what
        # separates the two, and it is only worth saying when nothing moved.
        if not changed_anything and config.delta_folder is not None:
            activity = health.delta_activity(config.delta_folder)
            self._say("")
            self._say(f"  {health.idle_sync_note(activity, time.time())}", "muted")

        # Last, because it is the one thing in the log that needs the user to go
        # and do something, and the log auto-scrolls to the bottom.
        for key, names in without_core.items():
            system = systems.SYSTEMS.get(key)
            if system is None:  # pragma: no cover -- keys come from SYSTEMS
                continue
            self._say("")
            for line in systems.missing_core_advice(system, names).splitlines():
                self._say(f"  {line}", "warn")

    def _sync_work(self) -> None:
        self._sync_once("Sync")

    def _play_work(self) -> None:
        exe = self.config.retroarch_exe
        if exe is None or not exe.is_file():
            self._say("RetroArch executable not found. Set the path above.", "error")
            return

        # Looked for before the sync so the answer is not buried under the
        # sync's own output. By name, not by the configured path: this machine
        # carries retroarch.exe at two locations, config named one, the user had
        # started the other, and a second copy opened anyway.
        running = processes.find_by_name(exe.name)

        self._sync_once("Sync before playing")

        if running:
            # Attaching rather than merely refusing: the point of the button is
            # the sync that happens after the game closes, and that works just
            # as well on a window this program did not open. It also answers
            # what happens to saves when RetroArch is started some other way.
            where = sorted({str(path) for _pid, path in running})
            self._say(
                f"RetroArch is already running, so a second copy will not be "
                f"started. Waiting for it to close.",
                "warn",
            )
            for path in where:
                self._say(f"    {path}", "muted")
            self._get_out_of_the_way()
            # All of them. Two emulators writing saves for the same games is
            # the state this is here to avoid, so the sync waits for the last.
            for pid, _path in running:
                processes.wait_for_exit(pid)
            self._come_back()
            self._say("RetroArch closed.", "muted")
        else:
            self._say("Launching RetroArch…", "muted")
            try:
                process = subprocess.Popen([str(exe)], cwd=str(exe.parent))
            except OSError as error:
                self._say(f"Could not launch RetroArch: {error}", "error")
                return

            self._get_out_of_the_way()
            process.wait()
            self._come_back()
            self._say(
                f"RetroArch closed (exit code {process.returncode}).",
                "muted" if process.returncode == 0 else "warn",
            )

        # Sync after the process exits, not on a timer: mGBA flushes its save on
        # clean exit, so syncing earlier would copy a stale file.
        self._sync_once("Sync after playing")
        self._say("Done.", "ok")


def main() -> int:
    # Before tk.Tk(), and it cannot be moved: Windows fixes a process's DPI
    # awareness when its first window is created, and ignores it afterwards.
    display.make_process_aware()
    # Without an explicit identity a Python-hosted window is grouped on the
    # taskbar under pythonw.exe and shows its icon, whatever icon we set here.
    display.set_app_id("midwestcrip.DeltaRetroArchSynchronizer")

    root = tk.Tk()
    # A fixed `tk scaling` of 1.25 used to be set here. display.adopt now works
    # the factor out from the display and sets it, so a constant guess is at
    # best redundant and at worst fights the real number.
    window = LauncherWindow(root)
    try:
        root.mainloop()
    finally:
        # An icon left in the notification area after the program exits is the
        # classic tray bug -- Windows only reaps it when someone hovers over it.
        window.tray.hide()
    return 0
