"""A software model of the P1 Gen 6 EC, used by the tests and by --simulate.

It emulates the real port-level handshake (IBF/OBF flags, 0x80/0x81 commands),
the fan selector at 0x31, per-fan 0x2F/0x84/0x85 registers, sensors that
sometimes report 0x80, and a crude thermal model so the GUI has something to
show. Fault injection hooks let tests simulate a busy or dead EC, a stalled
fan, or the EC resetting fans to auto (as after sleep).
"""
from __future__ import annotations

import math
import random
import threading
import time

from .ec import CMD_PORT, DATA_PORT, FLAG_IBF, FLAG_OBF, RD_EC, WR_EC

RPM_TABLE = {0: 0, 1: 2050, 2: 2300, 3: 2600, 4: 3000, 5: 3400, 6: 3800, 7: 4300}


class SimulatedEC:
    def __init__(self, *, thermal: bool = True, seed: int | None = 1) -> None:
        self.regs = bytearray(256)
        self.fan_level = {0: 0x80, 1: 0x80}      # keyed by selector value
        self.regs[0x31] = 0x00
        self.temps = {"CPU": 50.0, "GPU": 37.0}
        self.static = {0x79: 0x80, 0x7A: 49, 0x7C: 0x80, 0x7D: 44, 0x7E: 45, 0x7F: 0x80,
                       0xC0: 0x00, 0xC1: 0x01, 0xC2: 0x01, 0xC3: 0x08}
        self.thermal = thermal
        self.rng = random.Random(seed)
        self.load = 0.2                 # 0..1, drives CPU heat in the model
        self._lock = threading.RLock()
        self._t_last = time.monotonic()
        # port-level state
        self._state = "idle"
        self._cmd = 0
        self._addr = 0
        self._obf = False
        self._data = 0
        # fault injection
        self.busy_polls = 0              # IBF stays set for N status reads
        self.dead = False                # EC never clears IBF
        self.stalled_fans: set[int] = set()   # selector values whose fan won't spin
        self.writes: list[tuple[int, int]] = []   # (register, value) log
        self.reads = 0                       # register reads served
        self.corrupt_byte: int | None = None  # every register read returns this (interleaving simulation)
        # Another master (e.g. Windows' ACPI driver) leaves a byte in the output
        # buffer: on status read number `foreign_at`, OBF is set with `foreign_value`,
        # and the other master collects it after `foreign_hold` more status reads.
        self.foreign_at: int | None = None
        self.foreign_value = 0x90
        self.foreign_hold = 10
        self._status_reads = 0
        self._foreign_left = 0
        self._queued: list[int] = []        # EC answers waiting behind a full output buffer

    # ----------------------------------------------------------- PortIO API
    def acquire_lock(self, timeout_ms: int) -> bool:
        return self._lock.acquire(timeout=timeout_ms / 1000)

    def release_lock(self) -> None:
        self._lock.release()

    def read_port(self, port: int) -> int:
        if port == CMD_PORT:
            self._status_reads += 1
            if self.foreign_at is not None and self._status_reads == self.foreign_at:
                self._obf, self._data, self._foreign_left = True, self.foreign_value, self.foreign_hold
            elif self._foreign_left > 0:
                self._foreign_left -= 1
                if self._foreign_left == 0 and self._data == self.foreign_value:
                    # the other master reads its byte; anything queued moves up
                    if self._queued:
                        self._data = self._queued.pop(0)
                    else:
                        self._obf = False
            status = FLAG_OBF if self._obf else 0
            if self.dead:
                status |= FLAG_IBF
            elif self.busy_polls > 0:
                self.busy_polls -= 1
                status |= FLAG_IBF
            return status
        if port == DATA_PORT:
            value = self._data
            if self._queued:                 # the next byte moves into the output buffer
                self._data = self._queued.pop(0)
                self._obf = True
            else:
                self._obf = False
            self._foreign_left = 0
            return value
        raise ValueError(port)

    def write_port(self, port: int, value: int) -> None:
        if self.dead:
            return
        if port == CMD_PORT:
            if value in (RD_EC, WR_EC):
                self._cmd, self._state = value, "addr"
            return
        if port == DATA_PORT:
            if self._state == "addr":
                self._addr = value
                if self._cmd == RD_EC:
                    answer = self._read_reg(value)
                    if self._obf:                # one-byte buffer still full: answer waits behind it
                        self._queued.append(answer)
                    else:
                        self._data, self._obf = answer, True
                    self._state = "idle"
                else:
                    self._state = "data"
            elif self._state == "data":
                self._write_reg(self._addr, value)
                self._state = "idle"

    # ------------------------------------------------------- register model
    def _sel(self) -> int:
        return self.regs[0x31] & 0x01

    def _read_reg(self, reg: int) -> int:
        self.reads += 1
        if self.corrupt_byte is not None:
            return self.corrupt_byte
        self._tick()
        if reg == 0x2F:
            return self.fan_level[self._sel()]
        if reg in (0x84, 0x85):
            rpm = self.rpm(self._sel())
            return rpm & 0xFF if reg == 0x84 else (rpm >> 8) & 0xFF
        if reg == 0x78:
            return int(round(self.temps["CPU"]))
        if reg == 0x7B:
            return int(round(self.temps["GPU"]))
        if reg in self.static:
            v = self.static[reg]
            # APS/BAT1 flicker between real values and 0x80, like the real machine
            if reg in (0x79, 0x7C) and self.rng.random() < 0.5:
                return 50
            return v
        return self.regs[reg]

    def _write_reg(self, reg: int, value: int) -> None:
        self.writes.append((reg, value))
        if reg == 0x2F:
            self.fan_level[self._sel()] = value
        else:
            self.regs[reg] = value

    def effective_level(self, sel: int) -> int:
        lvl = self.fan_level[sel]
        if lvl == 0x80:                 # firmware curve
            cpu = self.temps["CPU"]
            return 0 if cpu < 55 else 2 if cpu < 62 else 4 if cpu < 72 else 7
        return lvl & 0x07

    def rpm(self, sel: int) -> int:
        if sel in self.stalled_fans:
            return 0
        base = RPM_TABLE[self.effective_level(sel)]
        return int(base * (0.88 if sel == 1 else 1.0))

    def _tick(self) -> None:
        if not self.thermal:
            return
        now = time.monotonic()
        dt = min(now - self._t_last, 5.0)
        if dt < 0.05:
            return
        self._t_last = now
        # wandering load with occasional bursts
        self.load = max(0.0, min(1.0, self.load + self.rng.uniform(-0.05, 0.05)
                                 + (0.5 if self.rng.random() < 0.01 else 0)))
        airflow = (self.rpm(0) + self.rpm(1)) / 8600       # 0..1
        target = 38 + 62 * self.load * (1 - 0.45 * airflow)
        self.temps["CPU"] += (target - self.temps["CPU"]) * (1 - math.exp(-dt / 6))
        gtarget = 36 + 0.35 * (self.temps["CPU"] - 38)
        self.temps["GPU"] += (gtarget - self.temps["GPU"]) * (1 - math.exp(-dt / 15))

    # --------------------------------------------------------- test helpers
    def ec_reset_to_auto(self) -> None:
        """What the firmware may do on its own, e.g. after resume from sleep."""
        self.fan_level = {0: 0x80, 1: 0x80}

    def set_cpu(self, t: float) -> None:
        self.thermal = False
        self.temps["CPU"] = t
