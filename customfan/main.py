"""Entry point: checks, logging, then window + control loop."""
from __future__ import annotations

import argparse
import atexit
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import APP_ID, APP_NAME, __version__
from . import config as cfgmod
from . import winutil
from .service import ControlLoop, CycleLog

if getattr(sys, "frozen", False):                    # built CustomFanController.exe
    APP_DIR = Path(sys.executable).resolve().parent
else:                                                # CustomFanController.pyw
    APP_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger("customfan")


def setup_logging(debug: bool) -> Path:
    logdir = APP_DIR / "logs"
    logdir.mkdir(exist_ok=True)
    path = logdir / f"{APP_ID}.log"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:                      # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    return path


def fatal(title: str, text: str) -> None:
    log.error("%s: %s", title, text)
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, text)
        r.destroy()
    except Exception:
        print(f"{title}: {text}", file=sys.stderr if sys.stderr else sys.stdout)
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} - ThinkPad P1 Gen 6 dual-fan control")
    ap.add_argument("--simulate", action="store_true", help="no hardware: use a simulated EC")
    ap.add_argument("--readonly", action="store_true", help="read sensors and fans, never write")
    ap.add_argument("--show", action="store_true", help="open the window at start (default: tray only)")
    ap.add_argument("--debug", action="store_true", help="verbose log")
    args = ap.parse_args(argv)

    logpath = setup_logging(args.debug)
    log.info("=== %s %s starting (exe=%s simulate=%s readonly=%s) ===", APP_NAME, __version__,
             Path(sys.executable).name, args.simulate, args.readonly)

    if winutil.IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp UI on scaled displays
        except Exception:
            pass

    if not args.simulate:
        if not winutil.is_admin():
            fatal("Administrator required",
                  "PawnIO needs administrator rights.\nRun from an admin terminal or via the scheduled task.")
        others = winutil.running_controllers()
        if others:
            fatal("Another fan controller is running",
                  "Close these first (two programs driving the EC at once causes errors):\n\n  "
                  + "\n  ".join(others))
    if not winutil.claim_single_instance():
        fatal("Already running", f"{APP_NAME} is already running (check the tray).")

    # Simulate mode keeps its own settings so experiments never touch the real curve.
    cfg_path = APP_DIR / ("config.simulate.json" if args.simulate else "config.json")
    cfg = cfgmod.load(cfg_path)

    if args.simulate:
        from .simulator import SimulatedEC
        open_io = SimulatedEC
    else:
        module = APP_DIR / "LpcACPIEC.bin"
        if not module.is_file():
            fatal("Missing file", f"{module} not found. It must sit in {APP_DIR}.")

        def open_io():
            from .pawnio import PawnIOPortIO
            return PawnIOPortIO(module)

    cycle_csv = APP_DIR / "logs" / ("cycles.simulate.csv" if args.simulate else "cycles.csv")
    loop = ControlLoop(open_io, cfg, readonly=args.readonly,
                       conflict_check=None if args.simulate else winutil.running_ec_monitors,
                       cycle_log=CycleLog(cycle_csv))
    atexit.register(loop.shutdown_hardware)      # last-resort hand-back to the EC
    loop.start()

    import tkinter as tk
    from .gui import App
    root = tk.Tk()
    suffix = " [SIMULATED]" if args.simulate else " [READ-ONLY]" if args.readonly else ""
    icon_file = APP_DIR / "assets" / "app.ico"
    if winutil.IS_WINDOWS and icon_file.is_file():
        try:
            root.iconbitmap(default=str(icon_file))
        except tk.TclError:
            pass
    App(root, loop, cfg, cfg_path, use_tray=True, start_hidden=not args.show, title_suffix=suffix,
        history_csv=cycle_csv, log_path=logpath, module_path=None if args.simulate else APP_DIR / "LpcACPIEC.bin")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()                         # fans back to EC (no-op if already done)
        log.info("=== stopped (log: %s) ===", logpath)
        logging.shutdown()
        # A tray library helper thread can linger and keep the process alive;
        # the fans are already handed back, so end the process explicitly.
        os._exit(0)


if __name__ == "__main__":
    main()
