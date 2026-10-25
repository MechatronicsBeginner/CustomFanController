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

from . import APP_NAME
from . import config as cfgmod
from . import winutil
from .control import StepCurve
from .service import (ControlLoop, Snapshot, ST_AUTO, ST_ERROR, ST_FAULT, ST_HANDOVER, ST_OK,
                      ST_PAUSED, ST_READONLY, ST_STARTING)
from .thinkpad import SENSORS, describe_level

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


class CurveEditor(ttk.Frame):
    """One row per step: level spinbox + temperature slider, kept in order."""

    def __init__(self, master, title: str, sensor: str, on_change) -> None:
        super().__init__(master, padding=10)
        self.sensor = sensor
        self.on_change = on_change
        self.rows: list[tuple[tk.IntVar, tk.IntVar]] = []
        self._building = False
        ttk.Label(self, text=title, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(self, text="Each row: the fan steps UP to this level as soon as the temperature reaches the slider value.",
                  wraplength=520, foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.body = ttk.Frame(self)
        self.body.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.summary = ttk.Label(self, text="", foreground="#333")
        self.summary.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.columnconfigure(0, weight=1)

    def load(self, steps: list[list[int]]) -> None:
        self._building = True
        for w in self.body.winfo_children():
            w.destroy()
        self.rows.clear()
        for i, (temp, level) in enumerate(steps):
            lv, tv = tk.IntVar(value=level), tk.IntVar(value=temp)
            ttk.Label(self.body, text="Level").grid(row=i, column=0, padx=(0, 4))
            sb = ttk.Spinbox(self.body, from_=0, to=7, width=3, textvariable=lv,
                             command=lambda idx=i: self._leveled(idx), state="readonly")
            sb.grid(row=i, column=1)
            ttk.Label(self.body, text="at").grid(row=i, column=2, padx=4)
            sc = tk.Scale(self.body, from_=cfgmod.TEMP_RANGE[0], to=cfgmod.TEMP_RANGE[1], orient="horizontal",
                          resolution=1, length=330, variable=tv, showvalue=True,
                          command=lambda _v, idx=i: self._slid(idx))
            sc.grid(row=i, column=3, sticky="ew")
            ttk.Label(self.body, text="°C").grid(row=i, column=4, padx=(2, 0))
            self.rows.append((lv, tv))
        self.body.columnconfigure(3, weight=1)
        self._building = False

    def _slid(self, idx: int) -> None:
        if self._building:
            return
        # keep thresholds strictly increasing: clamp between neighbours
        lo = self.rows[idx - 1][1].get() + 1 if idx > 0 else cfgmod.TEMP_RANGE[0]
        hi = self.rows[idx + 1][1].get() - 1 if idx + 1 < len(self.rows) else cfgmod.TEMP_RANGE[1]
        v = self.rows[idx][1].get()
        if v < lo or v > hi:
            self.rows[idx][1].set(max(lo, min(hi, v)))
        self._changed()

    def _leveled(self, idx: int) -> None:
        # levels may not go down as temperature goes up: clamp between neighbours
        lo = self.rows[idx - 1][0].get() if idx > 0 else 0
        hi = self.rows[idx + 1][0].get() if idx + 1 < len(self.rows) else 7
        v = self.rows[idx][0].get()
        if v < lo or v > hi:
            self.rows[idx][0].set(max(lo, min(hi, v)))
        self._changed()

    def _changed(self) -> None:
        if not self._building:
            self.on_change()

    def steps(self) -> list[list[int]]:
        return [[tv.get(), lv.get()] for lv, tv in self.rows]

    def update_summary(self, deadband: int, current: int | None) -> None:
        st = self.steps()
        first = st[0][0] if st else 0
        txt = f"Fan off below {first - deadband}°C (first step {first}°C minus {deadband}°C deadband)."
        if current is not None:
            txt += f"   Now: {self.sensor} {current}°C → level {StepCurve.preview([tuple(s) for s in st], current)}"
        self.summary.configure(text=txt)


class App:
    def __init__(self, root: tk.Tk, loop: ControlLoop, cfg: dict, cfg_path: Path, *,
                 use_tray: bool, start_hidden: bool, title_suffix: str = "") -> None:
        self.root = root
        self.loop = loop
        self.cfg = copy.deepcopy(cfg)
        self.cfg_path = cfg_path
        self.ui_queue: "queue.Queue[str]" = queue.Queue()
        self.icon = None
        self._icon_state = None
        self._exiting = False

        root.title(APP_NAME + title_suffix)
        root.minsize(680, 660)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.load_settings_into_ui(self.cfg)

        if use_tray:
            self._start_tray()
            self._schedule_pin()
        if start_hidden and self.icon is not None:
            root.withdraw()
        self.root.after(300, self._poll)

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        r = self.root
        top = ttk.Frame(r, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=18, height=18, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 16, 16, fill=STATUS_COLORS[ST_STARTING], outline="")
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.status_lbl = ttk.Label(top, text="Starting", font=("Segoe UI", 11, "bold"))
        self.status_lbl.grid(row=0, column=1, sticky="w")
        self.msg_lbl = ttk.Label(top, text="", foreground="#444")
        self.msg_lbl.grid(row=1, column=1, sticky="w")
        top.columnconfigure(1, weight=1)

        modef = ttk.Frame(top)
        modef.grid(row=0, column=2, rowspan=2, sticky="e")
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        ttk.Radiobutton(modef, text="Fan curve", value="curve", variable=self.mode_var,
                        command=self._mode_clicked).pack(side="left", padx=4)
        ttk.Radiobutton(modef, text="EC Auto (BIOS)", value="auto", variable=self.mode_var,
                        command=self._mode_clicked).pack(side="left", padx=4)

        live = ttk.LabelFrame(r, text="Now", padding=(12, 6))
        live.pack(fill="x", padx=12, pady=6)
        self.live: dict[str, ttk.Label] = {}
        rows = [("cpu", "CPU"), ("gpu", "GPU"), ("hot", "Hottest monitored"),
                ("fan1", "Fan 1"), ("fan2", "Fan 2")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(live, text=label + ":").grid(row=i, column=0, sticky="w", padx=(0, 10))
            lbl = ttk.Label(live, text="-", font=("Segoe UI", 10))
            lbl.grid(row=i, column=1, sticky="w")
            self.live[key] = lbl

        nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.cpu_ed = CurveEditor(nb, "CPU curve (EC sensor 0x78)", "CPU", self._dirty)
        self.gpu_ed = CurveEditor(nb, "GPU curve (EC sensor 0x7B)", "GPU", self._dirty)
        nb.add(self.cpu_ed, text="CPU curve")
        nb.add(self.gpu_ed, text="GPU curve")
        nb.add(self._build_behavior(nb), text="Behavior & safety")
        nb.add(self._build_sensors(nb), text="Sensors")

        bottom = ttk.Frame(r, padding=(12, 0, 12, 10))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Both fans always get the higher of the two curves' levels.",
                  foreground="#555").pack(side="left")
        self.dirty_lbl = ttk.Label(bottom, text="", foreground="#b45309")
        self.dirty_lbl.pack(side="left", padx=10)
        ttk.Button(bottom, text="Revert", command=self._revert).pack(side="right")
        ttk.Button(bottom, text="Apply & save", command=self._apply).pack(side="right", padx=6)

    def _slider(self, parent, row, label, var, lo, hi, hint):
        r = row * 2
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="sw", pady=(6, 0))
        tk.Scale(parent, from_=lo, to=hi, orient="horizontal", resolution=1, length=300, variable=var,
                 command=lambda _v: self._dirty()).grid(row=r, column=1, sticky="w", padx=8)
        ttk.Label(parent, text=hint, foreground="#555", wraplength=560).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def _build_behavior(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb, padding=10)
        self.v_deadband = tk.IntVar()
        self.v_delay = tk.IntVar()
        self.v_handover = tk.IntVar()
        self.v_resume = tk.IntVar()
        self.v_cycle = tk.IntVar()
        self.v_stall = tk.IntVar()
        L = cfgmod.LIMITS
        self._slider(f, 0, "Step-down deadband (°C)", self.v_deadband, *L["deadband_c"],
                     "Fans step up immediately, but only step down once the temperature is this far below the step. Stops hunting.")
        self._slider(f, 1, "Step-down delay (s)", self.v_delay, *L["step_down_delay_s"],
                     "Before stepping down, the temperature must stay below the deadband this long without "
                     "interruption. Rides out short dips (loading screens, a bad read) instead of cycling.")
        self._slider(f, 2, "Hand to EC at (°C)", self.v_handover, *L["handover_c"],
                     "If any monitored sensor reaches this, the EC's own curve takes over both fans.")
        self._slider(f, 3, "Take back control at (°C)", self.v_resume, *L["resume_c"],
                     "Your curve resumes once everything is back below this.")
        self._slider(f, 4, "Update every (s)", self.v_cycle, *L["cycle_s"],
                     "How often temperatures are read. 2-5 s is plenty; faster only adds EC traffic.")
        self._slider(f, 5, "Fan-stall alarm after (s)", self.v_stall, *L["stall_check_s"],
                     "Red alarm if a fan is commanded on but reads 0 RPM for this long.")
        self.v_allow_hwinfo = tk.BooleanVar()
        ttk.Checkbutton(f, text="Keep running while HWiNFO is open", variable=self.v_allow_hwinfo,
                        command=self._dirty).grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="Only tick this after turning off EC support in HWiNFO's settings. Otherwise "
                          "HWiNFO's EC reads collide with this app's and corrupt both, so by default the app "
                          "hands the fans to the EC and pauses while HWiNFO runs.",
                  foreground="#555", wraplength=560).grid(row=13, column=0, columnspan=2, sticky="w")
        return f

    def _build_sensors(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb, padding=10)
        ttk.Label(f, text="Ignored sensors aren't read at all (less EC traffic) and don't count toward "
                          "the safety hand-over. CPU and GPU are always used.", foreground="#555", wraplength=560).grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        for c, h in enumerate(("Sensor", "Register", "Raw", "Used value", "Ignore")):
            ttk.Label(f, text=h, font=("Segoe UI", 9, "bold")).grid(row=1, column=c, sticky="w", padx=6)
        self.sensor_rows: dict[str, tuple[ttk.Label, ttk.Label, tk.BooleanVar]] = {}
        for i, (reg, name) in enumerate(SENSORS, start=2):
            ttk.Label(f, text=name).grid(row=i, column=0, sticky="w", padx=6)
            ttk.Label(f, text=f"0x{reg:02X}").grid(row=i, column=1, sticky="w", padx=6)
            raw = ttk.Label(f, text="-")
            raw.grid(row=i, column=2, sticky="w", padx=6)
            used = ttk.Label(f, text="-")
            used.grid(row=i, column=3, sticky="w", padx=6)
            var = tk.BooleanVar()
            cb = ttk.Checkbutton(f, variable=var, command=self._dirty)
            if name in cfgmod.PROTECTED_SENSORS:
                cb.state(["disabled"])
            cb.grid(row=i, column=4, padx=6)
            self.sensor_rows[name] = (raw, used, var)
        return f

    # ----------------------------------------------------- settings <-> UI
    def load_settings_into_ui(self, cfg: dict) -> None:
        self.cpu_ed.load(cfg["cpu_curve"])
        self.gpu_ed.load(cfg["gpu_curve"])
        self.v_deadband.set(cfg["deadband_c"])
        self.v_delay.set(cfg["step_down_delay_s"])
        self.v_handover.set(cfg["handover_c"])
        self.v_resume.set(cfg["resume_c"])
        self.v_cycle.set(cfg["cycle_s"])
        self.v_stall.set(cfg["stall_check_s"])
        self.v_allow_hwinfo.set(cfg["allow_hwinfo"])
        for name, (_r, _u, var) in self.sensor_rows.items():
            var.set(name in cfg["ignore_sensors"])
        self.mode_var.set(cfg["mode"])
        self.dirty_lbl.configure(text="")

    def settings_from_ui(self) -> dict:
        cfg = copy.deepcopy(self.cfg)
        cfg.update(cpu_curve=self.cpu_ed.steps(), gpu_curve=self.gpu_ed.steps(),
                   deadband_c=self.v_deadband.get(), step_down_delay_s=self.v_delay.get(),
                   handover_c=self.v_handover.get(),
                   resume_c=self.v_resume.get(), cycle_s=self.v_cycle.get(),
                   stall_check_s=self.v_stall.get(), mode=self.mode_var.get(),
                   allow_hwinfo=self.v_allow_hwinfo.get(),
                   ignore_sensors=[n for n, (_r, _u, v) in self.sensor_rows.items() if v.get()])
        return cfg

    def _dirty(self) -> None:
        self.dirty_lbl.configure(text="Unsaved changes")

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
        self.live["cpu"].configure(text=f"{fmt_t('CPU')}   (curve asks for level {s.cpu_level})")
        self.live["gpu"].configure(text=f"{fmt_t('GPU')}   (curve asks for level {s.gpu_level})")
        self.live["hot"].configure(text=f"{s.hottest[0]} {s.hottest[1]}°C" if s.hottest else "-")
        fans = {f.fan: f for f in s.fans}
        for n in (1, 2):
            f = fans.get(n)
            warn = "   ⚠ not spinning" if n in s.stalled else ""
            self.live[f"fan{n}"].configure(
                text="-" if f is None else f"level {describe_level(f.level)}   ·   {f.rpm} RPM{warn}")

        for name, (raw_lbl, used_lbl, _v) in self.sensor_rows.items():
            raw = (s.raw or {}).get(name)
            used = t.get(name)
            if raw is None and s.raw and name in self.cfg["ignore_sensors"]:
                raw_lbl.configure(text="not read (ignored)")
                used_lbl.configure(text="-")
                continue
            raw_lbl.configure(text="-" if raw is None else (f"0x{raw:02X} (no data)" if raw in (0, 0x80) or raw > 0x80 else f"{raw}"))
            used_lbl.configure(text="-" if used is None else f"{used}°C")

        db = self.v_deadband.get()
        self.cpu_ed.update_summary(db, t.get("CPU"))
        self.gpu_ed.update_summary(db, t.get("GPU"))

        f1, f2 = fans.get(1), fans.get(2)
        tip = (f"{STATUS_TITLES.get(status, status)}\n"
               f"CPU {fmt_t('CPU')}  GPU {fmt_t('GPU')}\n"
               f"Fans: {f1.rpm if f1 else '-'} / {f2.rpm if f2 else '-'} RPM")
        self._set_tray(status, tip)

    # ------------------------------------------------------------- window
    def show(self) -> None:
        self.root.deiconify()
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
