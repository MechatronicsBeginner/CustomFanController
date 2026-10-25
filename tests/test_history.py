"""Tests for the dashboard's history and statistics (no Tk, no hardware)."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan.history import (GAP_CAP_S, History, Sample, clip_segment, parse_level,  # noqa: E402
                               split_segments)
from customfan.service import CycleLog, Snapshot                                       # noqa: E402
from customfan.thinkpad import FanReading                                              # noqa: E402


def sample(t, lvl=0, hot=50, status="ok", errors=0):
    return Sample(t=t, cpu=hot, gpu=None, hottest_c=hot, rpm1=lvl * 700, rpm2=lvl * 600,
                  lvl1=lvl, lvl2=lvl, status=status, ec_errors=errors)


def snap(t, cpu=60, level=3, status="ok"):
    return Snapshot(time=t, status=status, temps={"CPU": cpu}, hottest=("CPU", cpu),
                    fans=(FanReading(1, level, 2600), FanReading(2, level, 2300)), ec_errors=0)


class TestHistory(unittest.TestCase):
    def test_duplicates_and_old_samples_are_ignored(self):
        h = History(clock=lambda: 1000.0)
        self.assertTrue(h.add(sample(100)))
        self.assertFalse(h.add(sample(100)))        # same cycle seen twice
        self.assertFalse(h.add(sample(90)))         # clock went backwards
        self.assertEqual(len(h), 1)

    def test_add_snapshot_builds_a_sample(self):
        h = History(clock=lambda: 1000.0)
        self.assertTrue(h.add_snapshot(snap(500.0, cpu=62, level=3)))
        s = h.latest()
        self.assertEqual((s.cpu, s.hottest_c, s.rpm1, s.rpm2, s.lvl1), (62, 62, 2600, 2300, 3))
        self.assertFalse(h.add_snapshot(Snapshot()))   # time == 0: nothing measured yet

    def test_time_at_level_counts_each_interval_once(self):
        h = History(clock=lambda: 130.0)
        for t, lvl in ((100, 0), (103, 0), (106, 3), (109, 3), (112, 3), (115, 0)):
            h.add(sample(t, lvl))
        out = h.time_at_level(60, now=118.0)
        self.assertAlmostEqual(out[0], 6 + 3)        # 100-106, plus 115-118 up to "now"
        self.assertAlmostEqual(out[3], 9)            # 106-115

    def test_gaps_are_not_counted_as_time_at_a_level(self):
        h = History(clock=lambda: 1000.0)
        h.add(sample(100, 5))
        h.add(sample(103, 5))
        h.add(sample(103 + GAP_CAP_S + 60, 5))       # the laptop slept for a minute+
        out = h.time_at_level(3600, now=103 + GAP_CAP_S + 61)
        self.assertAlmostEqual(out[5], 3 + 1)        # 100-103, and 1 s after the last sample

    def test_peak_returns_hottest_and_when(self):
        h = History(clock=lambda: 1000.0)
        for t, c in ((100, 60), (103, 91), (106, 70)):
            h.add(sample(t, hot=c))
        self.assertEqual(h.peak(60, now=110.0), (91, 103))
        self.assertIsNone(History().peak(60))

    def test_level_changes_per_hour(self):
        h = History(clock=lambda: 1000.0)
        t = 0.0
        for lvl in [0, 1, 1, 3, 3, 1, 0] * 10:       # 4 changes per block, 3 s apart
            h.add(sample(t, lvl))
            t += 3.0
        self.assertEqual(h.level_changes(600, now=t), 40)        # 4 changes in each of 10 blocks
        cph = h.changes_per_hour(600, now=t)
        self.assertGreater(cph, 100)
        self.assertIsNone(History().changes_per_hour(600))      # not enough data for a rate

    def test_status_entries_counts_transitions_not_samples(self):
        h = History(clock=lambda: 1000.0)
        for t, st in ((100, "ok"), (103, "handover"), (106, "handover"), (109, "ok"), (112, "handover")):
            h.add(sample(t, status=st))
        self.assertEqual(h.status_entries(["handover"], 600, now=113.0), 2)

    def test_events_record_status_and_level_changes(self):
        h = History(clock=lambda: 1000.0)
        h.add(sample(100, 0))
        h.add(sample(103, 3))
        h.add(sample(106, 3, status="handover"))
        texts = [e[1] for e in h.events]
        self.assertIn("fans -> level 3", texts)
        self.assertIn("status: ok -> handover", texts)
        self.assertEqual(h.last_event()[0], 106)

    def test_sensor_ranges_track_lowest_and_highest_since_start(self):
        h = History(clock=lambda: 1000.0)
        for t, cpu, pcm in ((100, 50, 45), (103, 71, 44), (106, 60, None)):
            h.add_snapshot(Snapshot(time=t, status="ok", temps={"CPU": cpu, "PCM": pcm},
                                    hottest=("CPU", cpu), fans=(), ec_errors=0))
        self.assertEqual(h.sensor_range("CPU"), (50, 71))
        self.assertEqual(h.sensor_range("PCM"), (44, 45))       # a missing reading is not a value
        self.assertIsNone(h.sensor_range("GPU"))

    def test_duplicate_snapshot_does_not_change_ranges(self):
        h = History(clock=lambda: 1000.0)
        h.add_snapshot(snap(100.0, cpu=60))
        h.add_snapshot(Snapshot(time=100.0, status="ok", temps={"CPU": 99}, hottest=("CPU", 99), fans=()))
        self.assertEqual(h.sensor_range("CPU"), (60, 60))

    def test_old_samples_are_pruned_in_batches(self):
        h = History(max_age_s=100, clock=lambda: 0.0)
        for i in range(1000):
            h.add(sample(float(i)))
        self.assertLess(len(h), 400)
        self.assertEqual(h.latest().t, 999.0)


class TestPrefill(unittest.TestCase):
    def test_round_trip_through_cycles_csv(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cycles.csv"
            log = CycleLog(path)
            for i in range(5):
                log.write(snap(now - 100 + i * 3, cpu=60 + i, level=i))
            h = History(clock=lambda: now)
            self.assertEqual(h.prefill_csv(path, now=now), 5)
            s = h.latest()
            self.assertEqual((s.cpu, s.lvl1, s.rpm1, s.status), (64, 4, 2600, "ok"))
            # prefilled history is ordered and lets live samples continue after it
            self.assertTrue(h.add(sample(now, 2)))

    def test_missing_or_garbage_file_is_harmless(self):
        h = History()
        self.assertEqual(h.prefill_csv(Path("/nonexistent/cycles.csv")), 0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cycles.csv"
            p.write_text("time,status\nnot a date,ok\n", encoding="utf-8")
            self.assertEqual(h.prefill_csv(p), 0)

    def test_parse_level(self):
        self.assertEqual(parse_level("auto"), 0x80)
        self.assertEqual(parse_level("5"), 5)
        self.assertEqual(parse_level("0x40"), 0x40)
        self.assertIsNone(parse_level(""))
        self.assertIsNone(parse_level("?"))


class TestLineHelpers(unittest.TestCase):
    def test_split_at_gaps_and_missing_values(self):
        pts = [(0, 1), (3, 2), (6, None), (9, 4), (12, 5), (60, 6)]
        segs = split_segments(pts, gap_s=10)
        self.assertEqual(segs, [[(0, 1.0), (3, 2.0)], [(9, 4.0), (12, 5.0)], [(60, 6.0)]])

    def test_clip_inserts_edge_points(self):
        seg = [(0.0, 0.0), (10.0, 10.0), (20.0, 0.0)]
        out = clip_segment(seg, 5.0, 15.0)
        self.assertEqual(out, [(5.0, 5.0), (10.0, 10.0), (15.0, 5.0)])

    def test_clip_leaves_inside_segments_alone(self):
        seg = [(1.0, 1.0), (2.0, 2.0)]
        self.assertEqual(clip_segment(seg, 0.0, 10.0), seg)
        self.assertEqual(clip_segment(seg, 5.0, 10.0), [])


if __name__ == "__main__":
    unittest.main()
