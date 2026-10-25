"""Tests for the log-to-events logic (no Tk, no hardware)."""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan.events import (LogBuffer, LogEntry, filtered, last_problem, parse_lines,  # noqa: E402
                              read_tail)

SAMPLE = """\
2026-10-24 21:07:48,563 INFO    customfan: === Custom Fan Controller 1.4.0 starting ===
2026-10-24 21:07:48,566 INFO    customfan.service: Fans -> 0 (CPU curve)
2026-10-24 21:08:10,100 WARNING customfan.service: EC error 1/5: bad read
2026-10-24 21:08:11,100 ERROR   customfan.service: Unexpected error in control loop
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: boom
2026-10-24 21:08:12,000 INFO    customfan.service: Settings applied: cpu=[[52, 1]]
"""


class TestParse(unittest.TestCase):
    def test_parses_levels_sources_and_times(self):
        e = parse_lines(SAMPLE.splitlines())
        self.assertEqual(len(e), 5)
        self.assertEqual((e[1].level, e[1].source, e[1].text), ("INFO", "customfan.service", "Fans -> 0 (CPU curve)"))

    def test_time_differences_are_correct(self):
        e = parse_lines(SAMPLE.splitlines())
        self.assertAlmostEqual(e[2].t - e[1].t, 21.534, places=2)

    def test_traceback_lines_join_the_error_entry(self):
        e = parse_lines(SAMPLE.splitlines())
        err = e[3]
        self.assertTrue(err.is_problem)
        self.assertIn("ValueError: boom", err.text)
        self.assertIn("+3 lines", err.headline)
        self.assertNotIn("\n", err.headline)

    def test_garbage_is_ignored(self):
        self.assertEqual(parse_lines(["", "not a log line", "   "]), [])


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.entries = parse_lines(SAMPLE.splitlines())

    def test_problems_are_warnings_and_errors(self):
        self.assertEqual([e.level for e in filtered(self.entries, "problems")], ["WARNING", "ERROR"])

    def test_changes_are_fan_and_settings_changes(self):
        texts = [e.text.split(":")[0].split(" (")[0] for e in filtered(self.entries, "changes")]
        self.assertEqual(texts, ["Fans -> 0", "Settings applied"])

    def test_all_keeps_everything_and_last_problem_finds_newest(self):
        self.assertEqual(len(filtered(self.entries, "all")), 5)
        self.assertIn("Unexpected", last_problem(self.entries).text)
        self.assertIsNone(last_problem([]))


class TestFileAndBuffer(unittest.TestCase):
    def test_read_tail_returns_newest_entries_and_survives_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.log"
            p.write_text(SAMPLE, encoding="utf-8")
            self.assertEqual(len(read_tail(p)), 5)
            self.assertEqual(len(read_tail(p, max_entries=2)), 2)
            self.assertEqual(read_tail(p, max_entries=2)[-1].text.split(":")[0], "Settings applied")
            self.assertEqual(read_tail(Path(d) / "nope.log"), [])

    def test_read_tail_of_a_big_file_drops_the_cut_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.log"
            p.write_text(SAMPLE * 200, encoding="utf-8")
            got = read_tail(p, max_entries=10_000, max_bytes=2000)
            self.assertTrue(0 < len(got) < 1000)
            self.assertTrue(all(e.level in ("INFO", "WARNING", "ERROR") for e in got))

    def test_buffer_captures_only_this_programs_records(self):
        buf = LogBuffer(maxlen=3)
        lg = logging.getLogger("customfan.testbuf")
        other = logging.getLogger("somelib")
        for logger in (lg, other):
            logger.setLevel(logging.INFO)
            logger.addHandler(buf)
        try:
            lg.info("one")
            other.info("not ours")
            try:
                raise ValueError("bad")
            except ValueError:
                lg.exception("two")
            lg.info("three")
            lg.info("four")
        finally:
            for logger in (lg, other):
                logger.removeHandler(buf)
        texts = [e.text.split("\n")[0] for e in buf.snapshot()]
        self.assertEqual(texts, ["two", "three", "four"])          # bounded, "one" fell off, "not ours" never in
        self.assertIn("ValueError: bad", buf.snapshot()[0].text)
        self.assertGreaterEqual(buf.version, 4)

    def test_prefill_puts_old_entries_first(self):
        buf = LogBuffer()
        buf.prefill([LogEntry(1.0, "INFO", "customfan", "old")])
        self.assertEqual(buf.snapshot()[0].text, "old")


if __name__ == "__main__":
    unittest.main()
