"""Run with:  python -m unittest discover -s tests -v   (from the CustomFanController folder)

The lease and watchdog tests. Everything runs against the simulated EC - no hardware is touched.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan import config as cfgmod                                        # noqa: E402
from customfan.ec import EmbeddedController                                    # noqa: E402
from customfan.lease import CORRUPT, MISSING, LeaseWriter, pid_alive, read_lease  # noqa: E402
from customfan.service import ControlLoop                                      # noqa: E402
from customfan.simulator import SimulatedEC                                    # noqa: E402
from customfan.thinkpad import LEVEL_AUTO, ThinkPadFans                        # noqa: E402
from customfan.watchdog import EXIT, HANDED_BACK, IDLE, RETRY, Watchdog, WatchdogKeeper  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def dead_pid():
    """The id of a process that has definitely finished."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def write_lease(path, **kw):
    d = {"v": 1, "pid": 4242, "seq": 1, "manual": True, "cycle_s": 3.0, "t": time.time()}
    d.update(kw)
    Path(path).write_text(json.dumps(d), encoding="utf-8")


class TestLeaseFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.lease"

    def test_round_trip_and_clean_exit(self):
        w = LeaseWriter(self.path, cycle_s=3)
        self.assertEqual(read_lease(self.path), MISSING)
        self.assertTrue(w.beat(True))
        got = read_lease(self.path)
        self.assertEqual((got["pid"], got["seq"], got["manual"]), (w.pid, 1, True))
        w.beat(False)
        got = read_lease(self.path)
        self.assertEqual((got["seq"], got["manual"]), (2, False))
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")), "temp file left behind")
        w.clear()
        self.assertEqual(read_lease(self.path), MISSING)
        w.clear()                                       # clearing twice is harmless

    def test_garbage_is_corrupt_not_a_crash(self):
        for text in ("", "{", "[]", '{"v": 2, "pid": 1, "seq": 1, "manual": true}',
                     '{"v": 1, "pid": 1, "seq": 1, "manual": "yes"}',
                     '{"v": 1, "pid": "x", "seq": 1, "manual": true}'):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(read_lease(self.path), CORRUPT, text)

    def test_pid_alive(self):
        import os
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(dead_pid()))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-5))

    def test_unwritable_location_is_reported_not_raised(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        w = LeaseWriter(blocker / "sub" / "x.lease")     # a "directory" that is really a file
        with self.assertLogs("customfan.lease", "WARNING"):
            for _ in range(3):
                self.assertFalse(w.beat(True))
        self.assertEqual(w.failures, 3)


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.lease"
        self.clock = FakeClock()
        self.alive = True
        self.calls = 0
        self.hb_ok = True

    def hand_back(self):
        self.calls += 1
        return self.hb_ok

    def make(self):
        return Watchdog(self.path, self.hand_back, pid_alive=lambda p: self.alive, clock=self.clock,
                        poll_s=1.0, stale_s=12.0, startup_wait_s=30.0)

    def tick(self, wd, n=1, secs=1.0):
        out = None
        for _ in range(n):
            self.clock.advance(secs)
            out = wd.step()
        return out

    def until(self, wd, want, cap=60):
        """Seconds (1 s ticks) until step() returns `want`; fails if it never does."""
        for i in range(1, cap + 1):
            if self.tick(wd) == want:
                return i
        self.fail(f"never returned {want!r} in {cap} s")

    def test_killed_controller_in_manual_mode_is_handed_back_once(self):
        write_lease(self.path, manual=True)
        self.alive = False
        wd = self.make()
        self.assertEqual(self.tick(wd), HANDED_BACK)
        self.assertEqual(self.tick(wd, 20), IDLE)
        self.assertEqual(self.calls, 1)

    def test_controller_gone_but_fans_already_auto_is_left_alone(self):
        write_lease(self.path, manual=False)
        self.alive = False
        wd = self.make()
        self.assertEqual(self.tick(wd, 40), IDLE)
        self.assertEqual(self.calls, 0)

    def test_hung_controller_is_handed_back_only_after_the_stale_limit(self):
        write_lease(self.path, manual=True)              # alive, but the lease never changes again
        wd = self.make()
        secs = self.until(wd, HANDED_BACK)
        self.assertGreater(secs, 12)
        self.assertLessEqual(secs, 15)
        self.assertEqual(self.calls, 1)

    def test_healthy_controller_is_never_touched(self):
        wd = self.make()
        for seq in range(1, 120):
            write_lease(self.path, seq=seq, manual=True)
            self.assertEqual(self.tick(wd, secs=3.0), IDLE)
        self.assertEqual(self.calls, 0)

    def test_failed_handback_is_retried_with_backoff_then_succeeds(self):
        write_lease(self.path, manual=True)
        self.alive = False
        self.hb_ok = False
        wd = self.make()
        self.assertEqual(self.tick(wd), RETRY)           # attempt 1
        self.assertEqual(self.tick(wd), RETRY)           # attempt 2 (1 s later)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.tick(wd), RETRY)           # backing off 2 s: no call
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.tick(wd), RETRY)           # attempt 3
        self.assertEqual(self.calls, 3)
        self.hb_ok = True
        self.assertEqual(self.until(wd, HANDED_BACK), 4)  # waits out the 4 s back-off
        self.assertEqual(self.calls, 4)
        self.assertEqual(self.tick(wd, 10), IDLE)

    def test_hand_back_that_raises_is_a_failure_not_a_crash(self):
        write_lease(self.path, manual=True)
        self.alive = False
        wd = Watchdog(self.path, lambda: 1 / 0, pid_alive=lambda p: False, clock=self.clock)
        with self.assertLogs("customfan.watchdog", "ERROR"):
            self.assertEqual(self.tick(wd), RETRY)

    def test_sleep_gap_gives_the_controller_a_grace_period(self):
        write_lease(self.path, manual=True)
        wd = self.make()
        self.assertEqual(self.tick(wd), IDLE)
        self.assertEqual(self.tick(wd, secs=8 * 3600), IDLE)   # the whole machine was asleep
        self.assertEqual(self.calls, 0)
        self.assertGreater(self.until(wd, HANDED_BACK), 12)    # the grace period is finite

    def test_clean_exit_stops_the_watchdog(self):
        write_lease(self.path, manual=False)
        wd = self.make()
        self.assertEqual(self.tick(wd), IDLE)
        self.path.unlink()
        self.assertEqual(self.tick(wd), IDLE)
        self.assertEqual(self.tick(wd), EXIT)
        self.assertEqual(self.calls, 0)

    def test_gives_up_if_no_controller_ever_appears(self):
        wd = self.make()
        secs = self.until(wd, EXIT)
        self.assertGreater(secs, 30)
        self.assertLessEqual(secs, 32)

    def test_unreadable_lease_is_treated_as_manual_after_the_stale_limit(self):
        self.path.write_text("{ half a lea", encoding="utf-8")
        wd = self.make()
        self.assertEqual(self.tick(wd, 10), IDLE)
        self.assertGreater(self.until(wd, HANDED_BACK), 2)
        self.assertEqual(self.calls, 1)

    def test_new_controller_after_a_handback_is_protected_again(self):
        write_lease(self.path, pid=111, manual=True)
        self.alive = False
        wd = self.make()
        self.assertEqual(self.tick(wd), HANDED_BACK)
        write_lease(self.path, pid=222, seq=1, manual=True)    # the restarted controller
        self.alive = True
        self.assertEqual(self.tick(wd, 5), IDLE)
        self.alive = False                                      # and it is killed too
        self.assertEqual(self.tick(wd), HANDED_BACK)
        self.assertEqual(self.calls, 2)

    def test_controller_that_wakes_up_after_a_handback_is_protected_again(self):
        write_lease(self.path, manual=True)
        wd = self.make()
        self.until(wd, HANDED_BACK)
        write_lease(self.path, seq=2, manual=True)             # it was only stuck
        self.assertEqual(self.tick(wd), IDLE)
        self.until(wd, HANDED_BACK)                             # stuck again
        self.assertEqual(self.calls, 2)

    def test_slow_cycle_setting_widens_the_stale_limit(self):
        write_lease(self.path, manual=True, cycle_s=10.0)       # 4 cycles = 40 s
        wd = self.make()
        self.assertEqual(self.tick(wd, 38), IDLE)
        self.assertGreater(self.until(wd, HANDED_BACK), 2)


