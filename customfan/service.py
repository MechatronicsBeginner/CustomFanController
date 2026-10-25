"""Control loop thread. It is the ONLY code that touches the EC.

The GUI never calls hardware; it sends commands through a queue and reads
immutable snapshots. That keeps every EC access on one thread, so the
select-then-write sequences can never interleave with each other.

Failsafes (each one hands both fans back to the EC with 0x80):
  * user picks "EC Auto"
  * hottest monitored sensor >= handover temp (resumes at resume temp)
  * CPU sensor stops reporting
  * N consecutive EC errors (retries every cycle, resumes when reads recover)
  * any unexpected exception in the loop
  * program exit or stop (normal path, plus atexit in the entry point)

Re-assert: every cycle reads both fans' 0x2F back. If the EC changed it on
its own (sleep/resume, firmware override), the level is written again.
"""
from __future__ import annotations

import csv
import logging
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from . import config as cfgmod
from .control import AUTO, CURVE, Decision, Policy, SensorFilter
from .ec import ECError, EmbeddedController
from .thinkpad import LEVEL_AUTO, FanReading, ThinkPadFans, describe_level
from .config import PROTECTED_SENSORS

log = logging.getLogger(__name__)

# status values shown by the GUI (also drives the tray icon colour)
ST_STARTING = "starting"
ST_OK = "ok"            # curve in control
ST_AUTO = "auto"        # EC Auto chosen by the user
ST_HANDOVER = "handover"  # hot / sensor issue: EC temporarily in control
ST_PAUSED = "paused"    # another program is using the EC: fans on EC auto, no polling
ST_READONLY = "readonly"
ST_FAULT = "fault"      # EC errors or a fan not spinning
ST_ERROR = "error"      # could not open the driver at all


@dataclass(frozen=True)
class Snapshot:
    time: float = 0.0
    status: str = ST_STARTING
    message: str = "Starting..."
    mode: str = "curve"
    raw: dict = field(default_factory=dict)
    temps: dict = field(default_factory=dict)
    fans: tuple = ()
    target: Optional[int] = None          # level we want on both fans (None = auto)
    decision_reason: str = ""
    hottest: Optional[tuple] = None
    cpu_level: int = 0
    gpu_level: int = 0
    stalled: tuple = ()
    ec_errors: int = 0


class CycleLog:
    """One CSV row per control cycle, for tuning the curve from evidence.
    Size-capped: rolls over to cycles.1.csv at `max_bytes`."""

    FIELDS = ["time", "status", "mode", "cpu_c", "gpu_c", "hottest", "hottest_c",
              "cpu_wants", "gpu_wants", "target", "fan1_level", "fan1_rpm",
              "fan2_level", "fan2_rpm", "ec_errors"]

    def __init__(self, path: Path, max_bytes: int = 2_000_000) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def write(self, s: "Snapshot") -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.replace(self.path.with_suffix(".1.csv"))
            new = not self.path.exists()
            fans = {f.fan: f for f in s.fans}
            f1, f2 = fans.get(1), fans.get(2)
            row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.time)), s.status, s.mode,
                   s.temps.get("CPU", ""), s.temps.get("GPU", ""),
                   s.hottest[0] if s.hottest else "", s.hottest[1] if s.hottest else "",
                   s.cpu_level, s.gpu_level, "" if s.target is None else s.target,
                   describe_level(f1.level) if f1 else "", f1.rpm if f1 else "",
                   describe_level(f2.level) if f2 else "", f2.rpm if f2 else "", s.ec_errors]
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(self.FIELDS)
                w.writerow(["" if v is None else v for v in row])
        except Exception:
            log.debug("cycle log write failed", exc_info=True)


