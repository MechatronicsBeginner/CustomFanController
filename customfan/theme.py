"""Colours and ttk styling: a dark look modelled on Windows 11's Task Manager.

Rules the whole window follows:
  * text is white (secondary text grey);
  * information is drawn in greys and light blue;
  * orange and red are reserved for "pay attention" and "act now".
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from tkinter import ttk

BG = "#1c1c1c"          # window
SURFACE = "#272727"     # cards, charts, input fields
SURFACE_HI = "#323232"  # raised: buttons, selected tab
BORDER = "#3a3a3a"
TEXT = "#ffffff"
TEXT2 = "#a8a8a8"       # secondary text
TEXT3 = "#7c7c7c"       # tertiary text (axis hints)
LINE = "#5eb8f4"        # light blue: the main information
FILL = "#24394a"        # LINE blended into SURFACE, for area fills
GREY = "#9a9a9a"        # secondary series
GREY_DIM = "#5b5b5b"
WARN = "#ff9f43"        # orange: attention
CRIT = "#ff5c5c"        # red: act now
FONT = "Segoe UI"

_K = 1.0


def init_scaling(root: tk.Misc) -> None:
    """Pixel sizes drawn on canvases don't follow Windows display scaling by
    themselves; remember the factor so px() can apply it."""
    global _K
    try:
        _K = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        _K = 1.0


def px(v: float) -> int:
    return max(1, int(round(v * _K)))


def font(size: int, bold: bool = False):
    return (FONT, size, "bold") if bold else (FONT, size)


def rgb(hexcolor: str) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def apply_dark_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, troughcolor=SURFACE, focuscolor=BG, insertcolor=TEXT,
                    selectbackground=LINE, selectforeground=BG, font=font(9))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=TEXT2)
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=TEXT2, padding=(14, 7), borderwidth=0,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, focuscolor=BG)
    style.configure("TNotebook", lightcolor=BG, darkcolor=BG, bordercolor=BG)
    style.map("TNotebook.Tab", background=[("selected", SURFACE_HI), ("active", SURFACE)],
              foreground=[("selected", TEXT), ("active", TEXT)])
    style.configure("TButton", background=SURFACE_HI, foreground=TEXT, bordercolor=BORDER,
                    padding=(14, 6), relief="flat")
    style.map("TButton", background=[("pressed", SURFACE), ("active", "#3d3d3d")],
              foreground=[("disabled", TEXT3)])
    for name in ("TRadiobutton", "TCheckbutton"):
        style.configure(name, background=BG, foreground=TEXT, indicatorcolor=SURFACE,
                        indicatorbackground=SURFACE, indicatorforeground=LINE)
        style.map(name, background=[("active", BG)], indicatorcolor=[("selected", LINE), ("active", SURFACE_HI)],
                  foreground=[("disabled", TEXT3)])
    style.configure("TSpinbox", fieldbackground=SURFACE, foreground=TEXT, arrowcolor=TEXT2,
                    bordercolor=BORDER, background=SURFACE_HI)
    style.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT, bordercolor=BORDER)
    for orient in ("Vertical", "Horizontal"):
        name = f"{orient}.TScrollbar"
        style.configure(name, background=SURFACE_HI, troughcolor=SURFACE, bordercolor=SURFACE, lightcolor=SURFACE_HI,
                        darkcolor=SURFACE_HI, arrowcolor=TEXT2, gripcount=0, relief="flat", arrowsize=px(14))
        style.map(name, background=[("pressed", "#5a5a5a"), ("active", "#4a4a4a"), ("!active", SURFACE_HI)],
                  lightcolor=[("pressed", "#5a5a5a"), ("active", "#4a4a4a"), ("!active", SURFACE_HI)],
                  darkcolor=[("pressed", "#5a5a5a"), ("active", "#4a4a4a"), ("!active", SURFACE_HI)])
    style.configure("Card.TCheckbutton", background=SURFACE, foreground=TEXT, indicatorcolor=SURFACE_HI,
                    indicatorbackground=SURFACE_HI, indicatorforeground=LINE)
    style.map("Card.TCheckbutton", background=[("active", SURFACE)],
              indicatorcolor=[("selected", LINE), ("active", "#3d3d3d")], foreground=[("disabled", TEXT3)])
    style.configure("Accent.TButton", background=LINE, foreground="#0b1a26", bordercolor=LINE, padding=(14, 6),
                    relief="flat")
    style.map("Accent.TButton", background=[("pressed", "#4a9fd8"), ("active", "#7cc7f7")])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, rowheight=px(24), borderwidth=0, font=font(9))
    style.map("Treeview", background=[("selected", SURFACE_HI)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=SURFACE_HI, foreground=TEXT2, bordercolor=SURFACE_HI,
                    lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, relief="flat", padding=(8, 5), font=font(9))
    style.map("Treeview.Heading", background=[("active", SURFACE_HI)])
    style.configure("Hint.TLabel", background=BG, foreground=TEXT2)
    style.configure("Warn.TLabel", background=BG, foreground=WARN)

    # classic tk widgets take their colours from the option database
    for key, value in {
        "*Scale.background": BG, "*Scale.foreground": TEXT, "*Scale.troughColor": SURFACE_HI,
        "*Scale.activeBackground": LINE, "*Scale.highlightThickness": 0, "*Scale.borderWidth": 0,
        "*Scale.sliderRelief": "flat", "*Scale.relief": "flat",
        "*Canvas.background": BG, "*Canvas.highlightThickness": 0,
        "*Text.background": SURFACE, "*Text.foreground": TEXT,
        "*TCombobox*Listbox.background": SURFACE, "*TCombobox*Listbox.foreground": TEXT,
    }.items():
        root.option_add(key, value)


def enable_dark_titlebar(root: tk.Tk) -> None:
    """Dark title bar on Windows 10 (2004+) and 11. Silently does nothing elsewhere."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):          # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 on early builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass
