"""Window + tray icon. Never touches hardware: it reads snapshots from the
control loop and sends it commands.

Threading: Tk runs on the main thread. pystray runs detached on its own
thread; its menu callbacks only put messages on a queue that Tk drains.
"""
from __future__ import annotations

import copy
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

from . import APP_NAME, __version__
from . import config as cfgmod
from . import report as reportmod
from . import theme as T
from . import winutil
from .dashboard import Dashboard
from .events import LogBuffer, read_tail
from .history import History
from .pages import CurvesPage, EventsPage, SensorsPage, SettingsPage
from .service import (ControlLoop, Snapshot, ST_AUTO, ST_ERROR, ST_FAULT, ST_HANDOVER, ST_OK,
                      ST_PAUSED, ST_READONLY, ST_STARTING)
from .widgets import NavBar

log = logging.getLogger(__name__)

STATUS_COLORS = {
    ST_OK: "#22a35a",        # green: your curve is in control
    ST_AUTO: "#3b82f6",      # blue: EC Auto chosen
    ST_HANDOVER: "#f59e0b",  # orange: EC temporarily in control (hot / sensor issue)
    ST_PAUSED: "#f59e0b",    # orange: paused because another program is using the EC
    ST_READONLY: "#a855f7",  # purple: read-only test mode
    ST_FAULT: "#dc2626",     # red: fan not spinning or EC errors
    ST_ERROR: "#dc2626",
    ST_STARTING: "#9ca3af",
    "stale": "#9ca3af",      # grey: control loop not responding
}
STATUS_TITLES = {
    ST_OK: "Fan curve active", ST_AUTO: "EC Auto", ST_HANDOVER: "EC in control (safety)",
    ST_PAUSED: "Paused - EC in control",
    ST_READONLY: "Read-only test", ST_FAULT: "FAULT", ST_ERROR: "Cannot reach EC",
    ST_STARTING: "Starting", "stale": "Not responding",
}


def make_icon_image(color: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill="#1f2937")
    d.ellipse((12, 12, 52, 52), fill=color)
    return img


