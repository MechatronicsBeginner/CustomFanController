"""Tests for the "Copy report" text."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan.events import LogEntry                                     # noqa: E402
from customfan.report import (EXPECTED_MODULE_SHA256, build_report, redact,  # noqa: E402
                              sha256_file)
from customfan.service import Snapshot                                    # noqa: E402
from customfan.thinkpad import FanReading                                 # noqa: E402
import customfan.config as cfgmod                                         # noqa: E402

MACHINE = {"os": "Windows-11", "python": "3.12.1", "manufacturer": "LENOVO", "family": "ThinkPad P1 Gen 6",
           "product": "21FWSBAG00", "bios": "N3ZET57W (1.44)", "bios_date": "08/26/2026"}


def make(**over):
    snap = Snapshot(time=1.0, status="ok", message="Fan curve active", temps={"CPU": 50, "GPU": 38},
                    fans=(FanReading(1, 0, 0), FanReading(2, 0x80, 1200)), ec_errors=1)
    args = dict(version="1.4.0", frozen=True, machine=MACHINE, module_sha=EXPECTED_MODULE_SHA256,
                cfg=cfgmod.DEFAULTS, snap=snap,
                stats={"window": "30 min", "peak": (72, 1_700_000_000.0), "changes_per_hour": 42.4,
                       "handovers": 0, "pauses": 1},
                sensor_ranges={"CPU": (41, 72)},
                entries=[LogEntry(1.0, "INFO", "customfan", "hello"),
                         LogEntry(2.0, "WARNING", "customfan.service", "EC error 1/5: bad read")],
                home="C:\\Users\\Peter")
    args.update(over)
    return build_report(**args)


class TestReport(unittest.TestCase):
    def test_contains_the_facts_a_maintainer_needs(self):
        r = make()
        for needle in ("1.4.0", "built exe", "ThinkPad P1 Gen 6", "Machine type: 21FW", "N3ZET57W (1.44)",
                       "matches the tested module", "Fan 2: level auto, 1200 RPM", "CPU 50C", "Hand-overs to the EC: 0",
                       "Pauses (other EC tools): 1", "CPU 41-72C", "EC error 1/5: bad read", '"handover_c": 92'):
            self.assertIn(needle, r)

    def test_bios_string_is_tidied(self):
        r = make(machine=dict(MACHINE, bios="N3ZET57W (1.44 )"))
        self.assertIn("BIOS: N3ZET57W (1.44)", r)

    def test_serial_like_product_suffix_is_not_included(self):
        self.assertNotIn("SBAG00", make())

    def test_a_different_module_is_called_out(self):
        self.assertIn("DIFFERS", make(module_sha="0" * 64))
        self.assertIn("not found", make(module_sha=None))

    def test_user_name_is_removed_from_paths(self):
        e = [LogEntry(1.0, "ERROR", "customfan", r"Could not write C:\Users\peter\AppData\x and c:/users/Peter/y")]
        r = make(entries=e)
        self.assertNotIn("Peter", r.replace("Peter R", ""))
        self.assertNotIn("peter", r)
        self.assertIn("~\\AppData\\x", r)

    def test_redact_leaves_short_or_empty_home_alone(self):
        self.assertEqual(redact("a/b", home="/"), "a/b")
        self.assertEqual(redact("a/b", home=""), "a/b")

    def test_unknown_laptop_and_run_mode_are_stated_plainly(self):
        r = make(machine={"os": "Linux", "python": "3.12"}, run_mode="simulated")
        self.assertIn("Laptop: unknown", r)
        self.assertNotIn("?", r.split("== Now ==")[0].split("Laptop:")[1].split("\n")[0])
        self.assertIn("Run mode: simulated  (no real hardware involved)", r)

    def test_no_entries_is_handled(self):
        r = make(entries=[])
        self.assertIn("(none)", r)

    def test_expected_hash_is_the_one_the_loader_checks(self):
        from customfan.pawnio import KNOWN_MODULE_SHA256
        self.assertEqual(EXPECTED_MODULE_SHA256.lower(), KNOWN_MODULE_SHA256.lower())

    def test_sha256_of_file_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.bin"
            p.write_bytes(b"abc")
            self.assertEqual(sha256_file(p), "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD")
            self.assertIsNone(sha256_file(Path(d) / "none.bin"))


if __name__ == "__main__":
    unittest.main()
