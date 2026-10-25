"""The window's pages, besides the Dashboard: Fan curves, Sensors, Events and Settings.

Layout follows Windows Task Manager: a big title with a grey line at the right,
a large chart, large-number statistics, and flat dark cards. Every page offers the
same small interface to the window:

    load(cfg)            show these saved settings
    collect(cfg)         write the page's (possibly unsaved) values into cfg
    update_view(s, cfg)  show the newest Snapshot (called while the page is visible)

Pages never touch the hardware.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

from . import config as cfgmod
from . import theme as T
from .control import step_vertices
from .dashboard import SS, PlotImage, round_rect
from .events import FILTERS, LogBuffer, filtered
from .history import History
from .thinkpad import SENSORS
from .widgets import FlatScale, Stat

log = logging.getLogger(__name__)

SENSOR_REG = {"CPU": 0x78, "GPU": 0x7B}
CHART_XMIN, CHART_XMAX = 30, 100


def temp_color(value: Optional[float], handover: int) -> str:
    if value is None:
        return T.TEXT2
    return T.CRIT if value >= handover else (T.WARN if value >= handover - 10 else T.TEXT)


def page_header(parent, title: str, right: str = "") -> tuple[tk.Frame, tk.Label]:
    """Big light title at the left, a grey description at the right (like 'Memory ... 32.0 GB DDR5')."""
    f = tk.Frame(parent, bg=T.BG)
    tk.Label(f, text=title, bg=T.BG, fg=T.TEXT, font=T.font(20), anchor="w").pack(side="left")
    r = tk.Label(f, text=right, bg=T.BG, fg=T.TEXT2, font=T.font(11), anchor="e")
    r.pack(side="right", anchor="s", pady=(0, T.px(4)))
    return f, r


def card_frame(parent, padx: int = 14, pady: int = 12) -> tk.Frame:
    return tk.Frame(parent, bg=T.SURFACE, highlightthickness=1, highlightbackground=T.BORDER,
                    highlightcolor=T.BORDER, padx=T.px(padx), pady=T.px(pady))


def _dashed_v(d, x: float, y0: float, y1: float, color: str, dash: float, duty: float = 0.55, width: float = 1.0):
    rgbc = T.rgb(color)
    y = y0
    while y < y1:
        d.line([(x * SS, y * SS), (x * SS, min(y + dash * duty, y1) * SS)], fill=rgbc, width=max(1, int(width * SS)))
        y += dash


# ------------------------------------------------------------------ the chart
class CurveChart(tk.Canvas):
    """Level against temperature. Shows the step-up curve, the path it follows when the
    temperature falls (shifted left by the deadband), the hand-over temperature, and a
    marker for where the laptop is right now."""

    def __init__(self, master) -> None:
        super().__init__(master, bg=T.BG, highlightthickness=0, height=T.px(150))
        self._plot: Optional[PlotImage] = None
        self._key = None
        self._data = None
        self.bind("<Configure>", self._layout)

    def set(self, steps, deadband: int, handover: int, temp: Optional[int], level: Optional[int], label: str) -> None:
        key = (tuple(map(tuple, steps)), deadband, handover, temp, level, label)
        if key == self._key:
            return
        self._key = key
        self._data = (list(map(tuple, steps)), deadband, handover, temp, level, label)
        self._paint()

    # layout: a rounded card with the plot image inside and tick labels around it
    def _layout(self, _e=None) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 200 or h < 100:
            self._plot = None
            return
        self._x0, self._y0 = T.px(40), T.px(34)
        self._x1, self._y1 = w - T.px(16), h - T.px(30)
        round_rect(self, 1, 1, w - 1, h - 1, T.px(8), fill=T.SURFACE, outline=T.BORDER)
        self._plot = PlotImage(self._x1 - self._x0, self._y1 - self._y0, T.SURFACE)
        self.create_image(self._x0, self._y0, anchor="nw", image=self._plot.photo)
        self._m = T.px(8)                                   # headroom so level 0 and 7 aren't on the edge
        for lvl in range(8):
            self.create_text(self._x0 - T.px(8), self._ypx(lvl) + self._y0, anchor="e", text=str(lvl),
                             fill=T.TEXT3, font=T.font(8))
        for t in range(40, CHART_XMAX + 1, 10):
            self.create_text(self._x0 + self._xpx(t), self._y1 + T.px(5), anchor="n",
                             text=f"{t}°C" if t == CHART_XMAX else str(t), fill=T.TEXT3, font=T.font(8))
        self.create_text(self._x0 - T.px(8), self._y0 - T.px(10), anchor="e", text="level", fill=T.TEXT3, font=T.font(8))
        self._paint()

    def _xpx(self, t: float) -> float:
        return (min(max(t, CHART_XMIN), CHART_XMAX) - CHART_XMIN) / (CHART_XMAX - CHART_XMIN) * self._plot.w

    def _ypx(self, level: float) -> float:
        return self._plot.h - self._m - (level / 7.0) * (self._plot.h - 2 * self._m)

    def _paint(self) -> None:
        if self._plot is None or self._data is None:
            return
        steps, deadband, handover, temp, level, label = self._data
        w, h = self._plot.w, self._plot.h
        up = [(self._xpx(x), self._ypx(y)) for x, y in step_vertices(steps, CHART_XMIN, CHART_XMAX)]
        down = [(self._xpx(x), self._ypx(y)) for x, y in step_vertices(steps, CHART_XMIN, CHART_XMAX, shift=deadband)]
        base = self._ypx(0)
        hx = self._xpx(handover)
        cur = None if temp is None else (self._xpx(temp), self._ypx(level if level is not None else 0))
        dot_color = temp_color(temp, handover)

        def paint(d):
            # fill under the curve, then the fall-back path (dim), then the step-up curve on top
            d.polygon([(x * SS, y * SS) for x, y in up] + [(w * SS, base * SS), (0, base * SS)], fill=T.rgb(T.FILL))
            if deadband > 0:
                d.line([(x * SS, y * SS) for x, y in down], fill=T.rgb(T.GREY_DIM), width=max(1, int(1.2 * SS)),
                       joint="curve")
            d.line([(x * SS, y * SS) for x, y in up], fill=T.rgb(T.LINE), width=max(1, int(2 * SS)), joint="curve")
            _dashed_v(d, hx, 0, h, T.WARN, T.px(6))
            if cur is not None:
                _dashed_v(d, cur[0], 0, h, T.GREY, T.px(4), duty=0.5)
                r = T.px(5)
                d.ellipse(((cur[0] - r - 2) * SS, (cur[1] - r - 2) * SS, (cur[0] + r + 2) * SS, (cur[1] + r + 2) * SS),
                          fill=T.rgb(T.SURFACE))
                d.ellipse(((cur[0] - r) * SS, (cur[1] - r) * SS, (cur[0] + r) * SS, (cur[1] + r) * SS),
                          fill=T.rgb(dot_color))
        self._plot.render(paint)

        self.delete("dyn")
        x0, y0 = self._x0, self._y0
        self.create_text(x0 + hx - T.px(5), y0 + T.px(18), anchor="ne", fill=T.WARN, font=T.font(8),
                         text=f"hand-over {handover}°C", tags="dyn")
        if cur is not None:
            text = f"{label} {temp}°C  ›  level {level if level is not None else 0}"
            # beside the dashed line, never on top of it; on the left of it near the right edge
            left_of_line = cur[0] > w * 0.62
            above = cur[1] > T.px(34)                            # enough room above the dot, otherwise go below it
            anchor = ("s" if above else "n") + ("e" if left_of_line else "w")
            self.create_text(x0 + cur[0] + (-T.px(10) if left_of_line else T.px(10)),
                             y0 + cur[1] + (-T.px(8) if above else T.px(8)), anchor=anchor, fill=dot_color,
                             font=T.font(9, True), text=text, tags="dyn")
        # legend in the strip above the plot
        lx, ly = x0, T.px(17)
        self.create_line(lx, ly, lx + T.px(18), ly, fill=T.LINE, width=T.px(2), tags="dyn")
        self.create_text(lx + T.px(26), ly, anchor="w", fill=T.TEXT2, font=T.font(9), tags="dyn",
                         text="fans step up here")
        if deadband > 0:
            lx2 = lx + T.px(170)
            self.create_line(lx2, ly, lx2 + T.px(18), ly, fill=T.GREY, width=T.px(1), tags="dyn")
            self.create_text(lx2 + T.px(26), ly, anchor="w", fill=T.TEXT2, font=T.font(9), tags="dyn",
                             text=f"and step down here ({deadband}°C lower)")


class CurveCard(tk.Canvas):
    """One entry of the list on the left: a small picture of the curve, its name and the live state."""

    def __init__(self, master, name: str, on_click: Callable[[str], None]) -> None:
        super().__init__(master, bg=T.BG, highlightthickness=0, height=T.px(72), cursor="hand2")
        self.name, self.on_click = name, on_click
        self.selected = False
        self._hover = False
        self._sub, self._sub_color = "-", T.TEXT2
        self._thumb: Optional[PlotImage] = None
        self._data = None
        self._key = None
        self.bind("<Configure>", lambda _e: self._layout())
        self.bind("<Button-1>", lambda _e: self.on_click(self.name))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._layout()

    def set_selected(self, v: bool) -> None:
        self.selected = v
        self._layout()

    def set(self, steps, temp: Optional[int], level: Optional[int], handover: int) -> None:
        key = (tuple(map(tuple, steps)), temp, level, handover)
        sub = "-" if temp is None else f"{temp}°C  ›  level {level if level is not None else 0}"
        color = temp_color(temp, handover)
        if key == self._key and sub == self._sub:
            return
        self._key, self._sub, self._sub_color = key, sub, color
        self._data = (list(map(tuple, steps)), temp, level)
        self._layout()

    def _layout(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 40:
            return
        if self.selected or self._hover:
            round_rect(self, T.px(2), T.px(2), w - T.px(2), h - T.px(2), T.px(5),
                       fill=T.SURFACE_HI if self.selected else T.SURFACE, outline="")
        tw, th = T.px(64), T.px(44)
        tx, ty = T.px(12), (h - th) // 2
        self._thumb = PlotImage(tw, th, T.SURFACE)
        self.create_rectangle(tx - 1, ty - 1, tx + tw, ty + th, outline=T.BORDER, fill=T.SURFACE)
        self.create_image(tx, ty, anchor="nw", image=self._thumb.photo)
        if self._data:
            steps, temp, level = self._data
            pts = [((x - CHART_XMIN) / (CHART_XMAX - CHART_XMIN) * tw, th - 4 - y / 7.0 * (th - 8))
                   for x, y in step_vertices(steps, CHART_XMIN, CHART_XMAX)]
            base = th - 4

            def paint(d):
                d.polygon([(x * SS, y * SS) for x, y in pts] + [(tw * SS, base * SS), (0, base * SS)],
                          fill=T.rgb(T.FILL))
                d.line([(x * SS, y * SS) for x, y in pts], fill=T.rgb(T.LINE), width=max(1, int(1.5 * SS)))
                if temp is not None:
                    cx = (min(max(temp, CHART_XMIN), CHART_XMAX) - CHART_XMIN) / (CHART_XMAX - CHART_XMIN) * tw
                    cy = th - 4 - (level or 0) / 7.0 * (th - 8)
                    d.ellipse(((cx - 2.5) * SS, (cy - 2.5) * SS, (cx + 2.5) * SS, (cy + 2.5) * SS), fill=T.rgb(T.TEXT))
            self._thumb.render(paint)
        self.create_text(tx + tw + T.px(12), h / 2 - T.px(10), anchor="w", text=f"{self.name} curve",
                         fill=T.TEXT, font=T.font(11))
        self.create_text(tx + tw + T.px(12), h / 2 + T.px(10), anchor="w", text=self._sub, fill=self._sub_color,
                         font=T.font(9))


# ----------------------------------------------------------------- the editor
class CurveEditor(tk.Frame):
    """Rows of  'At [slider] °C  ›  level [n]',  kept in order (temperatures rise, levels never fall)."""

    def __init__(self, master, on_change: Callable[[], None]) -> None:
        super().__init__(master, bg=T.BG)
        self.on_change = on_change
        self.rows: list[tuple[tk.IntVar, tk.IntVar]] = []
        self._building = False
        self.columnconfigure(1, weight=1)

    def load(self, steps: list[list[int]]) -> None:
        self._building = True
        for w in self.winfo_children():
            w.destroy()
        self.rows.clear()
        lo, hi = cfgmod.TEMP_RANGE
        for i, (temp, level) in enumerate(steps):
            lv, tv = tk.IntVar(value=level), tk.IntVar(value=temp)
            tk.Label(self, text="At", bg=T.BG, fg=T.TEXT2, font=T.font(9)).grid(row=i, column=0, padx=(0, 6))
            FlatScale(self, lo, hi, tv, length=300, unit="°C",
                      command=lambda _v, idx=i: self._slid(idx)).grid(row=i, column=1, sticky="ew")
            tk.Label(self, text="›  level", bg=T.BG, fg=T.TEXT2, font=T.font(9)).grid(row=i, column=2, padx=(12, 6))
            ttk.Spinbox(self, from_=0, to=7, width=3, textvariable=lv, state="readonly",
                        command=lambda idx=i: self._leveled(idx)).grid(row=i, column=3)
            self.rows.append((lv, tv))
        self._building = False

    def _slid(self, idx: int) -> None:
        if self._building:
            return
        lo = self.rows[idx - 1][1].get() + 1 if idx > 0 else cfgmod.TEMP_RANGE[0]
        hi = self.rows[idx + 1][1].get() - 1 if idx + 1 < len(self.rows) else cfgmod.TEMP_RANGE[1]
        v = self.rows[idx][1].get()
        if v < lo or v > hi:
            self.rows[idx][1].set(max(lo, min(hi, v)))
        self.on_change()

    def _leveled(self, idx: int) -> None:
        if self._building:
            return
        lo = self.rows[idx - 1][0].get() if idx > 0 else 0
        hi = self.rows[idx + 1][0].get() if idx + 1 < len(self.rows) else 7
        v = self.rows[idx][0].get()
        if v < lo or v > hi:
            self.rows[idx][0].set(max(lo, min(hi, v)))
        self.on_change()

    def steps(self) -> list[list[int]]:
        return [[tv.get(), lv.get()] for lv, tv in self.rows]


# ------------------------------------------------------------- Fan curves page
class CurvesPage(tk.Frame):
    NAMES = ("CPU", "GPU")

    def __init__(self, master, on_dirty: Callable[[], None], params: Callable[[], tuple[int, int]]) -> None:
        super().__init__(master, bg=T.BG)
        self.on_dirty, self.params = on_dirty, params          # params() -> (deadband, handover) as currently shown
        self.current = "CPU"
        self._live: dict[str, tuple[Optional[int], Optional[int]]] = {n: (None, None) for n in self.NAMES}

        left = tk.Frame(self, bg=T.BG, width=T.px(250))
        left.grid(row=0, column=0, sticky="ns", padx=(0, T.px(10)))
        left.pack_propagate(False)
        self.cards = {n: CurveCard(left, n, self.select) for n in self.NAMES}
        for c in self.cards.values():
            c.pack(fill="x", pady=(0, T.px(4)))
        tk.Label(left, text="Both fans always get the higher of the two curves' levels.", bg=T.BG, fg=T.TEXT3,
                 font=T.font(8), wraplength=T.px(220), justify="left").pack(anchor="w", padx=T.px(10), pady=(T.px(10), 0))

        right = tk.Frame(self, bg=T.BG)
        right.grid(row=0, column=1, sticky="nsew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.title_hdr, self.title_right = page_header(right, "CPU curve", "")
        self.title_hdr.pack(fill="x", pady=(0, T.px(6)))
        self.chart = CurveChart(right)
        self.chart.pack(fill="both", expand=True)

        stats = tk.Frame(right, bg=T.BG)
        stats.pack(fill="x", pady=(T.px(12), T.px(8)))
        self.s_now, self.s_asks = Stat(stats, "Now"), Stat(stats, "Curve asks for")
        self.s_off, self.s_hand = Stat(stats, "Fans stop below"), Stat(stats, "Hand-over at")
        for i, st in enumerate((self.s_now, self.s_asks, self.s_off, self.s_hand)):
            st.grid(row=0, column=i, sticky="w", padx=(0, T.px(36)))

        tk.Label(right, text="Steps: the fans go up to the level as soon as the temperature reaches the slider.",
                 bg=T.BG, fg=T.TEXT2, font=T.font(9), anchor="w").pack(fill="x", pady=(T.px(4), T.px(4)))
        self.editors = {n: CurveEditor(right, self._edited) for n in self.NAMES}
        self.select("CPU")

    def select(self, name: str) -> None:
        self.current = name
        for n, c in self.cards.items():
            c.set_selected(n == name)
        for n, ed in self.editors.items():
            if n == name:
                ed.pack(fill="x")
            else:
                ed.pack_forget()
        self.title_hdr.winfo_children()[0].configure(text=f"{name} curve")
        self.title_right.configure(text=f"EC sensor 0x{SENSOR_REG[name]:02X}")
        self._redraw()

    def _edited(self) -> None:
        self.on_dirty()
        self._redraw()

    # ------------------------------------------------------------- data
    def load(self, cfg: dict) -> None:
        self.editors["CPU"].load(cfg["cpu_curve"])
        self.editors["GPU"].load(cfg["gpu_curve"])
        self._redraw()

    def collect(self, cfg: dict) -> None:
        cfg.update(cpu_curve=self.editors["CPU"].steps(), gpu_curve=self.editors["GPU"].steps())

    def update_view(self, s, cfg: dict) -> None:
        t = s.temps or {}
        self._live = {"CPU": (t.get("CPU"), s.cpu_level), "GPU": (t.get("GPU"), s.gpu_level)}
        self._redraw()

    def _redraw(self) -> None:
        deadband, handover = self.params()
        for n in self.NAMES:
            temp, level = self._live[n]
            self.cards[n].set(self.editors[n].steps(), temp, level, handover)
        steps = self.editors[self.current].steps()
        temp, level = self._live[self.current]
        self.chart.set(steps, deadband, handover, temp, level, self.current)
        self.s_now.set("-" if temp is None else f"{temp}°C", temp_color(temp, handover))
        self.s_asks.set("-" if temp is None else f"level {level if level is not None else 0}")
        first = steps[0][0] if steps else 0
        self.s_off.set(f"{first - deadband}°C")
        self.s_hand.set(f"{handover}°C", T.WARN)


# ----------------------------------------------------------------- Settings page
class SettingsPage(tk.Frame):
    def __init__(self, master, on_dirty: Callable[[], None], about: str) -> None:
        super().__init__(master, bg=T.BG)
        self.on_dirty = on_dirty
        self.v = {k: tk.IntVar() for k in ("deadband_c", "step_down_delay_s", "handover_c", "resume_c",
                                           "cycle_s", "stall_check_s")}
        self.v_hwinfo = tk.BooleanVar()
        hdr, _ = page_header(self, "Settings", "behavior and safety")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, T.px(10)))
        self.columnconfigure((0, 1), weight=1, uniform="cols")
        L = cfgmod.LIMITS

        g1 = self._group(0, 0, "Fan control")
        self._slider(g1, "Step-down deadband (°C)", "deadband_c", *L["deadband_c"],
                     "Fans step up at once, but only step down once the temperature is this far below the step. "
                     "Stops hunting.")
        self._slider(g1, "Step-down delay (s)", "step_down_delay_s", *L["step_down_delay_s"],
                     "The temperature must stay below the deadband this long before the fans step down. Rides out "
                     "short dips instead of cycling.")
        self._slider(g1, "Update every (s)", "cycle_s", *L["cycle_s"],
                     "How often temperatures are read. 2-5 s is plenty; faster only adds EC traffic.")

        g2 = self._group(0, 1, "Safety")
        self._slider(g2, "Hand to EC at (°C)", "handover_c", *L["handover_c"],
                     "If any monitored sensor reaches this, the EC's own curve takes over both fans.")
        self._slider(g2, "Take back control at (°C)", "resume_c", *L["resume_c"],
                     "Your curve resumes once everything is back below this.")
        self._slider(g2, "Fan-stall alarm after (s)", "stall_check_s", *L["stall_check_s"],
                     "Red alarm if a fan is commanded on but reads 0 RPM for this long.")

        g3 = self._group(1, 0, "Compatibility")
        ttk.Checkbutton(g3, text="Keep running while HWiNFO is open", variable=self.v_hwinfo, style="Card.TCheckbutton",
                        command=self.on_dirty).pack(anchor="w")
        tk.Label(g3, text="Only tick this after turning off EC support in HWiNFO's settings. Otherwise HWiNFO's EC "
                          "reads collide with this app's and corrupt both, so by default the app hands the fans to "
                          "the EC and pauses while HWiNFO runs.", bg=T.SURFACE, fg=T.TEXT2, font=T.font(9),
                 wraplength=T.px(380), justify="left").pack(anchor="w", pady=(T.px(4), 0))

        g4 = self._group(1, 1, "About")
        tk.Label(g4, text=about, bg=T.SURFACE, fg=T.TEXT2, font=T.font(9), wraplength=T.px(380),
                 justify="left").pack(anchor="w")
        self.rowconfigure(2, weight=1)

    def _group(self, row: int, col: int, title: str) -> tk.Frame:
        outer = card_frame(self)
        outer.grid(row=1 + row, column=col, sticky="nsew", padx=(0 if col == 0 else T.px(6), T.px(6) if col == 0 else 0),
                   pady=(0, T.px(10)))
        tk.Label(outer, text=title, bg=T.SURFACE, fg=T.TEXT, font=T.font(10, True)).pack(anchor="w", pady=(0, T.px(4)))
        return outer

    def _slider(self, parent, label: str, key: str, lo: int, hi: int, hint: str) -> None:
        tk.Label(parent, text=label, bg=T.SURFACE, fg=T.TEXT, font=T.font(9)).pack(anchor="w", pady=(T.px(6), 0))
        FlatScale(parent, lo, hi, self.v[key], length=300, bg=T.SURFACE,
                  command=lambda _v: self.on_dirty()).pack(fill="x")
        tk.Label(parent, text=hint, bg=T.SURFACE, fg=T.TEXT2, font=T.font(8), wraplength=T.px(380),
                 justify="left").pack(anchor="w")

    # the curve chart reads these live, so it follows unsaved edits too
    def params(self) -> tuple[int, int]:
        return self.v["deadband_c"].get(), self.v["handover_c"].get()

    def load(self, cfg: dict) -> None:
        for key, var in self.v.items():
            var.set(cfg[key])
        self.v_hwinfo.set(cfg["allow_hwinfo"])

    def collect(self, cfg: dict) -> None:
        cfg.update({k: v.get() for k, v in self.v.items()}, allow_hwinfo=self.v_hwinfo.get())

    def update_view(self, s, cfg: dict) -> None:
        pass


# ------------------------------------------------------------------ Sensors page
class SensorsPage(tk.Frame):
    COLUMNS = ("Sensor", "Register", "Now", "Lowest", "Highest", "Used for", "Ignore")

    def __init__(self, master, history: History, on_dirty: Callable[[], None]) -> None:
        super().__init__(master, bg=T.BG)
        self.history, self.on_dirty = history, on_dirty
        self.hdr, self.hot_lbl = page_header(self, "Sensors", "")
        self.hdr.pack(fill="x", pady=(0, T.px(10)))
        card = card_frame(self, pady=8)
        card.pack(fill="x")
        for c, w in enumerate((3, 2, 2, 2, 2, 4, 2)):
            card.columnconfigure(c, weight=w, uniform="sens")
        for c, name in enumerate(self.COLUMNS):
            tk.Label(card, text=name, bg=T.SURFACE, fg=T.TEXT2, font=T.font(9), anchor="w").grid(
                row=0, column=c, sticky="w", pady=(0, T.px(6)))
        self.rows: dict[str, dict] = {}
        self.vars: dict[str, tk.BooleanVar] = {}
        for i, (reg, name) in enumerate(SENSORS, start=1):
            tk.Frame(card, bg=T.BORDER, height=1).grid(row=2 * i - 1, column=0, columnspan=len(self.COLUMNS), sticky="ew")
            labels = {}
            for c, key in enumerate(("name", "reg", "now", "low", "high", "role")):
                lbl = tk.Label(card, text="", bg=T.SURFACE, fg=T.TEXT, font=T.font(9), anchor="w")
                lbl.grid(row=2 * i, column=c, sticky="w", pady=T.px(5))
                labels[key] = lbl
            labels["name"].configure(text=name)
            labels["reg"].configure(text=f"0x{reg:02X}", fg=T.TEXT3)
            var = tk.BooleanVar()
            if name in cfgmod.PROTECTED_SENSORS:            # can't be ignored: say so instead of a dead checkbox
                tk.Label(card, text="always on", bg=T.SURFACE, fg=T.TEXT3, font=T.font(9)).grid(
                    row=2 * i, column=len(self.COLUMNS) - 1, sticky="w")
            else:
                ttk.Checkbutton(card, variable=var, style="Card.TCheckbutton", command=self.on_dirty).grid(
                    row=2 * i, column=len(self.COLUMNS) - 1, sticky="w")
            self.rows[name] = labels
            self.vars[name] = var
        tk.Label(self, text="Lowest and highest are since the program started. Sensors you ignore aren't read at all "
                            "(less EC traffic) and don't count toward the safety hand-over. CPU and GPU are always "
                            "used.", bg=T.BG, fg=T.TEXT2, font=T.font(9), wraplength=T.px(700), justify="left").pack(
            anchor="w", pady=(T.px(10), 0))

    def load(self, cfg: dict) -> None:
        for name, var in self.vars.items():
            var.set(name in cfg["ignore_sensors"])

    def collect(self, cfg: dict) -> None:
        cfg["ignore_sensors"] = [n for n, v in self.vars.items() if v.get()]

    def update_view(self, s, cfg: dict) -> None:
        handover = int(cfg.get("handover_c", 92))
        applied_ignore = set(cfg.get("ignore_sensors", []))
        temps, raw = s.temps or {}, s.raw or {}
        hot = s.hottest
        self.hot_lbl.configure(text=f"hottest now: {hot[0]} {hot[1]}°C" if hot else "")
        for name, lbl in self.rows.items():
            ignored = name in applied_ignore and name not in cfgmod.PROTECTED_SENSORS
            value = temps.get(name)
            rng = self.history.sensor_range(name)
            if ignored:
                now, role, dim = "not read", "-", True
            elif value is None:
                now = "no data" if raw else "-"
                role, dim = "waiting for a reading" if not raw else "safety hand-over (no data)", bool(raw)
            else:
                now = f"{value}°C"
                role = {"CPU": "CPU curve", "GPU": "GPU curve"}.get(name, "safety hand-over")
                dim = False
            base = T.TEXT3 if dim else T.TEXT
            lbl["name"].configure(fg=base)
            lbl["now"].configure(text=now, fg=T.TEXT3 if dim else temp_color(value, handover))
            lbl["low"].configure(text=f"{rng[0]}°C" if rng else "-", fg=T.TEXT3 if dim else T.TEXT2)
            lbl["high"].configure(text=f"{rng[1]}°C" if rng else "-", fg=T.TEXT3 if dim else T.TEXT2)
            lbl["role"].configure(text=role, fg=T.TEXT3 if dim else T.TEXT2)


# ------------------------------------------------------------------- Events page
class EventsPage(tk.Frame):
    MAX_ROWS = 500

    def __init__(self, master, buffer: LogBuffer, log_dir: Optional[Path],
                 make_report: Callable[[], str]) -> None:
        super().__init__(master, bg=T.BG)
        self.buffer, self.log_dir, self.make_report = buffer, log_dir, make_report
        self.flt = "all"
        self._shown = (-1, "")
        self._toast_after = None
        self.hdr, self.count_lbl = page_header(self, "Events", "")
        self.hdr.pack(fill="x", pady=(0, T.px(8)))

        bar = tk.Frame(self, bg=T.BG)
        bar.pack(fill="x", pady=(0, T.px(8)))
        self._toggles: dict[str, tk.Label] = {}
        for key, text in FILTERS:
            lbl = tk.Label(bar, text=text, bg=T.BG, fg=T.TEXT2, font=T.font(9), cursor="hand2", padx=T.px(8))
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda _e, k=key: self.set_filter(k))
            self._toggles[key] = lbl
        ttk.Button(bar, text="Copy report", style="Accent.TButton", command=self.copy_report).pack(side="right")
        ttk.Button(bar, text="Open log folder", command=self.open_folder).pack(side="right", padx=(0, T.px(8)))
        self.toast = tk.Label(bar, text="", bg=T.BG, fg=T.LINE, font=T.font(9))
        self.toast.pack(side="right", padx=T.px(12))

        box = tk.Frame(self, bg=T.BORDER, padx=1, pady=1)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("time", "level", "message"), show="headings", selectmode="extended")
        for col, text, width, stretch in (("time", "Time", 130, False), ("level", "Level", 80, False),
                                          ("message", "Message", 600, True)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=T.px(width), minwidth=T.px(60), stretch=stretch, anchor="w")
        self.tree.tag_configure("warning", foreground=T.WARN)
        self.tree.tag_configure("error", foreground=T.CRIT)
        self.tree.tag_configure("info", foreground=T.TEXT)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Control-c>", lambda _e: self.copy_selected())
        self.set_filter("all")

    def set_filter(self, key: str) -> None:
        self.flt = key
        for k, lbl in self._toggles.items():
            lbl.configure(fg=T.LINE if k == key else T.TEXT2, font=T.font(9, True) if k == key else T.font(9))
        self._refresh(force=True)

    def load(self, cfg: dict) -> None:
        pass

    def collect(self, cfg: dict) -> None:
        pass

    def update_view(self, s, cfg: dict) -> None:
        self._refresh()

    def _refresh(self, force: bool = False) -> None:
        state = (self.buffer.version, self.flt)
        if not force and state == self._shown:
            return
        self._shown = state
        entries = filtered(self.buffer.snapshot(), self.flt)[-self.MAX_ROWS:]
        self.tree.delete(*self.tree.get_children())
        today = time.strftime("%Y-%m-%d")
        for e in reversed(entries):                                   # newest first
            stamp = time.strftime("%H:%M:%S" if time.strftime("%Y-%m-%d", time.localtime(e.t)) == today
                                  else "%m-%d %H:%M:%S", time.localtime(e.t))
            tag = {"WARNING": "warning", "ERROR": "error", "CRITICAL": "error"}.get(e.level, "info")
            self.tree.insert("", "end", values=(stamp, e.level.capitalize(), e.headline), tags=(tag,))
        self.count_lbl.configure(text=f"{len(entries)} shown" if entries else "nothing logged yet")

    # --------------------------------------------------------------- actions
    def _say(self, text: str, color: str = T.LINE) -> None:
        self.toast.configure(text=text, fg=color)
        if self._toast_after is not None:
            self.after_cancel(self._toast_after)
        self._toast_after = self.after(7000, lambda: self.toast.configure(text=""))

    def _to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()                                   # keep the text on the clipboard after the call returns

    def copy_report(self) -> None:
        try:
            text = self.make_report()
            self._to_clipboard(text)
        except Exception as e:
            log.exception("could not build or copy the report")
            self._say(f"Could not copy the report: {e}", T.CRIT)
            return
        self._say(f"Report copied ({len(text) / 1024:.1f} KB). Paste it into your GitHub issue.")

    def copy_selected(self) -> None:
        rows = [self.tree.item(i, "values") for i in self.tree.selection()]
        if rows:
            self._to_clipboard("\n".join("  ".join(r) for r in rows))
            self._say(f"{len(rows)} line(s) copied.")

    def open_folder(self) -> None:
        if self.log_dir is None:
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(self.log_dir))           # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(self.log_dir)])
        except Exception:
            self._say(f"Logs are in {self.log_dir}", T.TEXT2)
