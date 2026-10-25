"""ACPI embedded-controller protocol over two I/O ports.

The EC is a single shared mailbox: a status/command port (0x66) and a data
port (0x62). The handshake below mirrors ECManager.cpp in TPFanCtrl2 v2.8.2
step for step, because that is the version with evidence on this laptop
(1,423 fan reads, zero corrupted). Reading one register:

    drain stale output -> wait IBF=0 AND OBF=0 -> write 0x80 (READ) to 0x66
    -> wait IBF=0 -> write register to 0x62 -> wait IBF=0 -> read 0x62

The "AND OBF=0" before the command is the important part. Windows' own ACPI
driver uses the same mailbox without our lock. If one of its bytes is sitting
in the output buffer, we wait for it to be collected instead of starting our
transaction on top of it - otherwise that foreign byte comes back as our
answer (v1.2 did this: fans read as 0x90 / 37008 RPM).

Writing is the same with 0x81 (WRITE) and a final data byte. Every wait has
a timeout, so a stuck EC produces an ECError instead of a frozen program.

The whole handshake runs while holding the system-wide "Access_EC" mutex
(the same lock TPFanCtrl2 and other EC tools use). `transaction()` lets a
caller hold that lock across several register operations, e.g.
"select fan 2 -> write level -> read it back", so no other tool can move
the fan selector in the middle.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Protocol

CMD_PORT = 0x66
DATA_PORT = 0x62
FLAG_OBF = 0x01  # output buffer full: EC has a byte for us
FLAG_IBF = 0x02  # input buffer full: EC hasn't consumed our last byte yet
RD_EC = 0x80
WR_EC = 0x81


class ECError(RuntimeError):
    """EC did not respond in time, or the port backend failed."""


class PortIO(Protocol):
    """Minimal backend: raw port access plus the cross-process EC lock."""

    def read_port(self, port: int) -> int: ...
    def write_port(self, port: int, value: int) -> None: ...
    def acquire_lock(self, timeout_ms: int) -> bool: ...
    def release_lock(self) -> None: ...


class EmbeddedController:
    POLL_S = 0.010          # TPFanCtrl2 polls status every 10 ms while waiting

    def __init__(self, io: PortIO, wait_timeout_s: float = 1.0,
                 lock_timeout_ms: int = 1000, attempts: int = 2) -> None:
        self.io = io
        self.wait_timeout_s = wait_timeout_s
        self.lock_timeout_ms = lock_timeout_ms
        self.attempts = attempts
        self._depth = threading.local()

    # ---------------------------------------------------------------- locking
    @contextmanager
    def transaction(self):
        """Hold the cross-process EC lock for the duration of the block.

        Re-entrant within one thread: nested transactions don't re-acquire.
        """
        depth = getattr(self._depth, "n", 0)
        if depth == 0:
            if not self.io.acquire_lock(self.lock_timeout_ms):
                raise ECError("timed out waiting for the Access_EC lock "
                              "(another program is holding the EC)")
        self._depth.n = depth + 1
        try:
            yield self
        finally:
            self._depth.n -= 1
            if self._depth.n == 0:
                self.io.release_lock()

    # ------------------------------------------------------------- handshake
    def _status(self) -> int:
        return self.io.read_port(CMD_PORT) & 0xFF

    def _wait_clear(self, flags: int, what: str, reg: int) -> None:
        """Wait until all `flags` are clear (TPFanCtrl2's WaitForFlags(..., false))."""
        deadline = time.perf_counter() + self.wait_timeout_s
        while True:
            if not (self._status() & flags):
                return
            if time.perf_counter() > deadline:
                raise ECError(f"EC timeout waiting for {what} (register 0x{reg:02X})")
            time.sleep(self.POLL_S)

    def _drain_obf(self) -> None:
        """Discard a stale byte left in the output buffer (as TPFanCtrl2 does)."""
        for _ in range(10):
            if not (self._status() & FLAG_OBF):
                return
            self.io.read_port(DATA_PORT)
            time.sleep(0.001)

    def _read_once(self, reg: int) -> int:
        self._drain_obf()
        self._wait_clear(FLAG_IBF | FLAG_OBF, "IBF and OBF clear before command", reg)
        self.io.write_port(CMD_PORT, RD_EC)
        self._wait_clear(FLAG_IBF, "IBF clear after read command", reg)
        self.io.write_port(DATA_PORT, reg)
        self._wait_clear(FLAG_IBF, "IBF clear after address", reg)
        return self.io.read_port(DATA_PORT) & 0xFF

    def _write_once(self, reg: int, value: int) -> None:
        self._drain_obf()
        self._wait_clear(FLAG_IBF | FLAG_OBF, "IBF and OBF clear before command", reg)
        self.io.write_port(CMD_PORT, WR_EC)
        self._wait_clear(FLAG_IBF, "IBF clear after write command", reg)
        self.io.write_port(DATA_PORT, reg)
        self._wait_clear(FLAG_IBF, "IBF clear after address", reg)
        self.io.write_port(DATA_PORT, value)
        self._wait_clear(FLAG_IBF, "IBF clear after data", reg)

    def _retry(self, fn, *args):
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return fn(*args)
            except ECError as e:
                last = e
                time.sleep(0.01 * (attempt + 1))
        raise ECError(f"{last} (after {self.attempts} attempts)")

    # ------------------------------------------------------------ public API
    def read(self, reg: int) -> int:
        if not 0 <= reg <= 0xFF:
            raise ValueError(f"register out of range: {reg}")
        with self.transaction():
            return self._retry(self._read_once, reg)

    def write(self, reg: int, value: int) -> None:
        if not 0 <= reg <= 0xFF or not 0 <= value <= 0xFF:
            raise ValueError(f"bad register write: 0x{reg:X} <= 0x{value:X}")
        with self.transaction():
            self._retry(self._write_once, reg, value)
