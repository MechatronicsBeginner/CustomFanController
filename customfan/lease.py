"""A small "lease" file: proof that a process is alive, and what it has done to the fans.

Why this exists
---------------
The EC keeps obeying the last manual fan level it was given. If the controller
dies without handing the fans back (Task Manager "End task", a crash, a hard
hang), nothing is left that could fix it: the one process that knew about the
problem is the one that is gone. So the controller writes a lease on every loop
and a separate process (watchdog.py) reads it.

What is in it
-------------
  pid      who wrote it. The watchdog checks that process is still alive.
  seq      counts up on every write. The watchdog measures "no new seq for N
           seconds" on ITS OWN clock, so clock changes and sleep cannot fool it.
  manual   True from just BEFORE the first manual fan level is written until a
           hand-back to EC auto has been verified by reading the EC back.
           False means "the EC is in charge", so the watchdog has nothing to do.
  cycle_s  how often a new seq is expected (the watchdog scales its patience).
  t        wall-clock time, for humans and for the watchdog's own freshness check.

The file is replaced atomically (write a temp file, then os.replace), so a
reader sees either the old lease or the new one, never half of one, even if
the writer is killed in the middle of a write.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

VERSION = 1
MISSING = "missing"        # no lease file: never started, or removed on a clean exit
CORRUPT = "corrupt"        # exists but unreadable right now, or not a valid lease


class LeaseWriter:
    def __init__(self, path: Path, *, cycle_s: float = 3.0, pid: int | None = None) -> None:
        self.path = Path(path)
        self.cycle_s = float(cycle_s)
        self.pid = os.getpid() if pid is None else pid
        self.seq = 0
        self.failures = 0           # consecutive failed writes

    def beat(self, manual: bool) -> bool:
        """Publish "still alive" plus whether the fans may be in manual mode.
        Returns False if the lease could not be written."""
        self.seq += 1
        data = {"v": VERSION, "pid": self.pid, "seq": self.seq, "manual": bool(manual),
                "cycle_s": self.cycle_s, "t": round(time.time(), 1)}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    # Windows refuses to replace a file another process has open
                    # for a moment (the watchdog reading it). Try again shortly.
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
            self.failures = 0
            return True
        except OSError as e:
            self.failures += 1
            if self.failures == 3:      # once, not every cycle
                log.warning("Cannot write %s (%s) - the watchdog cannot protect the fans "
                            "until this is fixed", self.path, e)
            return False

    def clear(self) -> None:
        """Remove the lease: tells the watchdog this was a clean exit."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("could not remove %s", self.path, exc_info=True)


def read_lease(path: Path) -> Union[dict, str]:
    """A dict {pid, seq, manual, cycle_s, t}, or MISSING, or CORRUPT."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return MISSING
    except OSError:
        return CORRUPT
    try:
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get("v") != VERSION or not isinstance(d.get("manual"), bool):
            return CORRUPT
        cycle = float(d.get("cycle_s", 3.0))
        return {"pid": int(d["pid"]), "seq": int(d["seq"]), "manual": d["manual"],
                "cycle_s": cycle if 0 < cycle < 3600 else 3.0, "t": d.get("t")}
    except (ValueError, KeyError, TypeError):
        return CORRUPT


def pid_alive(pid: int) -> bool:
    """True if a process with this id is still running.

    On Windows this asks for a handle and checks whether it is signalled, which
    also catches a process that has exited while something still holds a
    handle to it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        SYNCHRONIZE, QUERY_LIMITED = 0x00100000, 0x1000
        h = k32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, pid)
        if not h:
            # 87 = no such process. 5 = exists but we may not open it: alive.
            return ctypes.get_last_error() == 5
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102      # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