class ControlLoop(threading.Thread):
    def __init__(self, open_io: Callable[[], object], cfg: dict, *,
                 readonly: bool = False, on_snapshot: Callable[[Snapshot], None] | None = None,
                 cycle_override_s: float | None = None, ec_wait_timeout_s: float = 0.5,
                 conflict_check: Callable[[], list] | None = None,
                 conflict_interval_s: float = 10.0, cycle_log: CycleLog | None = None) -> None:
        super().__init__(name="fan-control", daemon=True)
        self._open_io = open_io
        self.cfg = cfg
        self.readonly = readonly
        self.on_snapshot = on_snapshot
        self.cycle_override_s = cycle_override_s
        self.ec_wait_timeout_s = ec_wait_timeout_s
        self.cycle_log = cycle_log
        self.conflict_check = conflict_check or (lambda: [])
        self.conflict_interval_s = conflict_interval_s
        self._conflicts: list = []
        self._conflicts_checked = -1e9
        self._paused = False
        self._pause_restored = False
        self.commands: "queue.Queue[tuple]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._snap = Snapshot(mode=cfg["mode"])
        self._snap_lock = threading.Lock()
        self.io = None
        self.fans: ThinkPadFans | None = None
        self.policy = Policy(cfgmod.to_policy(cfg))
        self.filter = SensorFilter()
        self.ec_errors = 0
        self._last_target: object = "unset"
        self._zero_since: dict[int, float] = {}
        self._stall_reported: set[int] = set()
        self.heartbeat = time.monotonic()
        self.restored_on_exit = False

    # ---------------------------------------------------------- public API
    def snapshot(self) -> Snapshot:
        with self._snap_lock:
            return self._snap

    def set_mode(self, mode: str) -> None:
        self.commands.put(("mode", mode))

    def apply_config(self, cfg: dict) -> None:
        self.commands.put(("config", cfg))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout)

    # ------------------------------------------------------------ internals
    def _publish(self, **kw) -> None:
        with self._snap_lock:
            base = self._snap.__dict__.copy()
            base.update(kw, time=time.time(), mode=self.cfg["mode"], ec_errors=self.ec_errors)
            self._snap = Snapshot(**base)
            snap = self._snap
        if self.on_snapshot:
            try:
                self.on_snapshot(snap)
            except Exception:
                log.exception("snapshot callback failed")

    def _cycle_s(self) -> float:
        return self.cycle_override_s or float(self.cfg["cycle_s"])

    def _handle_commands(self) -> None:
        while True:
            try:
                kind, arg = self.commands.get_nowait()
            except queue.Empty:
                return
            if kind == "mode" and arg in ("curve", "auto"):
                if arg != self.cfg["mode"]:
                    log.info("Mode changed to %s", arg)
                self.cfg = {**self.cfg, "mode": arg}
            elif kind == "config":
                self.cfg = arg
                self.policy.apply_settings(cfgmod.to_policy(arg))
                self._last_target = "unset"
                log.info("Settings applied: cpu=%s gpu=%s deadband=%s handover=%s/%s",
                         arg["cpu_curve"], arg["gpu_curve"], arg["deadband_c"],
                         arg["handover_c"], arg["resume_c"])

    def _open(self) -> bool:
        try:
            self.io = self._open_io()
            self.fans = ThinkPadFans(EmbeddedController(self.io, wait_timeout_s=self.ec_wait_timeout_s))
            # probe: one sensor read proves the whole path works
            self.fans.ec.read(0x78)
        except Exception as e:
            log.error("Could not open EC access: %s", e)
            self._publish(status=ST_ERROR, message=f"Cannot access the EC: {e}")
            self._close_io()
            return False
        log.info("EC access OK (%s)", "read-only" if self.readonly else "control")
        if not self.readonly:
            self.fans.restore_auto()          # start from a known state
        return True

    def _close_io(self) -> None:
        close = getattr(self.io, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass
        self.io = None
        self.fans = None

    def _apply(self, decision: Decision, readings: list[FanReading]) -> list[FanReading]:
        """Write the desired level to any fan that isn't already there.
        Returns the readings updated with the read-back levels (no extra EC reads)."""
        desired = LEVEL_AUTO if decision.mode == AUTO else decision.level
        changed = desired != self._last_target
        if changed:
            log.info("%s %s (%s)", "Would set fans ->" if self.readonly else "Fans ->",
                     describe_level(desired), decision.reason)
            self._last_target = desired
        if self.readonly:
            return readings
        out = []
        for r in readings:
            if r.level == desired:
                out.append(r)
                continue
            if not changed:
                log.warning("Fan %d is at %s but should be %s (changed by the EC?) - re-applying",
                            r.fan, describe_level(r.level), describe_level(desired))
            got = self.fans.set_level(r.fan, desired)
            if got != desired:
                raise ECError(f"fan {r.fan} read back 0x{got:02X} after writing 0x{desired:02X}")
            out.append(replace(r, level=got))
        return out

    def _check_stalls(self, decision: Decision, readings: list[FanReading]) -> tuple:
        now = time.monotonic()
        stalled = []
        for r in readings:
            commanded_on = decision.mode == CURVE and (decision.level or 0) >= 1 and r.level == decision.level
            if commanded_on and r.rpm == 0:
                since = self._zero_since.setdefault(r.fan, now)
                if now - since >= self.cfg["stall_check_s"]:
                    stalled.append(r.fan)
            else:
                self._zero_since.pop(r.fan, None)
        return tuple(stalled)

    def _one_cycle(self) -> None:
        skip = set(self.cfg["ignore_sensors"]) - set(PROTECTED_SENSORS)
        raw = self.fans.read_sensors(skip=skip)
        temps = self.filter.update(raw)
        readings = self.fans.read_fans()
        decision = self.policy.decide(temps, self.cfg["mode"])
        readings = self._apply(decision, readings)
        stalled = self._check_stalls(decision, readings)
        self.ec_errors = 0

        would = describe_level(decision.level if decision.mode == CURVE else LEVEL_AUTO)
        if set(stalled) != self._stall_reported:
            if stalled:
                log.warning("Stall alarm: fan(s) %s commanded on but reading 0 RPM", list(stalled))
            else:
                log.info("Stall alarm cleared")
            self._stall_reported = set(stalled)

        if stalled:
            status = ST_FAULT
            msg = " / ".join(f"Fan {f} not spinning" for f in stalled) + " - check the fan"
        elif self.readonly:
            status, msg = ST_READONLY, f"Read-only test: would set level {would}"
        elif decision.mode == AUTO and self.cfg["mode"] == "auto":
            status, msg = ST_AUTO, "EC Auto (BIOS curve)"
        elif decision.mode == AUTO:
            status, msg = ST_HANDOVER, decision.reason
        else:
            status, msg = ST_OK, f"Level {decision.level} ({decision.reason})"

        self._publish(status=status, message=msg, raw=raw, temps=temps, fans=tuple(readings),
                      target=decision.level if decision.mode == CURVE else None,
                      decision_reason=decision.reason, hottest=decision.hottest,
                      cpu_level=decision.cpu_level, gpu_level=decision.gpu_level,
                      stalled=stalled)
        if self.cycle_log:
            self.cycle_log.write(self.snapshot())

    def _current_conflicts(self) -> list:
        now = time.monotonic()
        if now - self._conflicts_checked >= self.conflict_interval_s:
            self._conflicts_checked = now
            try:
                self._conflicts = list(self.conflict_check())
            except Exception:
                log.exception("conflict check failed")
                self._conflicts = []
        return self._conflicts

    def _pause(self, who: list) -> None:
        """Another program is reading the EC without the shared lock. Our reads
        would be corrupted (and would corrupt its reads), so hand the fans to
        the EC once, verified, and stop polling until it closes."""
        if not self._paused:
            log.warning("%s is accessing the EC - pausing (fans to EC auto) until it closes",
                        ", ".join(who))
            self._paused = True
            self._pause_restored = self.readonly
        if not self._pause_restored:
            self._pause_restored = self.fans.restore_auto()   # retried each cycle until verified
        self._publish(status=ST_PAUSED, raw={}, temps={}, fans=(), target=None, hottest=None,
                      cpu_level=0, gpu_level=0, stalled=(),
                      message=f"{', '.join(who)} is reading the EC - fans on EC auto until it closes"
                              + ("" if self._pause_restored else " (retrying hand-back)"))

    def _resume_from_pause(self) -> None:
        log.info("EC conflict gone - resuming control")
        self._paused = False
        self.filter = SensorFilter()
        self.policy.reset()
        self._last_target = "unset"

    def run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                self.heartbeat = time.monotonic()
                if self.fans is None:
                    if not self._open():
                        self._stop_evt.wait(10)
                        continue
                self._handle_commands()
                conflicts = [] if self.cfg.get("allow_hwinfo") else self._current_conflicts()
                if conflicts:
                    self._pause(conflicts)
                    self._stop_evt.wait(self._cycle_s())
                    continue
                if self._paused:
                    self._resume_from_pause()
                try:
                    self._one_cycle()
                except ECError as e:
                    self.ec_errors += 1
                    log.warning("EC error %d/%d: %s", self.ec_errors, self.cfg["max_ec_errors"], e)
                    self._last_target = "unset"
                    if self.ec_errors >= self.cfg["max_ec_errors"]:
                        if not self.readonly:
                            self.fans.restore_auto()
                        self._publish(status=ST_FAULT,
                                      message=f"EC not responding ({self.ec_errors} errors) - "
                                              "fans handed to EC, retrying")
                    else:
                        self._publish(message=f"EC busy, retrying ({self.ec_errors}/"
                                              f"{self.cfg['max_ec_errors']})")
                except Exception as e:      # programming error: fail safe, keep running
                    log.exception("Unexpected error in control loop")
                    if not self.readonly and self.fans:
                        self.fans.restore_auto()
                    self._last_target = "unset"
                    self._publish(status=ST_FAULT, message=f"Internal error: {e}")
                self._stop_evt.wait(self._cycle_s())
        finally:
            self.shutdown_hardware()

    def shutdown_hardware(self) -> None:
        """Hand fans back to the EC. Safe to call more than once."""
        if self.restored_on_exit:
            return
        self.restored_on_exit = True
        if self.fans and not self.readonly:
            ok = self.fans.restore_auto()
            log.info("Exit: fans handed back to EC auto (%s)", "verified" if ok else "NOT verified")
        self._close_io()