class TestControllerPublishesLease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.lease"
        self.loops = []

    def tearDown(self):
        for loop in self.loops:
            loop.stop()

    def start(self, sim, conflict_check=None):
        cfg, _ = cfgmod.validate({})
        loop = ControlLoop(lambda: sim, cfg, cycle_override_s=0.02, ec_wait_timeout_s=0.02,
                           conflict_check=conflict_check, conflict_interval_s=0.0,
                           lease=LeaseWriter(self.path, cycle_s=0.02))
        self.loops.append(loop)
        loop.start()
        return loop

    def manual_flag(self):
        got = read_lease(self.path)
        return got["manual"] if isinstance(got, dict) else None

    def wait_for(self, cond, what, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def test_manual_is_announced_before_the_first_manual_write(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        seen = []
        orig = sim._write_reg

        def spy(reg, value):
            if reg == 0x2F and value != LEVEL_AUTO:           # a manual level is about to reach the EC
                seen.append(read_lease(self.path))
            return orig(reg, value)

        sim._write_reg = spy
        loop = self.start(sim)
        self.wait_for(lambda: len(seen) >= 2, "manual writes")
        loop.stop()
        for lease in seen:
            self.assertIsInstance(lease, dict, "no lease on disk when a manual level was written")
            self.assertTrue(lease["manual"], "lease said auto when a manual level was written")

    def test_clean_exit_hands_back_and_removes_the_lease(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop = self.start(sim)
        self.wait_for(lambda: self.manual_flag() is True, "manual lease")
        loop.stop()
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})
        self.assertEqual(read_lease(self.path), MISSING)

    def test_unverified_exit_leaves_manual_set_for_the_watchdog(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop = self.start(sim)
        self.wait_for(lambda: self.manual_flag() is True, "manual lease")
        loop.fans.restore_auto = lambda: False                  # the hand-back does not verify
        loop.stop()
        got = read_lease(self.path)
        self.assertIsInstance(got, dict, "lease was removed although the hand-back failed")
        self.assertTrue(got["manual"])

    def test_hwinfo_pause_publishes_auto_then_resumes_manual(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        conflicts = []
        loop = self.start(sim, conflict_check=lambda: list(conflicts))
        self.wait_for(lambda: self.manual_flag() is True, "manual lease")
        conflicts.append("HWiNFO64.exe")
        self.wait_for(lambda: self.manual_flag() is False, "auto lease during the pause")
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})
        conflicts.clear()
        self.wait_for(lambda: self.manual_flag() is True, "manual lease after the pause")

    def test_hot_handover_publishes_auto_then_resumes_manual(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop = self.start(sim)
        self.wait_for(lambda: self.manual_flag() is True, "manual lease")
        sim.set_cpu(98)
        self.wait_for(lambda: self.manual_flag() is False, "auto lease while hot")
        sim.set_cpu(60)
        self.wait_for(lambda: self.manual_flag() is True, "manual lease after cooling")

    def test_lease_keeps_advancing_while_running(self):
        sim = SimulatedEC(thermal=False)
        loop = self.start(sim)
        self.wait_for(lambda: isinstance(read_lease(self.path), dict), "first lease")
        first = read_lease(self.path)["seq"]
        self.wait_for(lambda: read_lease(self.path)["seq"] > first + 3, "seq to advance")

    def test_killed_controller_is_rescued_end_to_end(self):
        """The failure from the real laptop: process killed with the fans at level 0."""
        sim = SimulatedEC(thermal=False)
        ThinkPadFans(EmbeddedController(sim, wait_timeout_s=0.05)).set_all(0)
        self.assertEqual(sim.fan_level, {0: 0, 1: 0})            # fans off, nothing left to change that
        write_lease(self.path, pid=dead_pid(), manual=True)
        wd = Watchdog(self.path, lambda: ThinkPadFans(
            EmbeddedController(sim, wait_timeout_s=0.05, attempts=3)).restore_auto())
        self.assertEqual(wd.step(), HANDED_BACK)
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})
        self.assertEqual(sim.regs[0x31], 0x00, "fan selector left on fan 2")


