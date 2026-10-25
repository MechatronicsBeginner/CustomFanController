"""Custom Fan Controller - start here.

    pythonw CustomFanController.pyw                      normal use: tray icon, fan curve active
    python  CustomFanController.pyw --readonly --show    first test: reads everything, writes nothing
    python  CustomFanController.pyw --simulate --show    try the window with no hardware at all

build.ps1 turns this into CustomFanController.exe (recommended for daily use:
the tray icon then has its own name and can stay pinned on the taskbar).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from customfan.main import main  # noqa: E402

main()
