"""The Dashboard tab: cards, scrolling history charts and session statistics.

Everything is drawn with Tk canvases plus Pillow (already needed for the tray
icon); there is no matplotlib, so memory use stays small. Nothing here touches
the hardware: it only reads Snapshots and the History.

Why the charts feel smooth
--------------------------
The laptop is only polled every few seconds, and polling it faster would mean
more traffic on the shared embedded controller (which is exactly what
corrupted readings earlier). So the charts do not step when a reading
arrives. They draw the recent past *slightly behind real time* (one cycle
plus a little), which means the next reading is always already known, and the
line between two readings can be interpolated exactly. The picture then
scrolls continuously and ramps between readings. The numbers in the cards
always show the newest real reading.

Lines are drawn at twice the size and averaged down, which gives anti-aliased
edges like Task Manager; Tk's own canvas lines are not anti-aliased on Windows.
Charts only redraw while they are on screen, so a window hidden in the tray
costs nothing.
"""
from __future__ import annotations

import logging
import time
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageTk

from . import theme as T
from .history import History, Sample, clip_segment, describe, split_segments

log = logging.getLogger(__name__)

SS = 2                                          # supersampling factor for anti-aliasing
_BOX = getattr(getattr(Image, "Resampling", Image), "BOX")
SPARK_WINDOW_S = 300.0
STATUS_WORDS = {            # status -> (short word for the Controller card, colour)
    "ok": ("On curve", T.TEXT), "auto": ("EC auto", T.TEXT), "starting": ("Starting", T.TEXT2),
    "readonly": ("Read-only", T.TEXT), "handover": ("EC in control", T.WARN), "paused": ("Paused", T.WARN),
    "fault": ("Fault", T.CRIT), "error": ("No EC access", T.CRIT),
}


# ----------------------------------------------------------------- helpers
def fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m} min"
    return f"{s} s"


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def round_rect(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class PlotImage:
    """A plot area drawn at SS x and averaged down, then pasted into a PhotoImage."""

    def __init__(self, w: int, h: int, bg: str) -> None:
        self.bg = T.rgb(bg)
        self.w, self.h = max(8, int(w)), max(8, int(h))
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (self.w, self.h), self.bg))

    def render(self, painter: Callable[[ImageDraw.ImageDraw], None]) -> None:
        big = Image.new("RGB", (self.w * SS, self.h * SS), self.bg)
        painter(ImageDraw.Draw(big))
        self.photo.paste(big.resize((self.w, self.h), _BOX))


def fill_runs(d: ImageDraw.ImageDraw, runs, color: str, base: float) -> None:
    rgbc = T.rgb(color)
    for run in runs:
        if len(run) >= 2:
            pts = [(x * SS, y * SS) for x, y in run]
            d.polygon(pts + [(pts[-1][0], base * SS), (pts[0][0], base * SS)], fill=rgbc)


def line_runs(d: ImageDraw.ImageDraw, runs, color: str, width: float) -> None:
    rgbc = T.rgb(color)
    lw = max(1, int(round(width * SS)))
    for run in runs:
        pts = [(x * SS, y * SS) for x, y in run]
        if len(pts) >= 2:
            d.line(pts, fill=rgbc, width=lw, joint="curve")
        elif len(pts) == 1:
            x, y = pts[0]
            r = lw / 2
            d.ellipse((x - r, y - r, x + r, y + r), fill=rgbc)