class TestWatchdogKeeper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        (self.app / "logs").mkdir()
        self.calls = []

    def spawner(self, app_dir):
        self.calls.append(app_dir)
        return True, "test"

    def test_starts_a_watchdog_when_none_is_running_then_leaves_it_alone(self):
        keeper = WatchdogKeeper(self.app, spawner=self.spawner, retry_s=0.0)
        self.assertTrue(keeper.check())
        self.assertEqual(len(self.calls), 1)
        LeaseWriter(self.app / "logs" / "watchdog.lease", cycle_s=1.0).beat(False)   # it publishes
        self.assertTrue(keeper.check())
        self.assertEqual(len(self.calls), 1, "started a second watchdog")

    def test_a_stale_watchdog_lease_means_it_is_gone(self):
        write_lease(self.app / "logs" / "watchdog.lease", pid=__import__("os").getpid(),
                    manual=False, t=time.time() - 120)
        keeper = WatchdogKeeper(self.app, spawner=self.spawner, retry_s=0.0)
        keeper.check()
        self.assertEqual(len(self.calls), 1)

    def test_says_so_when_it_cannot_start_one(self):
        keeper = WatchdogKeeper(self.app, spawner=lambda d: (False, "access denied"), retry_s=0.0)
        with self.assertLogs("customfan.watchdog", "WARNING") as cm:
            self.assertFalse(keeper.check())
        self.assertTrue(any("Could not start" in m for m in cm.output))
        self.assertTrue(any("NOT running" in m for m in cm.output))

    def test_does_not_respawn_in_a_tight_loop(self):
        keeper = WatchdogKeeper(self.app, spawner=self.spawner, retry_s=60.0)
        for _ in range(5):
            keeper.check()
        self.assertEqual(len(self.calls), 1)


