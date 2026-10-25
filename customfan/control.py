"""Pure control logic - no hardware, fully unit-testable.

StepCurve      temperature -> fan level, with a step-down deadband (hysteresis)
               and a step-down delay (dwell time)
SensorFilter   rejects the EC's "no data" values (0x80, 0) and briefly holds
               the last good reading so a single glitch can't stop the fans
Policy         combines CPU and GPU curves (the hotter request wins) and decides
               when to hand control back to the EC
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

AUTO = "auto"
CURVE = "curve"


class StepCurve:
    """Steps are (temp_c, level) pairs, temps strictly increasing.

    Below the first step the fan is off (level 0).
    Rising:  a step engages as soon as temp >= its threshold (no delay, ever).
    Falling: a step only disengages once temp < threshold - deadband, AND that
             has stayed true for `down_delay_s` seconds without interruption.

    The deadband is hysteresis in temperature; the delay is hysteresis in time.
    The deadband stops flapping around one threshold; the delay stops the fans
    dropping on a short dip (a game loading screen, or a single bad read) and
    then ramping straight back up.

    Example with steps (52,1) (58,3), deadband 4, delay 15 s:
        fan turns on at 52, turns off only after 15 s continuously below 48.
    """

    def __init__(self, steps: list[tuple[int, int]], deadband: int, down_delay_s: float = 0.0) -> None:
        self.steps = [(int(t), int(l)) for t, l in steps]
        self.deadband = int(deadband)
        self.down_delay_s = float(down_delay_s)
        self._idx = -1          # -1 = below all steps (fan off)
        self._below_since: Optional[float] = None

    def reset(self) -> None:
        self._idx = -1
        self._below_since = None

    @property
    def level(self) -> int:
        return 0 if self._idx < 0 else self.steps[self._idx][1]

    def target_index(self, temp: float) -> int:
        idx = -1
        for i, (t, _) in enumerate(self.steps):
            if temp >= t:
                idx = i
        return idx

    def update(self, temp: float, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        target = self.target_index(temp)
        if target > self._idx:                      # heating: step up immediately
            self._idx = target
            self._below_since = None
            return self.level
        idx = self._idx                             # how far down the deadband allows
        while idx > target and temp < self.steps[idx][0] - self.deadband:
            idx -= 1
        if idx < self._idx:
            if self._below_since is None:
                self._below_since = now
            if now - self._below_since >= self.down_delay_s:
                self._idx = idx
                self._below_since = None
        else:
            self._below_since = None                # condition broken: restart the timer
        return self.level

    @staticmethod
    def preview(steps: list[tuple[int, int]], temp: float) -> int:
        """Level for `temp` ignoring hysteresis (for the settings preview)."""
        lvl = 0
        for t, l in steps:
            if temp >= t:
                lvl = l
        return lvl


def is_valid_temp(raw: Optional[int]) -> bool:
    return raw is not None and 0 < raw < 0x80


class SensorFilter:
    """Two guards against bad EC reads:

    * "no data" values (0x80, 0, >=128): hold the last valid value for up to
      `hold_cycles` reads, then report None.
    * Implausible jumps: a change larger than `jump_c` from the last accepted
      value is only accepted if the next read confirms it (within
      `confirm_c`). A corrupted byte that happens to look like a temperature
      can then cost at most one cycle at the old value, never a wrong decision.
    """

    def __init__(self, hold_cycles: int = 3, jump_c: int = 15, confirm_c: int = 5) -> None:
        self.hold_cycles = hold_cycles
        self.jump_c = jump_c
        self.confirm_c = confirm_c
        self._last: dict[str, int] = {}
        self._bad: dict[str, int] = {}
        self._pending: dict[str, int] = {}

    def update(self, raw: dict[str, int]) -> dict[str, Optional[int]]:
        out: dict[str, Optional[int]] = {}
        for name, value in raw.items():
            if is_valid_temp(value):
                self._bad[name] = 0
                last = self._last.get(name)
                if last is not None and abs(value - last) > self.jump_c:
                    pend = self._pending.get(name)
                    if pend is None or abs(value - pend) > self.confirm_c:
                        self._pending[name] = value      # wait for a second read to agree
                        out[name] = last
                        continue
                self._pending.pop(name, None)
                self._last[name] = value
                out[name] = value
            else:
                n = self._bad.get(name, 0) + 1
                self._bad[name] = n
                out[name] = self._last.get(name) if n <= self.hold_cycles else None
        return out


@dataclass
class Decision:
    mode: str                     # CURVE or AUTO
    level: Optional[int] = None   # set when mode == CURVE
    reason: str = ""
    cpu_level: int = 0
    gpu_level: int = 0
    hottest: Optional[tuple[str, int]] = None
    handover: bool = False


@dataclass
class PolicySettings:
    cpu_steps: list[tuple[int, int]]
    gpu_steps: list[tuple[int, int]]
    deadband: int = 4
    down_delay_s: float = 15.0
    handover_c: int = 92
    resume_c: int = 85
    ignore: set[str] = field(default_factory=set)


class Policy:
    def __init__(self, s: PolicySettings) -> None:
        self.apply_settings(s)
        self._handover = False

    def apply_settings(self, s: PolicySettings) -> None:
        self.s = s
        self.cpu = StepCurve(s.cpu_steps, s.deadband, s.down_delay_s)
        self.gpu = StepCurve(s.gpu_steps, s.deadband, s.down_delay_s)

    def reset(self) -> None:
        self.cpu.reset()
        self.gpu.reset()

    def hottest(self, temps: dict[str, Optional[int]]) -> Optional[tuple[str, int]]:
        cands = [(n, v) for n, v in temps.items() if v is not None and n not in self.s.ignore]
        return max(cands, key=lambda nv: nv[1]) if cands else None

    def decide(self, temps: dict[str, Optional[int]], user_mode: str,
               now: Optional[float] = None) -> Decision:
        hottest = self.hottest(temps)

        if user_mode == AUTO:
            self.reset()
            self._handover = False
            return Decision(AUTO, reason="EC Auto selected", hottest=hottest)

        cpu = temps.get("CPU")
        if cpu is None:
            self.reset()
            return Decision(AUTO, reason="CPU sensor not reporting - EC in control", hottest=hottest)

        hot = hottest[1] if hottest else cpu
        if self._handover:
            if hot <= self.s.resume_c:
                self._handover = False
                self.reset()
        elif hot >= self.s.handover_c:
            self._handover = True
        if self._handover:
            self.reset()
            return Decision(AUTO, reason=f"{hottest[0] if hottest else 'CPU'} at {hot}°C - "
                                         f"EC in control until {self.s.resume_c}°C",
                            hottest=hottest, handover=True)

        cpu_lvl = self.cpu.update(cpu, now)
        gpu = temps.get("GPU")
        gpu_lvl = self.gpu.update(gpu, now) if gpu is not None else 0
        level = max(cpu_lvl, gpu_lvl)
        src = "GPU" if gpu_lvl > cpu_lvl else "CPU"
        return Decision(CURVE, level=level, reason=f"{src} curve", cpu_level=cpu_lvl,
                        gpu_level=gpu_lvl, hottest=hottest)
