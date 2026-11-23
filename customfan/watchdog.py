"""Out-of-process safety net for the fans.  (The idea is explained in lease.py.)

The controller is the only program that can put the fans in manual mode, and
the EC keeps the last manual level forever. If the controller is killed or
crashes, the fans stay wherever it left them - at level 0 that means OFF while
the CPU heats up. This small, separate program watches the controller's lease
and, if the controller is gone or stuck while the fans are in manual mode,
hands the fans back to the EC (the same verified hand-back the controller does
on a normal exit).

It is deliberately dumb: no curves, no sensors, no GUI. It never touches the EC
while the controller is healthy, and never polls the EC at all - it only opens
the driver at the moment it has to act, so it cannot add EC traffic or collide
with other tools in normal use.

It runs as CustomFanWatchdog.exe (a copy of the controller exe) so that
"taskkill /IM CustomFanController.exe" does not take it down as well. The
controller starts it if it is not running and warns if it cannot.

What it cannot cover: Modern Standby. Windows freezes user programs there -
including this one - so a hand-back before sleep has to come from the
controller itself, while it is still awake.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .lease import CORRUPT, MISSING, LeaseWriter, pid_alive, read_lease

log = logging.getLogger("customfan.watchdog")

CONTROLLER_LEASE = "controller.lease"
WATCHDOG_LEASE = "watchdog.lease"
WATCHDOG_EXE = "CustomFanWatchdog.exe"
MUTEX_NAME = "Local\\CustomFanController.Watchdog"

POLL_S = 1.0
STALE_S = 12.0              # about 4 control cycles with no sign of life
STARTUP_WAIT_S = 30.0       # how long to wait for a first lease before giving up
MISSING_POLLS_TO_EXIT = 2   # lease gone this many polls in a row = clean exit

# What step() reports (the tests assert on these)
IDLE, HANDED_BACK, RETRY, EXIT = "idle", "handed-back", "retry", "exit"


class Watchdog:
    def __init__(self, lease_path: Path, hand_back: Callable[[], bool], *,
                 pid_alive: Callable[[int], bool] = pid_alive,
                 clock: Callable[[], float] = time.monotonic,
                 poll_s: float = POLL_S, stale_s: float = STALE_S,
                 startup_wait_s: float = STARTUP_WAIT_S) -> None:
        self.lease_path = Path(lease_path)
        self.hand_back = hand_back
        self.pid_alive = pid_alive
        self.clock = clock
        self.poll_s = poll_s
        self.stale_s = stale_s
        self.startup_wait_s = startup_wait_s
        self._started = clock()
        self._last_step: float | None = None
        self._key: tuple | None = None      # (pid, seq) of the newest lease seen
        self._changed_at = self._started    # when that key last changed (our clock)
        self._had_lease = False
        self._missing = 0
        self._handled: tuple | None = None  # key we already handed back for
        self._fails = 0
        self._next_try = 0.0

    def step(self) -> str:
        now = self.clock()
        if self._last_step is not None and now - self._last_step > max(5 * self.poll_s, 5.0):
            # We were not running (sleep, or the machine was starved). The
            # controller was frozen too, so don't blame it for the silence.
            log.info("watchdog was not scheduled for %.0f s - giving the controller a fresh grace period",
                     now - self._last_step)
            self._changed_at = now
        self._last_step = now

        lease = read_lease(self.lease_path)

        if lease == MISSING:
            if self._had_lease:
                self._missing += 1
                if self._missing >= MISSING_POLLS_TO_EXIT:
                    log.info("controller lease removed (clean exit) - watchdog done")
                    return EXIT
            elif now - self._started > self.startup_wait_s:
                log.info("no controller lease appeared within %.0f s - watchdog done", self.startup_wait_s)
                return EXIT
            return IDLE
        self._missing = 0
        self._had_lease = True

        if lease == CORRUPT:
            # Can't tell what state the fans are in: assume the worst, and let
            # the staleness clock (which keeps running) decide when to act.
            key: tuple = ("unreadable",)
            manual, pid, dead, stale_s = True, None, False, self.stale_s
        else:
            key = (lease["pid"], lease["seq"])
            if key != self._key:
                self._key = key
                self._changed_at = now
            manual, pid = lease["manual"], lease["pid"]
            dead = not self.pid_alive(pid)
            stale_s = max(self.stale_s, 4 * lease["cycle_s"])

        if not manual:
            self._handled, self._fails = None, 0
            return IDLE
        age = now - self._changed_at
        if not (dead or age > stale_s):
            self._handled, self._fails = None, 0     # healthy again: any earlier episode is over
            return IDLE
        if self._handled == key:
            return IDLE
        if now < self._next_try:
            return RETRY

        why = (f"controller (pid {pid}) is no longer running" if dead
               else f"no sign of life from the controller for {age:.0f} s")
        log.warning("%s, and the fans may be in manual mode - handing them back to the EC", why)
        try:
            ok = bool(self.hand_back())
        except Exception:
            log.exception("hand-back raised")
            ok = False
        if ok:
            log.warning("fans handed back to EC auto (verified)")
            self._handled, self._fails = key, 0
            return HANDED_BACK
        self._fails += 1
        delay = min(10.0, 2.0 ** (self._fails - 1))
        self._next_try = now + delay
        log.error("hand-back NOT verified (attempt %d) - trying again in %.0f s", self._fails, delay)
        return RETRY


def make_hand_back(module_path: Path) -> Callable[[], bool]:
    """The real hand-back: open the driver, set both fans to EC auto, read back, close."""
    def hand_back() -> bool:
        from .ec import EmbeddedController
        from .pawnio import PawnIOPortIO
        from .thinkpad import ThinkPadFans
        io = None
        try:
            io = PawnIOPortIO(module_path)
            return ThinkPadFans(EmbeddedController(io, wait_timeout_s=0.5, attempts=3)).restore_auto()
        finally:
            close = getattr(io, "close", None)
            if close:
                close()
    return hand_back


def main(app_dir: Path) -> int:
    """Entry point for  CustomFanWatchdog.exe --watchdog."""
    from . import winutil
    logdir = Path(app_dir) / "logs"
    logdir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(logdir / "watchdog.log", maxBytes=200_000,
                                                      backupCount=2, encoding="utf-8")])
    log.info("=== watchdog starting (pid %d, exe=%s) ===", os.getpid(), Path(sys.executable).name)
    if not winutil.is_admin():
        log.error("needs administrator rights (PawnIO), like the controller")
        return 1
    if not winutil.claim_single_instance(MUTEX_NAME):
        log.info("another watchdog is already running - exiting")
        return 0
    module = Path(app_dir) / "LpcACPIEC.bin"
    if not module.is_file():
        log.error("%s not found", module)
        return 1
    wd = Watchdog(logdir / CONTROLLER_LEASE, make_hand_back(module))
    mine = LeaseWriter(logdir / WATCHDOG_LEASE, cycle_s=POLL_S)
    try:
        while True:
            mine.beat(manual=False)
            if wd.step() == EXIT:
                break
            time.sleep(wd.poll_s)
    finally:
        mine.clear()
        log.info("=== watchdog stopped ===")
        logging.shutdown()
    return 0


# ----------------------------------------------------------- controller side
def watchdog_command(app_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        separate = Path(app_dir) / WATCHDOG_EXE
        if separate.is_file():
            return [str(separate), "--watchdog"]
        log.warning("%s not found next to the controller; using %s instead "
                    "(taskkill /IM on that name would take both down)", WATCHDOG_EXE, Path(sys.executable).name)
        return [sys.executable, "--watchdog"]
    return [sys.executable, str(Path(app_dir) / "CustomFanController.pyw"), "--watchdog"]


def spawn(app_dir: Path) -> tuple[bool, str]:
    """Start a detached watchdog. Returns (started, detail)."""
    cmd = watchdog_command(app_dir)
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"       # a clean start for the frozen child
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, cwd=str(app_dir), env=env)
    if sys.platform != "win32":
        kw["start_new_session"] = True
        attempts = [(0, "")]
    else:
        base = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        # Try to leave the scheduled task's job object, so that stopping the
        # task (or its time limit) does not kill the watchdog with it.
        attempts = [(base | 0x01000000, "outside the task's job"), (base, "inside the task's job")]
    last = ""
    for flags, label in attempts:
        try:
            if sys.platform == "win32":
                subprocess.Popen(cmd, creationflags=flags, **kw)
            else:
                subprocess.Popen(cmd, **kw)
            # Say why an earlier attempt failed (e.g. the task's job refusing breakaway)
            return True, f"{label}; earlier attempt failed: {last}" if last else label
        except OSError as e:
            last = str(e)
    return False, last


class WatchdogKeeper:
    """Controller side: make sure a watchdog is running, and say so if it is not."""

    def __init__(self, app_dir: Path, *, spawner=spawn, alive=pid_alive,
                 max_age_s: float = 15.0, retry_s: float = 30.0) -> None:
        self.app_dir = Path(app_dir)
        self.path = self.app_dir / "logs" / WATCHDOG_LEASE
        self.spawner, self.alive = spawner, alive
        self.max_age_s, self.retry_s = max_age_s, retry_s
        self._ok: bool | None = None
        self._last_spawn = -1e9

    def _running(self) -> bool:
        lease = read_lease(self.path)
        if not isinstance(lease, dict) or not self.alive(lease["pid"]):
            return False
        t = lease.get("t")
        return isinstance(t, (int, float)) and abs(time.time() - t) < self.max_age_s

    def check(self) -> bool:
        """True if a watchdog is running (or was only just started)."""
        ok = self._running()
        spawned = False
        if not ok and time.monotonic() - self._last_spawn >= self.retry_s:
            self._last_spawn = time.monotonic()
            spawned, detail = self.spawner(self.app_dir)
            if spawned:
                log.info("Watchdog started (%s)", detail)
            else:
                log.warning("Could not start the watchdog: %s", detail)
        # A watchdog we only just started has not published its lease yet:
        # don't judge it until the next check.
        if ok != self._ok and not (spawned and not ok):
            if ok:
                log.info("Watchdog is running")
            else:
                log.warning("Watchdog is NOT running - if the controller dies now, "
                            "the fans stay at their last level")
            self._ok = ok
        return ok or spawned
