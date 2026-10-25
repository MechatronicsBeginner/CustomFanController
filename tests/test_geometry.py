"""step_vertices: the corner points the curve chart draws."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan.control import step_vertices  # noqa: E402


class TestStepVertices(unittest.TestCase):
    def test_curve_goes_off_then_up_in_steps(self):
        pts = step_vertices([(52, 1), (58, 3)], 30, 100)
        self.assertEqual(pts, [(30, 0), (52, 0), (52, 1), (58, 1), (58, 3), (100, 3)])

    def test_shift_moves_every_step_left_by_the_deadband(self):
        pts = step_vertices([(52, 1), (58, 3)], 30, 100, shift=4)
        self.assertEqual(pts, [(30, 0), (48, 0), (48, 1), (54, 1), (54, 3), (100, 3)])

    def test_points_never_leave_the_drawing_range(self):
        pts = step_vertices([(31, 1)], 30, 100, shift=10)
        self.assertTrue(all(30 <= x <= 100 for x, _ in pts))

    def test_no_steps_is_a_flat_off_line(self):
        self.assertEqual(step_vertices([], 30, 100), [(30, 0), (100, 0)])


if __name__ == "__main__":
    unittest.main()
