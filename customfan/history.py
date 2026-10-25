"""Rolling history and session statistics for the dashboard.

Pure logic: no Tk, no hardware. The GUI feeds it one Snapshot per control
cycle and asks it for slices of the last few minutes or hours.

The newest data is pre-filled from logs/cycles.csv when the program starts, so
the charts are not empty after a restart. A gap in the data (sleep, a pause
for HWiNFO, the program not running) is never drawn as a straight line across
the gap: split_segments() breaks the line there.
"""
from __future__ import annotations

import csv
import logging
import time
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

LEVEL_AUTO = 0x80
GAP_CAP_S = 15.0        # time between two samples longer than this is not counted as "time at a level"
LEVEL_NOT_OK = ("handover", "paused", "fault", "error")


@dataclass(frozen=True, slots=True)
class Sample:
    t: float
    cpu: Optional[int]
    gpu: Optional[int]
    hottest_c: Optional[int]
    rpm1: Optional[int]
    rpm2: Optional[int]
    lvl1: Optional[int]      # raw level register: 0-7, 0x80 = EC auto
    lvl2: Optional[int]
    status: str
    ec_errors: int


def parse_level(text: str) -> Optional[int]:
    text = (text or "").strip().lower()
    if not text:
        return None
    if text == "auto":
        return LEVEL_AUTO
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


def _int(text: str) -> Optional[int]:
    try:
        return int(float(text)) if text not in ("", None) else None
    except ValueError:
        return None


def describe(level: Optional[int]) -> str:
    if level is None:
        return "?"
    return "EC auto" if level == LEVEL_AUTO else f"level {level}"