class TestSpawn(unittest.TestCase):
    """spawn() on the Windows path, with Popen faked: nothing is started."""
    BREAKAWAY = 0x01000000

    def run_spawn(self, refuse_breakaway: bool, refuse_all: bool = False):
        from unittest import mock
        from customfan import watchdog
        flags_seen = []

        def fake_popen(cmd, creationflags=0, **kw):
            flags_seen.append(creationflags)
            if refuse_all or (refuse_breakaway and creationflags & self.BREAKAWAY):
                raise PermissionError(13, "[WinError 5] Access is denied")
            return mock.Mock()

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(watchdog.sys, "platform", "win32"), \
                mock.patch.object(watchdog.subprocess, "Popen", fake_popen):
            return watchdog.spawn(Path(d)), flags_seen

    def test_breakaway_allowed_reports_outside_the_job(self):
        (ok, detail), flags = self.run_spawn(refuse_breakaway=False)
        self.assertTrue(ok)
        self.assertEqual(detail, "outside the task's job")
        self.assertEqual(len(flags), 1)

    def test_refused_breakaway_logs_the_real_error(self):
        # Seen on hardware: a Task Scheduler job refuses breakaway with WinError 5.
        (ok, detail), flags = self.run_spawn(refuse_breakaway=True)
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("inside the task's job"), detail)
        self.assertIn("Access is denied", detail)
        self.assertTrue(flags[0] & self.BREAKAWAY and not flags[1] & self.BREAKAWAY)

    def test_both_attempts_failing_returns_the_error(self):
        (ok, detail), flags = self.run_spawn(refuse_breakaway=True, refuse_all=True)
        self.assertFalse(ok)
        self.assertIn("Access is denied", detail)
        self.assertEqual(len(flags), 2)


if __name__ == "__main__":
    unittest.main()