def to_runs(samples: list[Sample], get: Callable[[Sample], Optional[float]], t0: float, t1: float,
            gap_s: float, w: float, h: float, ymin: float, ymax: float) -> list[list[tuple[float, float]]]:
    """Samples -> pixel polylines, one per unbroken run, clipped to [t0, t1]."""
    span_t = max(t1 - t0, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    runs = []
    for seg in split_segments([(s.t, get(s)) for s in samples], gap_s):
        c = clip_segment(seg, t0, t1)
        if not c:
            continue
        if len(c) > 2 * w:                                   # more points than pixels: thin them out
            step = int(len(c) // (2 * w)) + 1
            c = c[::step] + [c[-1]]
        runs.append([((t - t0) / span_t * w,
                      h - (min(max(v, ymin), ymax) - ymin) / span_y * h) for t, v in c])
    return runs


# -------------------------------------------------------------------- card
class Card(tk.Canvas):
    """Title, big number, one line of detail and a 5-minute sparkline."""

    def __init__(self, master, title: str, *, meter: bool = False) -> None:
        super().__init__(master, bg=T.BG, highlightthickness=0, height=T.px(116))
        self.title = title
        self.meter = meter
        self._value, self._sub, self._color, self._sub_color = "-", "", T.TEXT, T.TEXT2
        self._level: Optional[int] = None
        self._series = None
        self._plot: Optional[PlotImage] = None
        self.bind("<Configure>", self._layout)

    def _layout(self, _e=None) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 40:
            return
        round_rect(self, 1, 1, w - 1, h - 1, T.px(8), fill=T.SURFACE, outline=T.BORDER)
        self.create_text(T.px(12), T.px(9), anchor="nw", text=self.title, fill=T.TEXT2, font=T.font(9))
        self._vid = self.create_text(T.px(12), T.px(23), anchor="nw", text=self._value, fill=self._color,
                                     font=T.font(20))
        self._sid = self.create_text(T.px(12), T.px(61), anchor="nw", text=self._sub, fill=self._sub_color,
                                     font=T.font(9))
        self._segs = []
        if self.meter:
            sw, gap, sh = T.px(5), T.px(2), T.px(11)
            x = w - T.px(12) - 7 * (sw + gap) + gap
            for i in range(7):
                self._segs.append(self.create_rectangle(x + i * (sw + gap), T.px(62),
                                                        x + i * (sw + gap) + sw, T.px(62) + sh,
                                                        fill=T.GREY_DIM, outline=""))
        self._sx, self._sy = T.px(12), T.px(84)
        self._plot = PlotImage(w - T.px(24), h - T.px(84) - T.px(10), T.SURFACE)
        self.create_image(self._sx, self._sy, anchor="nw", image=self._plot.photo)
        self._paint_meter()
        self._paint_spark()

    def set(self, value: str, sub: str, color: str = T.TEXT, level: Optional[int] = None,
            sub_color: str = T.TEXT2) -> None:
        self._value, self._sub, self._color, self._level = value, sub, color, level
        self._sub_color = sub_color
        if self._plot is None:
            return
        self.itemconfigure(self._vid, text=value, fill=color)
        self.itemconfigure(self._sid, text=sub, fill=sub_color)
        self._paint_meter()

    def set_series(self, samples: list[Sample], get, t1: float, ymin: float, ymax: float) -> None:
        self._series = (samples, get, t1, ymin, ymax)
        self._paint_spark()

    def _paint_meter(self) -> None:
        if not self.meter or self._plot is None:
            return
        lvl = self._level if self._level is not None and 0 <= self._level <= 7 else 0
        for i, seg in enumerate(self._segs):
            on = i < lvl
            self.itemconfigure(seg, fill=(T.WARN if (on and i >= 6) else T.LINE) if on else T.GREY_DIM)

    def _paint_spark(self) -> None:
        if self._plot is None or self._series is None:
            return
        samples, get, t1, ymin, ymax = self._series
        w, h = self._plot.w, self._plot.h
        runs = to_runs(samples, get, t1 - SPARK_WINDOW_S, t1, 20.0, w, h - 2, ymin, ymax)

        def paint(d):
            fill_runs(d, runs, T.FILL, h)
            line_runs(d, runs, T.LINE, 1.5)
        self._plot.render(paint)


# --------------------------------------------------------------- the charts
@dataclass
class SeriesSpec:
    key: str
    label: str
    color: str
    get: Callable[[Sample], Optional[float]]
    width: float = 1.8
    fill: Optional[str] = None


class TimeChart(tk.Canvas):
    def __init__(self, master, title: str, unit: str, series: list[SeriesSpec], history: History,
                 ymin: float, ymax: float) -> None:
        super().__init__(master, bg=T.BG, highlightthickness=0, height=T.px(80))
        self.title, self.unit, self.series, self.history = title, unit, series, history
        self.ymin, self.ymax = ymin, ymax
        self.window_s = 1800.0
        self.delay = 3.75               # render this far behind real time (see module docstring)
        self.gap_s = 20.0               # a longer gap in the data breaks the line
        self.last_frame_ms = 0.0
        self._marker: Optional[tuple[float, str, str]] = None
        self._plot: Optional[PlotImage] = None
        self._live: dict[str, str] = {}
        self._running = False
        self.bind("<Configure>", self._layout)
        self.bind("<Map>", lambda _e: self.start())

    # ------------------------------------------------------------- layout
    def _layout(self, _e=None) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 120 or h < 80:
            return
        self._x0, self._y0 = T.px(14), T.px(36)
        self._x1, self._y1 = w - T.px(14), h - T.px(24)
        round_rect(self, 1, 1, w - 1, h - 1, T.px(8), fill=T.SURFACE, outline=T.BORDER)
        self.create_text(T.px(14), T.px(10), anchor="nw", text=self.title, fill=T.TEXT, font=T.font(10, True))
        slot = T.px(150)
        self._legend = {}
        x = w - T.px(14)
        for spec in reversed(self.series):
            x -= slot
            dot = self.create_oval(x, T.px(14), x + T.px(8), T.px(22), fill=spec.color, outline="")
            txt = self.create_text(x + T.px(14), T.px(18), anchor="w", text=self._live.get(spec.key, spec.label),
                                   fill=T.TEXT, font=T.font(9))
            self._legend[spec.key] = txt
        # scale shown beside the title, not over the plot where it would sit on the data
        self.create_text(T.px(14) + T.px(8 * len(self.title) + 14), T.px(11), anchor="nw", fill=T.TEXT3,
                         font=T.font(8), text=f"scale {self.ymin:g} to {self.ymax:g}{self.unit}")
        self._tl_left = self.create_text(self._x0, h - T.px(8), anchor="w", fill=T.TEXT3, font=T.font(8), text="")
        self.create_text(self._x1, h - T.px(8), anchor="e", fill=T.TEXT3, font=T.font(8), text="now")
        self._mark_txt = self.create_text(self._x1 - T.px(4), self._y0, anchor="se", fill=T.WARN,
                                          font=T.font(8), text="", state="hidden")
        self._plot = PlotImage(self._x1 - self._x0, self._y1 - self._y0, T.SURFACE)
        self.create_image(self._x0, self._y0, anchor="nw", image=self._plot.photo)
        self.tag_raise(self._mark_txt)
        self._update_window_label()
        self._update_marker_text()
        self._draw(time.time())

    # ------------------------------------------------------------- control
    def set_window(self, seconds: float) -> None:
        self.window_s = float(seconds)
        self._update_window_label()
        self._draw(time.time())

    def _interval_ms(self) -> int:
        """Redraw only as often as the picture visibly moves: about a third of a pixel per frame.
        (5 min: ~9 fps, 30 min and 2 h: 2 fps. A faster redraw would just repeat the same picture.)"""
        if self._plot is None:
            return 500
        speed = self._plot.w / self.window_s                      # pixels per second
        return int(min(500, max(50, 350.0 / max(speed, 1e-3))))

    def _update_window_label(self) -> None:
        if self._plot is None:
            return
        s = self.window_s
        label = f"{int(s // 3600)} h ago" if s >= 3600 else f"{int(s // 60)} min ago"
        self.itemconfigure(self._tl_left, text=label)

    def set_live(self, texts: dict[str, str]) -> None:
        self._live.update(texts)
        if self._plot is None:
            return
        for key, text in texts.items():
            if key in self._legend:
                self.itemconfigure(self._legend[key], text=text)

    def set_marker(self, value: Optional[float], text: str = "", color: str = T.WARN) -> None:
        self._marker = None if value is None else (float(value), text, color)
        self._update_marker_text()

    def _update_marker_text(self) -> None:
        if self._plot is None:
            return
        if self._marker and self.ymin < self._marker[0] < self.ymax:
            v, text, color = self._marker
            y = self._y0 + (self._y1 - self._y0) * (1 - (v - self.ymin) / (self.ymax - self.ymin))
            self.coords(self._mark_txt, self._x1 - T.px(4), y - T.px(2))
            self.itemconfigure(self._mark_txt, text=text, fill=color, state="normal")
        else:
            self.itemconfigure(self._mark_txt, state="hidden")

    # ----------------------------------------------------------- animation
    def start(self) -> None:
        if not self._running:
            self._running = True
            self._tick()

    def _tick(self) -> None:
        try:
            visible = bool(self.winfo_exists() and self.winfo_viewable())
        except tk.TclError:
            visible = False
        if not visible:
            self._running = False               # redraw resumes from the <Map> event
            return
        t = time.perf_counter()
        try:
            self._draw(time.time())
        except Exception:
            log.exception("chart redraw failed; stopping this chart")
            self._running = False
            return
        self.last_frame_ms = (time.perf_counter() - t) * 1000
        self.after(max(5, int(self._interval_ms() - self.last_frame_ms)), self._tick)

    def _draw(self, now: float) -> None:
        if self._plot is None:
            return
        rt = now - self.delay
        t0 = rt - self.window_s
        raw = self.history.samples_between(t0 - 90, rt + 90)
        w, h = self._plot.w, self._plot.h
        layers = [(spec, to_runs(raw, spec.get, t0, rt, self.gap_s, w, h, self.ymin, self.ymax))
                  for spec in self.series]
        marker = self._marker

        def paint(d):
            for spec, runs in layers:
                if spec.fill:
                    fill_runs(d, runs, spec.fill, h)
            if marker and self.ymin < marker[0] < self.ymax:
                y = h * (1 - (marker[0] - self.ymin) / (self.ymax - self.ymin))
                dash, rgbc = T.px(6), T.rgb(marker[2])
                x = 0
                while x < w:
                    d.line([(x * SS, y * SS), (min(x + dash * 0.55, w) * SS, y * SS)], fill=rgbc, width=SS)
                    x += dash
            for spec, runs in reversed(layers):          # first series on top
                line_runs(d, runs, spec.color, spec.width)
        self._plot.render(paint)


# -------------------------------------------------------- time at each level
class LevelBars(tk.Canvas):
    def __init__(self, master) -> None:
        super().__init__(master, bg=T.BG, highlightthickness=0, height=T.px(164))
        self._fractions: dict[int, float] = {}
        self._title = "Time at each fan level"
        self.bind("<Configure>", lambda _e: self.redraw())

    def set(self, fractions: dict[int, float], title: str) -> None:
        self._fractions, self._title = fractions, title
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 120 or h < 60:
            return
        round_rect(self, 1, 1, w - 1, h - 1, T.px(8), fill=T.SURFACE, outline=T.BORDER)
        self.create_text(T.px(14), T.px(10), anchor="nw", text=self._title, fill=T.TEXT, font=T.font(10, True))
        levels = list(range(8))
        if 0x80 in self._fractions:
            levels.append(0x80)
        top, row_h = T.px(34), (h - T.px(34) - T.px(8)) / len(levels)
        bx0, bx1 = T.px(70), w - T.px(58)
        for i, lvl in enumerate(levels):
            y = top + i * row_h
            frac = self._fractions.get(lvl, 0.0)
            label = "EC auto" if lvl == 0x80 else f"Level {lvl}"
            self.create_text(T.px(14), y + row_h / 2, anchor="w", text=label,
                             fill=T.TEXT if frac > 0 else T.TEXT3, font=T.font(9))
            bar_h = max(T.px(5), row_h * 0.46)
            cy = y + row_h / 2
            round_rect(self, bx0, cy - bar_h / 2, bx1, cy + bar_h / 2, T.px(3), fill=T.SURFACE_HI, outline="")
            if frac > 0:
                color = T.GREY if lvl in (0, 0x80) else (T.WARN if lvl >= 7 else T.LINE)
                x_end = bx0 + max(T.px(5), (bx1 - bx0) * frac)
                round_rect(self, bx0, cy - bar_h / 2, x_end, cy + bar_h / 2, T.px(3), fill=color, outline="")
            self.create_text(w - T.px(14), cy, anchor="e", text=f"{frac * 100:.0f}%" if frac >= 0.005 else "-",
                             fill=T.TEXT if frac > 0 else T.TEXT3, font=T.font(9))


# ---------------------------------------------------------------- the tab
class Dashboard(tk.Frame):
    WINDOWS = ((300, "5 min"), (1800, "30 min"), (7200, "2 h"))

    def __init__(self, master, history: History) -> None:
        super().__init__(master, bg=T.BG)
        self.history = history
        self.window_s = 1800
        self._last_stats = 0.0
        pad = T.px(6)

        cards = tk.Frame(self, bg=T.BG)
        cards.pack(fill="x", padx=pad, pady=(pad + 2, 0))
        self.cards = {
            "cpu": Card(cards, "CPU"), "gpu": Card(cards, "GPU"),
            "fan1": Card(cards, "Fan 1", meter=True), "fan2": Card(cards, "Fan 2", meter=True),
            "run": Card(cards, "Controller", meter=False),
        }
        for i, c in enumerate(self.cards.values()):
            c.grid(row=0, column=i, sticky="ew", padx=pad // 2)
            cards.columnconfigure(i, weight=1, uniform="cards")

        head = tk.Frame(self, bg=T.BG)
        head.pack(fill="x", padx=pad + 4, pady=(T.px(10), T.px(2)))
        tk.Label(head, text="History", bg=T.BG, fg=T.TEXT, font=T.font(11, True)).pack(side="left")
        self._toggles: dict[int, tk.Label] = {}
        for secs, text in reversed(self.WINDOWS):
            lbl = tk.Label(head, text=text, bg=T.BG, fg=T.TEXT2, font=T.font(9), cursor="hand2", padx=T.px(8))
            lbl.pack(side="right")
            lbl.bind("<Button-1>", lambda _e, s=secs: self.set_window(s))
            self._toggles[secs] = lbl

        # The bottom row is packed before the charts so a short window shrinks the
        # charts (which stretch) rather than cutting this row off.
        bottom = tk.Frame(self, bg=T.BG)
        bottom.pack(side="bottom", fill="x", padx=pad, pady=(0, pad))
        charts = tk.Frame(self, bg=T.BG)
        charts.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.temp_chart = TimeChart(charts, "Temperature", "°C", [
            SeriesSpec("cpu", "CPU", T.LINE, lambda s: s.cpu, 2.0, T.FILL),
            SeriesSpec("gpu", "GPU", T.GREY, lambda s: s.gpu, 1.5),
        ], history, 30, 110)
        self.fan_chart = TimeChart(charts, "Fan speed", " RPM", [
            SeriesSpec("fan1", "Fan 1", T.LINE, lambda s: s.rpm1, 2.0, T.FILL),
            SeriesSpec("fan2", "Fan 2", T.GREY, lambda s: s.rpm2, 1.5),
        ], history, 0, 7000)
        for r, ch in enumerate((self.temp_chart, self.fan_chart)):
            ch.grid(row=r, column=0, sticky="nsew", pady=(0, pad // 2))
            charts.rowconfigure(r, weight=1)
        charts.columnconfigure(0, weight=1)

        self.bars = LevelBars(bottom)
        self.bars.grid(row=0, column=0, sticky="nsew", padx=pad // 2)
        self.stats_box = tk.Canvas(bottom, bg=T.BG, highlightthickness=0, height=T.px(164))
        self.stats_box.grid(row=0, column=1, sticky="nsew", padx=pad // 2)
        bottom.columnconfigure(0, weight=1, uniform="bottom")
        bottom.columnconfigure(1, weight=1, uniform="bottom")
        self._stats: list[tuple[str, str, str]] = []
        self._stats_title = "Statistics"
        self._spark_version = -1
        self.stats_box.bind("<Configure>", lambda _e: self._draw_stats())

        self.set_window(self.window_s)

    # --------------------------------------------------------------- window
    def set_window(self, seconds: int) -> None:
        self.window_s = seconds
        for secs, lbl in self._toggles.items():
            lbl.configure(fg=T.LINE if secs == seconds else T.TEXT2,
                          font=T.font(9, True) if secs == seconds else T.font(9))
        self.temp_chart.set_window(seconds)
        self.fan_chart.set_window(seconds)
        self._last_stats = 0.0
        latest = self.history.latest()
        if latest is not None:
            self._refresh_stats(time.time())

    def _window_name(self) -> str:
        return dict(self.WINDOWS)[self.window_s]

    # --------------------------------------------------------------- update
    def update_view(self, s, cfg: dict) -> None:
        """Called every GUI poll with the newest snapshot and the live settings."""
        cycle = float(cfg.get("cycle_s", 3))
        for ch in (self.temp_chart, self.fan_chart):
            ch.delay = cycle + 0.75
            ch.gap_s = max(20.0, cycle * 4 + 5)
        handover = int(cfg.get("handover_c", 92))
        warn = handover - 10

        def temp_color(v):
            if v is None:
                return T.TEXT2
            return T.CRIT if v >= handover else (T.WARN if v >= warn else T.TEXT)

        t = s.temps or {}
        cpu, gpu = t.get("CPU"), t.get("GPU")
        self.cards["cpu"].set("-" if cpu is None else f"{cpu}°C", f"curve asks for level {s.cpu_level}",
                              temp_color(cpu))
        self.cards["gpu"].set("-" if gpu is None else f"{gpu}°C", f"curve asks for level {s.gpu_level}",
                              temp_color(gpu))
        fans = {f.fan: f for f in s.fans}
        for n in (1, 2):
            f = fans.get(n)
            key = f"fan{n}"
            if f is None:
                self.cards[key].set("-", "", T.TEXT2)
                continue
            stalled = n in s.stalled
            self.cards[key].set(f"{f.rpm:,} RPM", "not spinning!" if stalled else describe(f.level),
                                T.CRIT if stalled else T.TEXT, f.level,
                                sub_color=T.CRIT if stalled else T.TEXT2)
        errs = s.ec_errors
        word, color = STATUS_WORDS.get(s.status, (s.status, T.TEXT))
        err_txt = "no EC errors" if not errs else f"{errs} EC error{'s' if errs != 1 else ''}"
        self.cards["run"].set(word, f"up {fmt_duration(time.time() - self.history.started)}  ·  {err_txt}", color,
                              sub_color=T.CRIT if errs >= 5 else (T.WARN if errs >= 3 else T.TEXT2))

        self.temp_chart.set_live({"cpu": f"CPU {cpu}°C" if cpu is not None else "CPU -",
                                  "gpu": f"GPU {gpu}°C" if gpu is not None else "GPU -"})
        self.fan_chart.set_live({"fan1": f"Fan 1  {fans[1].rpm:,} RPM" if 1 in fans else "Fan 1  -",
                                 "fan2": f"Fan 2  {fans[2].rpm:,} RPM" if 2 in fans else "Fan 2  -"})
        self.temp_chart.set_marker(handover, f"hand-over {handover}°C", T.WARN)

        latest = self.history.latest()
        if latest is not None and self._spark_version != self.history.version:
            self._spark_version = self.history.version
            recent = self.history.samples_between(latest.t - SPARK_WINDOW_S - 60, latest.t)
            self.cards["cpu"].set_series(recent, lambda x: x.cpu, latest.t, 30, 100)
            self.cards["gpu"].set_series(recent, lambda x: x.gpu, latest.t, 30, 100)
            self.cards["fan1"].set_series(recent, lambda x: x.rpm1, latest.t, 0, 7000)
            self.cards["fan2"].set_series(recent, lambda x: x.rpm2, latest.t, 0, 7000)
            self.cards["run"].set_series(recent, lambda x: x.lvl1 if x.lvl1 is not None and x.lvl1 <= 7 else None,
                                         latest.t, 0, 7)
        now = time.time()
        if now - self._last_stats >= 1.0:
            self._refresh_stats(now, s)

    # ---------------------------------------------------------------- stats
    def _refresh_stats(self, now: float, s=None) -> None:
        self._last_stats = now
        w, name = self.window_s, self._window_name()
        h = self.history
        total = sum(h.time_at_level(w, now).values())
        fractions = {k: v / total for k, v in h.time_at_level(w, now).items()} if total > 0 else {}
        self.bars.set(fractions, f"Time at each fan level (last {name})")

        peak = h.peak(w, now)
        cph = h.changes_per_hour(w, now)
        hand = h.status_entries(["handover"], w, now)
        pauses = h.status_entries(["paused"], w, now)
        ev = h.last_event()
        hot = getattr(s, "hottest", None) if s is not None else None
        rows = [
            ("Peak temperature", f"{peak[0]}°C at {time.strftime('%H:%M', time.localtime(peak[1]))}" if peak else "-",
             T.TEXT),
            ("Fan level changes", f"{cph:.0f} per hour" if cph is not None else "-", T.TEXT),
            ("Hand-overs to the EC", str(hand), T.WARN if hand else T.TEXT),
            ("Pauses (other EC tools)", str(pauses), T.WARN if pauses else T.TEXT),
            ("Hottest sensor now", f"{hot[0]} {hot[1]}°C" if hot else "-", T.TEXT),
            ("Last event", f"{clock(ev[0])}  {ev[1]}" if ev else "none yet", T.TEXT),
        ]
        self._stats = [(f"{r[0]}", r[1], r[2]) for r in rows]
        self._stats_title = f"Statistics (last {name})"
        self._draw_stats()

    def _draw_stats(self) -> None:
        c = self.stats_box
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 120 or h < 60:
            return
        round_rect(c, 1, 1, w - 1, h - 1, T.px(8), fill=T.SURFACE, outline=T.BORDER)
        c.create_text(T.px(14), T.px(10), anchor="nw", text=self._stats_title,
                      fill=T.TEXT, font=T.font(10, True))
        if not self._stats:
            return
        top = T.px(36)
        row_h = (h - top - T.px(8)) / len(self._stats)
        for i, (k, v, color) in enumerate(self._stats):
            y = top + i * row_h + row_h / 2
            c.create_text(T.px(14), y, anchor="w", text=k, fill=T.TEXT2, font=T.font(9))
            c.create_text(w - T.px(14), y, anchor="e", text=v, fill=color, font=T.font(9))