class History:
    def __init__(self, max_age_s: float = 7200 + 120, clock=time.time) -> None:
        self.max_age_s = max_age_s
        self.clock = clock
        self.started = clock()               # when this program started (for "running for ...")
        self._s: list[Sample] = []
        self._t: list[float] = []
        self.version = 0                     # bumped whenever data is added
        self.events: deque = deque(maxlen=300)   # (time, text)
        self.sensor_stats: dict[str, list[int]] = {}    # name -> [lowest, highest] since the program started

    # ------------------------------------------------------------- adding
    def add(self, s: Sample) -> bool:
        if self._t and s.t <= self._t[-1]:
            return False                    # same cycle seen twice, or clock went backwards
        prev = self._s[-1] if self._s else None
        self._s.append(s)
        self._t.append(s.t)
        self.version += 1
        if prev is not None:
            if s.status != prev.status:
                self.events.append((s.t, f"status: {prev.status} -> {s.status}"))
            if s.lvl1 != prev.lvl1 and s.lvl1 is not None:
                self.events.append((s.t, f"fans -> {describe(s.lvl1)}"))
        self._prune(s.t)
        return True

    def add_snapshot(self, snap) -> bool:
        if not snap.time:
            return False
        fans = {f.fan: f for f in snap.fans}
        f1, f2 = fans.get(1), fans.get(2)
        temps = snap.temps or {}
        added = self.add(Sample(
            t=float(snap.time), cpu=temps.get("CPU"), gpu=temps.get("GPU"),
            hottest_c=snap.hottest[1] if snap.hottest else None,
            rpm1=f1.rpm if f1 else None, rpm2=f2.rpm if f2 else None,
            lvl1=f1.level if f1 else None, lvl2=f2.level if f2 else None,
            status=snap.status, ec_errors=snap.ec_errors))
        if added:
            self._note_sensors(temps)
        return added

    def _note_sensors(self, temps: dict) -> None:
        for name, value in temps.items():
            if value is None:
                continue
            lo_hi = self.sensor_stats.get(name)
            if lo_hi is None:
                self.sensor_stats[name] = [value, value]
            else:
                lo_hi[0] = min(lo_hi[0], value)
                lo_hi[1] = max(lo_hi[1], value)

    def sensor_range(self, name: str) -> Optional[tuple[int, int]]:
        lo_hi = self.sensor_stats.get(name)
        return (lo_hi[0], lo_hi[1]) if lo_hi else None

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_age_s
        k = bisect_left(self._t, cutoff)
        if k > 200:                          # prune in batches, not every cycle
            del self._s[:k]
            del self._t[:k]

    def prefill_csv(self, path: Path, now: Optional[float] = None) -> int:
        """Load the tail of cycles.csv. Returns the number of samples loaded."""
        now = self.clock() if now is None else now
        path = Path(path)
        if not path.exists():
            return 0
        loaded = 0
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        t = time.mktime(time.strptime(row["time"], "%Y-%m-%d %H:%M:%S"))
                    except (KeyError, ValueError):
                        continue
                    if t < now - self.max_age_s or t > now + 5:
                        continue
                    if self._t and t <= self._t[-1]:
                        continue
                    self._s.append(Sample(
                        t=t, cpu=_int(row.get("cpu_c", "")), gpu=_int(row.get("gpu_c", "")),
                        hottest_c=_int(row.get("hottest_c", "")),
                        rpm1=_int(row.get("fan1_rpm", "")), rpm2=_int(row.get("fan2_rpm", "")),
                        lvl1=parse_level(row.get("fan1_level", "")), lvl2=parse_level(row.get("fan2_level", "")),
                        status=row.get("status", "") or "ok", ec_errors=_int(row.get("ec_errors", "")) or 0))
                    self._t.append(t)
                    loaded += 1
        except Exception:
            log.debug("could not read %s for history", path, exc_info=True)
        if loaded:
            self.version += 1
        return loaded

    # ------------------------------------------------------------ reading
    def __len__(self) -> int:
        return len(self._s)

    def samples_between(self, t0: float, t1: float) -> list[Sample]:
        return self._s[bisect_left(self._t, t0):bisect_right(self._t, t1)]

    def window(self, window_s: float, now: Optional[float] = None) -> list[Sample]:
        now = self.clock() if now is None else now
        return self.samples_between(now - window_s, now)

    def latest(self) -> Optional[Sample]:
        return self._s[-1] if self._s else None

    # --------------------------------------------------------- statistics
    def time_at_level(self, window_s: float, now: Optional[float] = None) -> dict[int, float]:
        """Seconds spent at each fan-1 level in the window. Gaps are not counted."""
        now = self.clock() if now is None else now
        s = self.window(window_s, now)
        out: dict[int, float] = {}
        for a, b in zip(s, s[1:]):
            dt = b.t - a.t
            if a.lvl1 is not None and 0 < dt <= GAP_CAP_S:
                out[a.lvl1] = out.get(a.lvl1, 0.0) + dt
        if s and s[-1].lvl1 is not None:
            dt = min(max(now - s[-1].t, 0.0), GAP_CAP_S)
            out[s[-1].lvl1] = out.get(s[-1].lvl1, 0.0) + dt
        return out

    def covered_seconds(self, window_s: float, now: Optional[float] = None) -> float:
        return sum(self.time_at_level(window_s, now).values())

    def peak(self, window_s: float, now: Optional[float] = None) -> Optional[tuple[int, float]]:
        best: Optional[tuple[int, float]] = None
        for s in self.window(window_s, now):
            if s.hottest_c is not None and (best is None or s.hottest_c >= best[0]):
                best = (s.hottest_c, s.t)
        return best

    def level_changes(self, window_s: float, now: Optional[float] = None) -> int:
        n = 0
        s = self.window(window_s, now)
        for a, b in zip(s, s[1:]):
            if a.lvl1 is not None and b.lvl1 is not None and a.lvl1 != b.lvl1 and b.t - a.t <= GAP_CAP_S:
                n += 1
        return n

    def changes_per_hour(self, window_s: float, now: Optional[float] = None) -> Optional[float]:
        covered = self.covered_seconds(window_s, now)
        if covered < 60:
            return None
        return self.level_changes(window_s, now) * 3600.0 / covered

    def status_entries(self, statuses: Iterable[str], window_s: float, now: Optional[float] = None) -> int:
        """How many times the status switched INTO one of `statuses` in the window."""
        wanted = set(statuses)
        n = 0
        s = self.window(window_s, now)
        for a, b in zip(s, s[1:]):
            if b.status in wanted and a.status not in wanted:
                n += 1
        return n

    def last_event(self) -> Optional[tuple[float, str]]:
        return self.events[-1] if self.events else None


# ------------------------------------------------------------ line helpers
def split_segments(points: list[tuple[float, Optional[float]]], gap_s: float) -> list[list[tuple[float, float]]]:
    """Split (t, value) points into runs. A missing value, or a time gap bigger
    than gap_s, ends the current run so no line is drawn across it."""
    segs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    prev_t: Optional[float] = None
    for t, v in points:
        if v is None or (prev_t is not None and t - prev_t > gap_s):
            if cur:
                segs.append(cur)
            cur = []
        if v is not None:
            cur.append((t, float(v)))
        prev_t = t
    if cur:
        segs.append(cur)
    return segs


def clip_segment(seg: list[tuple[float, float]], t0: float, t1: float) -> list[tuple[float, float]]:
    """Keep the part of a run inside [t0, t1], inserting interpolated points at
    the edges so a line that crosses an edge ends exactly on it."""
    def lerp(a, b, t):
        return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])

    head = tail = None
    for a, b in zip(seg, seg[1:]):
        if a[0] < t0 < b[0]:
            head = (t0, lerp(a, b, t0))
        if a[0] < t1 < b[0]:
            tail = (t1, lerp(a, b, t1))
    inside = [p for p in seg if t0 <= p[0] <= t1]
    return ([head] if head else []) + inside + ([tail] if tail else [])
