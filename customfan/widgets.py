"""Small custom widgets for the dark theme.

tk.Scale cannot colour its handle separately from its background, so on a dark
window the handle vanishes. FlatScale draws its own: a thin track, a light-blue
filled part, a round white handle and the value written at the right end.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from . import theme as T


class FlatScale(tk.Canvas):
    def __init__(self, parent, from_: int, to: int, variable: tk.IntVar, length: int = 300,
                 command: Optional[Callable[[str], None]] = None, unit: str = "",
                 bg: str = T.BG) -> None:
        self._h = T.px(30)
        super().__init__(parent, width=T.px(length), height=self._h, bg=bg, highlightthickness=0,
                         takefocus=1, cursor="hand2")
        self._bg = bg
        self.lo, self.hi = int(from_), int(to)
        self.var = variable
        self.command = command
        self.unit = unit
        self._label_w = T.px(46)
        self._pad = T.px(10)
        self._hover = False
        self._focus = False
        self.var.trace_add("write", lambda *_: self._draw())
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<B1-Motion>", self._click)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Left>", lambda _e: self._nudge(-1))
        self.bind("<Right>", lambda _e: self._nudge(+1))
        self.bind("<Down>", lambda _e: self._nudge(-1))
        self.bind("<Up>", lambda _e: self._nudge(+1))
        self.bind("<Prior>", lambda _e: self._nudge(+5))
        self.bind("<Next>", lambda _e: self._nudge(-5))
        self.bind("<Home>", lambda _e: self._set(self.lo))
        self.bind("<End>", lambda _e: self._set(self.hi))
        self._draw()

    # ----------------------------------------------------------- value
    def get(self) -> int:
        try:
            return int(self.var.get())
        except (tk.TclError, ValueError):
            return self.lo

    def _set(self, value: int) -> None:
        value = max(self.lo, min(self.hi, int(value)))
        if value == self.get():
            return
        self.var.set(value)
        if self.command:
            self.command(str(value))

    def _nudge(self, delta: int) -> str:
        self._set(self.get() + delta)
        return "break"

    # ----------------------------------------------------------- mouse
    def _track(self) -> tuple[int, int]:
        width = max(self.winfo_width(), T.px(120))
        return self._pad, width - self._label_w - self._pad

    def _click(self, event) -> None:
        self.focus_set()
        x0, x1 = self._track()
        frac = (event.x - x0) / max(1, x1 - x0)
        self._set(round(self.lo + max(0.0, min(1.0, frac)) * (self.hi - self.lo)))

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _set_focus(self, on: bool) -> None:
        self._focus = on
        self._draw()

    # ----------------------------------------------------------- draw
    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), T.px(120))
        x0, x1 = self._track()
        mid = self._h // 2
        value = self.get()
        frac = (value - self.lo) / max(1, self.hi - self.lo)
        hx = x0 + frac * (x1 - x0)
        th = T.px(2)
        self.create_rectangle(x0, mid - th, x1, mid + th, fill=T.BORDER if self._bg == T.SURFACE_HI else T.SURFACE_HI, outline="")
        self.create_rectangle(x0, mid - th, hx, mid + th, fill=T.LINE, outline="")
        r = T.px(7 if (self._hover or self._focus) else 6)
        if self._focus:
            self.create_oval(hx - r - T.px(3), mid - r - T.px(3), hx + r + T.px(3), mid + r + T.px(3),
                             outline=T.LINE, width=1)
        self.create_oval(hx - r, mid - r, hx + r, mid + r, fill=T.TEXT, outline=self._bg, width=T.px(2))
        self.create_text(width - self._pad // 2, mid, text=f"{value}{self.unit}", fill=T.TEXT,
                         anchor="e", font=T.font(10))


class Stat(tk.Frame):
    """A Task Manager style statistic: small grey caption over a large value."""

    def __init__(self, parent, caption: str, bg: str = T.BG, size: int = 15) -> None:
        super().__init__(parent, bg=bg)
        tk.Label(self, text=caption, bg=bg, fg=T.TEXT2, font=T.font(9), anchor="w").pack(anchor="w")
        self._value = tk.Label(self, text="-", bg=bg, fg=T.TEXT, font=T.font(size), anchor="w")
        self._value.pack(anchor="w")

    def set(self, text: str, color: str = T.TEXT) -> None:
        self._value.configure(text=text, fg=color)


class NavBar(tk.Canvas):
    """Task Manager's left rail: an icon and a name per page, a collapse button on top
    and Settings at the bottom. Click an item to select it."""

    ROW = 40
    WIDE, NARROW = 196, 54

    def __init__(self, parent, items, bottom_items, on_select: Callable[[str], None]) -> None:
        super().__init__(parent, bg=T.BG, highlightthickness=0, width=T.px(self.WIDE))
        self.items, self.bottom_items, self.on_select = list(items), list(bottom_items), on_select
        self.expanded = True
        self.selected = self.items[0][0]
        self._hover: Optional[str] = None
        self._hits: list[tuple[int, int, str]] = []
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self._set_hover(None))
        self.bind("<Button-1>", self._click)

    # ------------------------------------------------------------ state
    def select(self, key: str) -> None:
        self.selected = key
        self.redraw()

    def _set_hover(self, key: Optional[str]) -> None:
        if key != self._hover:
            self._hover = key
            self.redraw()

    def _at(self, y: int) -> Optional[str]:
        for y0, y1, key in self._hits:
            if y0 <= y < y1:
                return key
        return None

    def _motion(self, e) -> None:
        self._set_hover(self._at(e.y))

    def _click(self, e) -> None:
        key = self._at(e.y)
        if key == "menu":
            self.expanded = not self.expanded
            self.configure(width=T.px(self.WIDE if self.expanded else self.NARROW))
            self.redraw()
        elif key and key != self.selected:
            self.selected = key
            self.redraw()
            self.on_select(key)

    # ------------------------------------------------------------ drawing
    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 100:
            return
        row = T.px(self.ROW)
        self._hits = []
        y = T.px(6)
        self._row("menu", "menu", "", y, w, row)
        y += row + T.px(10)
        for key, label, icon in self.items:
            self._row(key, icon, label, y, w, row)
            y += row + T.px(2)
        y = h - T.px(8) - len(self.bottom_items) * (row + T.px(2))
        for key, label, icon in self.bottom_items:
            self._row(key, icon, label, y, w, row)
            y += row + T.px(2)

    def _row(self, key: str, icon: str, label: str, y: int, w: int, row: int) -> None:
        self._hits.append((y, y + row, key))
        sel = key == self.selected
        if sel or key == self._hover:
            round_rect_(self, T.px(6), y, w - T.px(6), y + row, T.px(5), fill=T.SURFACE_HI if sel else T.SURFACE)
        if sel:
            self.create_rectangle(T.px(6), y + row / 2 - T.px(8), T.px(9), y + row / 2 + T.px(8),
                                  fill=T.LINE, outline="")
        colour = T.TEXT if sel or key == self._hover else T.TEXT2
        ix = T.px(18) if self.expanded else (w - T.px(20)) // 2
        self._icon(icon, ix, y + (row - T.px(20)) // 2, colour)
        if self.expanded and label:
            self.create_text(T.px(50), y + row / 2, anchor="w", text=label, fill=colour, font=T.font(10))

    def _icon(self, kind: str, x: int, y: int, colour: str) -> None:
        def pts(*coords):
            return [(x if i % 2 == 0 else y) + T.px(c) for i, c in enumerate(coords)]
        lw = T.px(1)
        line = lambda *c: self.create_line(*pts(*c), fill=colour, width=lw, joinstyle="round")   # noqa: E731
        if kind == "menu":
            for yy in (5, 10, 15):
                line(3, yy, 17, yy)
        elif kind == "overview":
            self.create_rectangle(*pts(2, 3, 18, 17), outline=colour, width=lw)
            line(4, 11, 7, 11, 9, 6, 12, 14, 14, 11, 16, 11)
        elif kind == "curves":
            line(2, 17, 2, 13, 7, 13, 7, 9, 12, 9, 12, 4, 18, 4)
        elif kind == "sensors":
            line(8, 12, 8, 3, 12, 3, 12, 12)
            self.create_oval(*pts(5, 10, 15, 19), outline=colour, width=lw)
            self.create_oval(*pts(8, 13, 12, 17), fill=colour, outline="")
        elif kind == "events":
            for yy in (5, 10, 15):
                line(7, yy, 17, yy)
                self.create_oval(*pts(2.5, yy - 1.2, 4.7, yy + 1.2), fill=colour, outline="")
        elif kind == "settings":
            line(3, 6, 17, 6)
            line(3, 14, 17, 14)
            self.create_oval(*pts(10, 3.5, 15, 8.5), fill=T.BG, outline=colour, width=lw)
            self.create_oval(*pts(5, 11.5, 10, 16.5), fill=T.BG, outline=colour, width=lw)


def round_rect_(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, outline="", **kw)
