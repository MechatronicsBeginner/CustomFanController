"""The program's log as structured events, for the Events page and the report.

Two sources feed the same list: the tail of logs/CustomFanController.log (so a
restart doesn't empty the page) and a logging handler that captures everything
logged from now on. Pure logic, no Tk: the page only filters and shows these.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

LEVELS_PROBLEM = ("WARNING", "ERROR", "CRITICAL")
CHANGE_PREFIXES = ("Fans ->", "Would set fans ->", "Mode changed", "Settings applied", "Settings saved",
                   "Stall alarm cleared", "EC conflict gone")
FILTERS = (("all", "All"), ("changes", "Changes"), ("problems", "Problems"))

# 2026-10-24 21:07:48,563 INFO    customfan.service: EC access OK (control)
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) (\w+)\s+([\w.]+): (.*)$")


@dataclass(frozen=True, slots=True)
class LogEntry:
    t: float
    level: str
    source: str
    text: str                    # may hold several lines (a traceback)

    @property
    def headline(self) -> str:
        first, _, rest = self.text.partition("\n")
        return first + (f"   (+{rest.count(chr(10)) + 1} lines in the log)" if rest else "")

    @property
    def is_problem(self) -> bool:
        return self.level in LEVELS_PROBLEM

    @property
    def is_change(self) -> bool:
        return self.text.startswith(CHANGE_PREFIXES)


def parse_lines(lines: Iterable[str]) -> list[LogEntry]:
    """Parse log lines; indented/unmatched lines (tracebacks) join the entry before them."""
    out: list[LogEntry] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = _LINE.match(line)
        if m:
            try:
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000.0
            except (ValueError, OverflowError):
                continue
            out.append(LogEntry(t, m.group(3), m.group(4), m.group(5)))
        elif out and line.strip():
            last = out[-1]
            out[-1] = LogEntry(last.t, last.level, last.source, last.text + "\n" + line)
    return out


def read_tail(path: Path, max_entries: int = 400, max_bytes: int = 300_000) -> list[LogEntry]:
    """The newest entries of a log file. Missing or unreadable file -> empty list."""
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                       # the first line is probably cut in half
    return parse_lines(lines)[-max_entries:]


def matches(entry: LogEntry, flt: str) -> bool:
    if flt == "changes":
        return entry.is_change
    if flt == "problems":
        return entry.is_problem
    return True


def filtered(entries: Iterable[LogEntry], flt: str) -> list[LogEntry]:
    return [e for e in entries if matches(e, flt)]


class LogBuffer(logging.Handler):
    """Keeps the newest records logged by this program in memory (bounded)."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self.entries: deque[LogEntry] = deque(maxlen=maxlen)
        self.version = 0
        self._lock = threading.Lock()       # the control loop writes, the window thread reads (cheap insurance)

    def prefill(self, entries: Iterable[LogEntry]) -> None:
        with self._lock:
            self.entries.extend(entries)
            self.version += 1

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("customfan"):
                return
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            with self._lock:
                self.entries.append(LogEntry(record.created, record.levelname, record.name, text))
                self.version += 1
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[LogEntry]:
        with self._lock:
            return list(self.entries)


def latest(entries: list[LogEntry], n: int, flt: str = "all") -> list[LogEntry]:
    return filtered(entries, flt)[-n:]


def last_problem(entries: list[LogEntry]) -> Optional[LogEntry]:
    for e in reversed(entries):
        if e.is_problem:
            return e
    return None