class App:
    def __init__(self, root: tk.Tk, loop: ControlLoop, cfg: dict, cfg_path: Path, *,
                 use_tray: bool, start_hidden: bool, title_suffix: str = "",
                 history_csv: Path | None = None, log_path: Path | None = None,
                 module_path: Path | None = None) -> None:
        self.root = root
        self.loop = loop
        self.cfg = copy.deepcopy(cfg)
        self.cfg_path = cfg_path
        self.module_path = module_path
        self.run_mode = title_suffix.strip(" []").lower() or "normal"
        self.ui_queue: "queue.Queue[str]" = queue.Queue()
        self.icon = None
        self._icon_state = None
        self._exiting = False
        self._failed_pages: set[str] = set()
        self.page_key = "overview"

        T.init_scaling(root)
        T.apply_dark_theme(root)
        self.history = History()
        if history_csv is not None:
            log.info("Dashboard history: %d earlier samples loaded", self.history.prefill_csv(history_csv))
        self.log_buffer = LogBuffer()
        if log_path is not None:
            self.log_buffer.prefill(read_tail(log_path))
        logging.getLogger().addHandler(self.log_buffer)         # from here on, new log lines reach the Events page

        root.title(APP_NAME + title_suffix)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(T.px(1140), sw - 80), min(T.px(860), sh - 120)          # never bigger than the screen
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(T.px(980), sw - 80), min(T.px(680), sh - 120))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build(log_path.parent if log_path is not None else None)
        T.enable_dark_titlebar(root)
        self.load_settings_into_ui(self.cfg)
        self._navigate("overview")

        if use_tray:
            self._start_tray()
            self._schedule_pin()
        if start_hidden and self.icon is not None:
            root.withdraw()
        self.root.after(300, self._poll)

    # ------------------------------------------------------------- layout
    def _build(self, log_dir: Path | None) -> None:
        r = self.root
        r.columnconfigure(1, weight=1)
        r.rowconfigure(0, weight=1)
        self.nav = NavBar(r, [("overview", "Overview", "overview"), ("curves", "Fan curves", "curves"),
                              ("sensors", "Sensors", "sensors"), ("events", "Events", "events")],
                          [("settings", "Settings", "settings")], self._navigate)
        self.nav.grid(row=0, column=0, sticky="ns")

        main = tk.Frame(r, bg=T.BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main, padding=(T.px(16), T.px(12), T.px(16), T.px(4)))
        top.grid(row=0, column=0, sticky="ew")
        self.dot = tk.Canvas(top, width=T.px(18), height=T.px(18), highlightthickness=0)
        self.dot_id = self.dot.create_oval(T.px(2), T.px(2), T.px(16), T.px(16), fill=STATUS_COLORS[ST_STARTING],
                                           outline="")
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.status_lbl = ttk.Label(top, text="Starting", font=T.font(12, True))
        self.status_lbl.grid(row=0, column=1, sticky="w")
        self.msg_lbl = ttk.Label(top, text="", foreground=T.TEXT2)
        self.msg_lbl.grid(row=1, column=1, sticky="w")
        top.columnconfigure(1, weight=1)
        modef = ttk.Frame(top)
        modef.grid(row=0, column=2, rowspan=2, sticky="e")
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        ttk.Radiobutton(modef, text="Fan curve", value="curve", variable=self.mode_var,
                        command=self._mode_clicked).pack(side="left", padx=4)
        ttk.Radiobutton(modef, text="EC Auto (BIOS)", value="auto", variable=self.mode_var,
                        command=self._mode_clicked).pack(side="left", padx=4)

        host = tk.Frame(main, bg=T.BG)
        host.grid(row=1, column=0, sticky="nsew", padx=T.px(16), pady=(T.px(6), T.px(8)))
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        about = (f"{APP_NAME} {__version__}\nTested on one laptop: ThinkPad P1 Gen 6. See the README before "
                 f"trusting it on anything else.\nSettings are saved next to the program (config.json).")
        self.settings = SettingsPage(host, self._dirty, about)
        self.curves = CurvesPage(host, self._dirty, self.settings.params)
        self.sensors = SensorsPage(host, self.history, self._dirty)
        self.events = EventsPage(host, self.log_buffer, log_dir, self._make_report)
        self.dashboard = Dashboard(host, self.history)
        self.pages = {"overview": self.dashboard, "curves": self.curves, "sensors": self.sensors,
                      "events": self.events, "settings": self.settings}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()

        # shown only while there are unsaved changes (grid row 2 shrinks to nothing otherwise)
        self.bar = tk.Frame(main, bg=T.SURFACE, padx=T.px(16), pady=T.px(8))
        self.bar.grid(row=2, column=0, sticky="ew")
        tk.Label(self.bar, text="You have unsaved changes", bg=T.SURFACE, fg=T.WARN, font=T.font(10)).pack(side="left")
        ttk.Button(self.bar, text="Revert", command=self._revert).pack(side="right")
        ttk.Button(self.bar, text="Apply & save", style="Accent.TButton", command=self._apply).pack(side="right", padx=8)
        self.bar.grid_remove()

    def _navigate(self, key: str) -> None:
        self.page_key = key
        for k, page in self.pages.items():
            if k == key:
                page.grid()
            else:
                page.grid_remove()
        self.nav.select(key)
        self._update_page(self.loop.snapshot())

    # ----------------------------------------------------- settings <-> UI
    def load_settings_into_ui(self, cfg: dict) -> None:
        for page in (self.settings, self.curves, self.sensors):
            page.load(cfg)
        self.mode_var.set(cfg["mode"])
        self.bar.grid_remove()

    def settings_from_ui(self) -> dict:
        cfg = copy.deepcopy(self.cfg)
        for page in (self.settings, self.curves, self.sensors):
            page.collect(cfg)
        cfg["mode"] = self.mode_var.get()
        return cfg

    def _dirty(self) -> None:
        self.bar.grid()

    def _apply(self) -> None:
        wanted = self.settings_from_ui()
        clean, problems = cfgmod.validate(wanted)
        if problems:
            if not messagebox.askokcancel("Adjust settings?",
                                          "Some values were adjusted to stay safe:\n\n- " + "\n- ".join(problems)
                                          + "\n\nApply the adjusted settings?"):
                return
        try:
            cfgmod.save(self.cfg_path, clean)
        except OSError as e:
            messagebox.showerror("Save failed", f"Could not write {self.cfg_path}:\n{e}")
            return
        self.cfg = clean
        self.loop.apply_config(copy.deepcopy(clean))
        self.load_settings_into_ui(clean)
        log.info("Settings saved from the window")

    def _revert(self) -> None:
        self.load_settings_into_ui(self.cfg)

    def _mode_clicked(self) -> None:
        self.set_mode(self.mode_var.get())

    def set_mode(self, mode: str) -> None:
        self.cfg["mode"] = mode
        self.mode_var.set(mode)
        self.loop.set_mode(mode)
        try:
            cfgmod.save(self.cfg_path, self.cfg)
        except OSError:
            log.exception("could not save mode")
        if self.icon is not None:
            self.icon.update_menu()

    # ------------------------------------------------------------- report
    def _make_report(self) -> str:
        h, now, window = self.history, time.time(), 1800
        stats = {"window": "30 min", "peak": h.peak(window, now), "changes_per_hour": h.changes_per_hour(window, now),
                 "handovers": h.status_entries(["handover"], window, now),
                 "pauses": h.status_entries(["paused"], window, now)}
        ranges = {name: (lo, hi) for name, (lo, hi) in h.sensor_stats.items()}
        return reportmod.build_report(
            version=__version__, frozen=bool(getattr(sys, "frozen", False)), machine=reportmod.machine_info(),
            module_sha=reportmod.sha256_file(self.module_path) if self.module_path else None, cfg=self.cfg,
            snap=self.loop.snapshot(), stats=stats, sensor_ranges=ranges, entries=self.log_buffer.snapshot(),
            run_mode=self.run_mode)

    # --------------------------------------------------------------- tray
    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as e:
            log.warning("Tray icon unavailable (%s); running as a normal window", e)
            return
        M = pystray.MenuItem
        menu = pystray.Menu(
            M("Open", lambda: self.ui_queue.put("show"), default=True),
            pystray.Menu.SEPARATOR,
            M("Fan curve", lambda: self.ui_queue.put("mode:curve"),
              checked=lambda _i: self.cfg["mode"] == "curve", radio=True),
            M("EC Auto (BIOS)", lambda: self.ui_queue.put("mode:auto"),
              checked=lambda _i: self.cfg["mode"] == "auto", radio=True),
            pystray.Menu.SEPARATOR,
            M("Exit (fans back to BIOS)", lambda: self.ui_queue.put("exit")),
        )
        self.icon = pystray.Icon("CustomFanController", make_icon_image(STATUS_COLORS[ST_STARTING]),
                                 f"{APP_NAME} - starting", menu)
        try:
            self.icon.run_detached()
        except Exception as e:
            log.warning("Tray icon failed to start (%s)", e)
            self.icon = None

    def _schedule_pin(self) -> None:
        """First run only (per executable): pin the tray icon so it shows on the
        taskbar instead of in the ^ overflow. Later choices in Windows Settings
        are left alone, because we never pin the same executable twice."""
        self._pin_exe = Path(sys.executable).name
        if self.icon is None or not winutil.IS_WINDOWS:
            return
        if not getattr(sys, "frozen", False):
            # Script mode: the icon belongs to python(w).exe, and pinning that would
            # pin every Python tray icon. Only the built exe pins itself.
            log.info("Running as a script: tray icon not auto-pinned (build the exe for that)")
            return
        if self._pin_exe.lower() in self.cfg.get("tray_pinned_for", []):
            return
        # Windows registers the icon a moment after it first appears; retry a few times.
        self._pin_delays = [2000, 6000, 15000, 45000]
        self.root.after(self._pin_delays.pop(0), self._try_pin)

    def _try_pin(self) -> None:
        result = winutil.promote_tray_icon(self._pin_exe)
        if result in (winutil.PIN_PROMOTED, winutil.PIN_ALREADY):
            log.info("Tray icon pinned to the taskbar for %s (%s)", self._pin_exe, result)
            self._remember_pinned()
        elif result == winutil.PIN_UNSUPPORTED:
            log.info("Automatic tray pinning isn't available on this Windows version; pin it in "
                     "Settings > Personalization > Taskbar > Other system tray icons")
            self._remember_pinned()             # don't retry on every start
        elif self._pin_delays:
            self.root.after(self._pin_delays.pop(0), self._try_pin)
        else:
            log.info("Windows hasn't registered the tray icon yet; will try again next start")

    def _remember_pinned(self) -> None:
        pinned = set(self.cfg.get("tray_pinned_for", [])) | {self._pin_exe.lower()}
        self.cfg["tray_pinned_for"] = sorted(pinned)
        try:
            cfgmod.save(self.cfg_path, self.cfg)
        except OSError:
            log.exception("could not save tray pin state")

    def _set_tray(self, status: str, tooltip: str) -> None:
        if self.icon is None:
            return
        if status != self._icon_state:
            self.icon.icon = make_icon_image(STATUS_COLORS.get(status, "#9ca3af"))
            self._icon_state = status
        self.icon.title = tooltip[:127]          # Windows tooltip limit

    # ------------------------------------------------------------ polling
    def _poll(self) -> None:
        while True:
            try:
                msg = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if msg == "show":
                self.show()
            elif msg.startswith("mode:"):
                self.set_mode(msg.split(":", 1)[1])
            elif msg == "exit":
                self.exit()
                return
        self._render(self.loop.snapshot())
        if not self._exiting:
            self.root.after(500, self._poll)

    def _update_page(self, s: Snapshot) -> None:
        """Feed the visible page. A drawing bug must never break status or tray updates."""
        key = self.page_key
        try:
            self.pages[key].update_view(s, self.loop.cfg)
        except Exception:
            if key not in self._failed_pages:
                self._failed_pages.add(key)
                log.exception("Updating the %s page failed (further errors on this page suppressed)", key)

    def _render(self, s: Snapshot) -> None:
        stale_after = 3 * float(self.loop.cfg["cycle_s"]) + 8
        status = s.status
        if s.time and time.time() - s.time > stale_after and status not in (ST_ERROR,):
            status = "stale"
        if not self.loop.is_alive() and not self._exiting:
            status = "stale"
        color = STATUS_COLORS.get(status, "#9ca3af")
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.status_lbl.configure(text=STATUS_TITLES.get(status, status))
        msg = s.message if status != "stale" else "Control loop not responding - fans may be at a fixed level. Exit and restart."
        self.msg_lbl.configure(text=msg)

        t = s.temps or {}
        def fmt_t(name):
            v = t.get(name)
            return "-" if v is None else f"{v}°C"
        fans = {f.fan: f for f in s.fans}
        try:
            self.history.add_snapshot(s)
        except Exception:
            log.exception("history update failed")
        if self.root.winfo_viewable():
            self._update_page(s)

        f1, f2 = fans.get(1), fans.get(2)
        tip = (f"{STATUS_TITLES.get(status, status)}\n"
               f"CPU {fmt_t('CPU')}  GPU {fmt_t('GPU')}\n"
               f"Fans: {f1.rpm if f1 else '-'} / {f2.rpm if f2 else '-'} RPM")
        self._set_tray(status, tip)

    # ------------------------------------------------------------- window
    def show(self) -> None:
        self.root.deiconify()
        T.enable_dark_titlebar(self.root)
        self.root.lift()
        self.root.focus_force()

    def on_close(self) -> None:
        if self.icon is not None:
            self.root.withdraw()            # keep running in the tray
        else:
            if messagebox.askokcancel("Exit", "Stop fan control and hand the fans back to the BIOS?"):
                self.exit()

    def exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        log.info("Exiting on user request")
        self.loop.stop()                    # hands both fans back to the EC first
        icon = self.icon
        if icon is not None:
            # pystray's stop() can block for seconds on some backends; don't hold up exit
            threading.Thread(target=icon.stop, daemon=True).start()
        self.root.destroy()
