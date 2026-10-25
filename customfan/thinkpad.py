"""ThinkPad EC register map, verified on this P1 Gen 6 with TPFanCtrl2 v2.8.2.

    0x31        fan selector (0x00 = fan 1, 0x01 = fan 2). Registers 0x2F and
                0x84/0x85 are a window onto whichever fan is selected.
    0x2F        fan mode/level for the selected fan:
                  0..7  manual level
                  0x80  EC automatic (firmware curve)
                  0x40  "disengaged" (unregulated) - deliberately NOT allowed here
    0x84/0x85   selected fan's tachometer, low byte then high byte (read 0x84 first)
    0x78..0x7F  temperature sensors, 0xC0..0xC3 extended slots
                0x80 (and 0) mean "no data" on this machine
"""
from __future__ import annotations

from dataclasses import dataclass

from .ec import ECError, EmbeddedController

REG_FAN_LEVEL = 0x2F
REG_FAN_SELECT = 0x31
REG_RPM_LO = 0x84
REG_RPM_HI = 0x85

FAN_SELECT = {1: 0x00, 2: 0x01}
FANS = (1, 2)

LEVEL_AUTO = 0x80
MANUAL_LEVELS = tuple(range(8))
# Highest plausible tachometer value. Level 7 runs ~4,300 RPM; even disengaged
# mode stays well under this. Anything above means the read was corrupted.
MAX_PLAUSIBLE_RPM = 8000
ALLOWED_LEVELS = frozenset(MANUAL_LEVELS) | {LEVEL_AUTO}

# (register, name) - names match what TPFanCtrl2 shows for this machine.
SENSORS: tuple[tuple[int, str], ...] = (
    (0x78, "CPU"), (0x79, "APS"), (0x7A, "PCM"), (0x7B, "GPU"),
    (0x7C, "BAT1"), (0x7D, "X7D"), (0x7E, "BAT2"), (0x7F, "X7F"),
    (0xC0, "BUS"), (0xC1, "PCI"), (0xC2, "PWR"), (0xC3, "XC3"),
)


def describe_level(level: int | None) -> str:
    if level is None:
        return "?"
    if level == LEVEL_AUTO:
        return "auto"
    if level in MANUAL_LEVELS:
        return str(level)
    return f"0x{level:02X}"


@dataclass
class FanReading:
    fan: int
    level: int      # raw 0x2F value for this fan
    rpm: int


class ThinkPadFans:
    """All multi-register sequences run inside one EC transaction (lock held),
    and the selector is always left on fan 1 afterwards - the state other
    tools (and the EC itself) expect."""

    def __init__(self, ec: EmbeddedController) -> None:
        self.ec = ec

    def _select(self, fan: int) -> None:
        self.ec.write(REG_FAN_SELECT, FAN_SELECT[fan])

    def read_sensors(self, skip: frozenset[str] | set[str] = frozenset()) -> dict[str, int]:
        """Read the sensor registers, except those in `skip`. Every read we
        don't make is one less chance to collide with Windows' own EC traffic."""
        with self.ec.transaction():
            return {name: self.ec.read(reg) for reg, name in SENSORS if name not in skip}

    def read_fans(self) -> list[FanReading]:
        out = []
        with self.ec.transaction():
            try:
                for fan in (2, 1):              # finishing on fan 1 leaves the selector there
                    self._select(fan)
                    level = self.ec.read(REG_FAN_LEVEL)
                    lo = self.ec.read(REG_RPM_LO)
                    hi = self.ec.read(REG_RPM_HI)
                    rpm = (hi << 8) | lo
                    if rpm > MAX_PLAUSIBLE_RPM:
                        raise ECError(f"implausible fan {fan} reading (level 0x{level:02X}, {rpm} RPM) - "
                                      "another program may be accessing the EC")
                    out.append(FanReading(fan, level, rpm))
            except BaseException:
                self._restore_selector()        # a failure may have left fan 2 selected
                raise
        return sorted(out, key=lambda r: r.fan)

    def set_level(self, fan: int, level: int) -> int:
        """Select, write, read back. Returns the read-back value."""
        if level not in ALLOWED_LEVELS:
            raise ValueError(f"refusing fan level 0x{level:02X}: only 0-7 and 0x80 are allowed")
        with self.ec.transaction():
            try:
                self._select(fan)
                self.ec.write(REG_FAN_LEVEL, level)
                return self.ec.read(REG_FAN_LEVEL)
            finally:
                if fan != 1:
                    self._restore_selector()

    def set_all(self, level: int) -> bool:
        """Set both fans; True only if both read back correctly."""
        with self.ec.transaction():
            return all(self.set_level(fan, level) == level for fan in FANS)

    def restore_auto(self) -> bool:
        """Best effort: hand both fans back to the EC. Never raises."""
        ok = True
        for fan in FANS:
            try:
                ok &= self.set_level(fan, LEVEL_AUTO) == LEVEL_AUTO
            except Exception:
                ok = False
        return ok

    def _restore_selector(self) -> None:
        try:
            self._select(1)
        except Exception:
            pass
