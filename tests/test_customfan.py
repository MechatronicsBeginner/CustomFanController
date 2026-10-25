"""Run with:  python -m unittest discover -s tests -v   (from the CustomFanController folder)

Everything here runs against the simulated EC - no hardware is touched.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customfan import config as cfgmod                                   # noqa: E402
from customfan.control import AUTO, CURVE, Policy, SensorFilter, StepCurve  # noqa: E402
from customfan.ec import ECError, EmbeddedController                      # noqa: E402
from customfan.service import (ControlLoop, ST_AUTO, ST_FAULT, ST_HANDOVER,  # noqa: E402
                           ST_OK, ST_PAUSED, ST_READONLY)
from customfan.simulator import SimulatedEC                               # noqa: E402
from customfan.thinkpad import LEVEL_AUTO, ThinkPadFans                   # noqa: E402

STEPS = [(52, 1), (58, 3), (64, 5), (70, 7)]


def make(sim=None, wait_timeout_s=0.05, **kw):
    sim = sim or SimulatedEC(thermal=False)
    return sim, ThinkPadFans(EmbeddedController(sim, wait_timeout_s=wait_timeout_s, **kw))


class TestECProtocol(unittest.TestCase):
    def test_read_write_roundtrip(self):
        sim, fans = make()
        fans.ec.write(0x10, 0x5A)
        self.assertEqual(fans.ec.read(0x10), 0x5A)

    def test_sensors_and_rpm_decode(self):
        sim, fans = make()
        sim.set_cpu(54)
        raw = fans.read_sensors()
        self.assertEqual(raw["CPU"], 54)
        self.assertEqual(raw["X7F"], 0x80)
        fans.set_all(3)
        r = {f.fan: f for f in fans.read_fans()}
        self.assertEqual(r[1].rpm, 2600)                 # low/high byte assembled correctly
        self.assertEqual(r[2].rpm, int(2600 * 0.88))

    def test_set_level_targets_each_fan_and_restores_selector(self):
        sim, fans = make()
        self.assertEqual(fans.set_level(2, 4), 4)
        self.assertEqual(sim.fan_level[1], 4)            # fan 2 (selector 0x01)
        self.assertEqual(sim.fan_level[0], LEVEL_AUTO)   # fan 1 untouched
        self.assertEqual(sim.regs[0x31], 0x00)           # selector left on fan 1

    def test_refuses_unsafe_levels(self):
        _, fans = make()
        for bad in (0x40, 70, 80, 8, 0x81):
            with self.assertRaises(ValueError):
                fans.set_level(1, bad)

    def test_foreign_byte_is_not_returned_as_our_data(self):
        """The v1.2 bug: a byte left in the output buffer by another master
        (Windows' ACPI driver) right after our drain came back as our answer."""
        sim, fans = make()
        sim.set_cpu(54)
        sim._status_reads = 0
        sim.foreign_at = 2            # appears just after our drain check
        self.assertEqual(fans.ec.read(0x78), 54)    # waited for it to be collected

    def test_busy_ec_is_retried(self):
        sim, fans = make(wait_timeout_s=1.0)     # real timeout; 30 busy polls ~ 0.3 s
        sim.busy_polls = 30                               # IBF stuck briefly
        self.assertEqual(fans.ec.read(0x78), int(round(sim.temps["CPU"])))

    def test_dead_ec_raises_instead_of_hanging(self):
        sim, fans = make()
        sim.dead = True
        t0 = time.perf_counter()
        with self.assertRaises(ECError):
            fans.ec.read(0x78)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_lock_held_across_transaction(self):
        sim, fans = make()
        with fans.ec.transaction():
            self.assertTrue(sim._lock._is_owned())
        # released afterwards: another thread can take it
        import threading
        got = []
        t = threading.Thread(target=lambda: got.append(sim.acquire_lock(100)))
        t.start(); t.join()
        self.assertTrue(got[0])


class TestCorruptionGuards(unittest.TestCase):
    def test_interleaved_garbage_is_rejected_not_believed(self):
        sim, fans = make()
        sim.corrupt_byte = 0x90                  # what we saw with HWiNFO open: 0x90 / 37008 RPM
        with self.assertRaises(ECError):
            fans.read_fans()

    def test_temperature_spike_needs_confirmation(self):
        f = SensorFilter()
        self.assertEqual(f.update({"CPU": 55})["CPU"], 55)
        self.assertEqual(f.update({"CPU": 120 - 1})["CPU"], 55)   # one wild read: ignored
        self.assertEqual(f.update({"CPU": 56})["CPU"], 56)       # back to normal
        self.assertEqual(f.update({"CPU": 85})["CPU"], 56)       # real jump: held one cycle...
        self.assertEqual(f.update({"CPU": 87})["CPU"], 87)       # ...accepted once confirmed


class TestCurve(unittest.TestCase):
    def test_hysteresis_matches_old_ini_semantics(self):
        c = StepCurve(STEPS, deadband=4)
        seq = [(47, 0), (52, 1), (50, 1), (48, 1), (47, 0),   # on at 52, off below 48
               (58, 3), (55, 3), (53, 1),                      # 3 holds until < 54
               (75, 7), (67, 7), (65, 5), (61, 5), (59, 3)]    # jump straight to 7
        for temp, want in seq:
            self.assertEqual(c.update(temp), want, f"temp {temp}")

    def test_step_down_delay_rides_out_short_dips(self):
        c = StepCurve(STEPS, deadband=4, down_delay_s=15)
        self.assertEqual(c.update(72, now=0), 7)
        self.assertEqual(c.update(50, now=3), 7)      # sudden dip (or a bad read): hold
        self.assertEqual(c.update(71, now=6), 7)      # back up: timer resets
        self.assertEqual(c.update(60, now=9), 7)      # dip starts again
        self.assertEqual(c.update(60, now=20), 7)     # 11 s below: still holding
        self.assertEqual(c.update(60, now=24), 5)     # 15 s continuously below: step down (5 holds until < 60)
        self.assertEqual(c.update(75, now=25), 7)     # heating is always immediate

    def test_preview_ignores_hysteresis(self):
        self.assertEqual(StepCurve.preview(STEPS, 51), 0)
        self.assertEqual(StepCurve.preview(STEPS, 64), 5)


class TestFilterAndPolicy(unittest.TestCase):
    def test_filter_holds_then_drops_bad_values(self):
        f = SensorFilter(hold_cycles=3)
        self.assertEqual(f.update({"APS": 50})["APS"], 50)
        for _ in range(3):
            self.assertEqual(f.update({"APS": 0x80})["APS"], 50)
        self.assertIsNone(f.update({"APS": 0x80})["APS"])
        self.assertIsNone(f.update({"X7F": 0x80})["X7F"])     # never valid
        self.assertIsNone(f.update({"BUS": 0})["BUS"])

    def policy(self, **kw):
        cfg, _ = cfgmod.validate({})
        cfg.update(kw)
        return Policy(cfgmod.to_policy(cfg))

    def test_higher_curve_wins(self):
        p = self.policy()
        d = p.decide({"CPU": 53, "GPU": 67}, "curve")
        self.assertEqual((d.mode, d.cpu_level, d.gpu_level, d.level), (CURVE, 1, 3, 3))

    def test_handover_and_resume(self):
        p = self.policy()
        self.assertEqual(p.decide({"CPU": 92, "GPU": 40}, "curve").mode, AUTO)
        self.assertEqual(p.decide({"CPU": 88, "GPU": 40}, "curve").mode, AUTO)   # still above resume
        d = p.decide({"CPU": 84, "GPU": 40}, "curve")
        self.assertEqual((d.mode, d.level), (CURVE, 7))

    def test_ignored_sensor_does_not_trigger_handover(self):
        p = self.policy()
        d = p.decide({"CPU": 60, "GPU": 40, "BAT1": 99}, "curve")
        self.assertEqual(d.mode, CURVE)

    def test_missing_cpu_hands_to_ec(self):
        p = self.policy()
        self.assertEqual(p.decide({"CPU": None, "GPU": 40}, "curve").mode, AUTO)

    def test_user_auto(self):
        self.assertEqual(self.policy().decide({"CPU": 60}, "auto").mode, AUTO)


class TestConfig(unittest.TestCase):
    def test_bad_values_are_repaired(self):
        cfg, problems = cfgmod.validate({
            "cpu_curve": [[60, 1], [55, 3]],          # not increasing
            "gpu_curve": [[60, 1], [70, 64]],         # disengaged level
            "handover_c": 150, "resume_c": 99, "mode": "turbo",
            "ignore_sensors": ["cpu", "bat1"]})
        self.assertEqual(cfg["cpu_curve"], cfgmod.DEFAULTS["cpu_curve"])
        self.assertEqual(cfg["gpu_curve"], cfgmod.DEFAULTS["gpu_curve"])
        self.assertEqual(cfg["handover_c"], 99)
        self.assertLessEqual(cfg["resume_c"], 96)
        self.assertEqual(cfg["mode"], "curve")
        self.assertEqual(cfg["ignore_sensors"], ["BAT1"])     # CPU can't be ignored
        self.assertGreaterEqual(len(problems), 5)

    def test_corrupt_file_is_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text("{not json")
            cfg = cfgmod.load(p)
            self.assertEqual(cfg, cfgmod.DEFAULTS)
            self.assertTrue((Path(d) / "config.bad.json").exists())
            self.assertEqual(json.loads(p.read_text()), cfgmod.DEFAULTS)


class TestControlLoop(unittest.TestCase):
    def run_loop(self, sim, cycles=4, readonly=False, conflict_check=None, **cfg_over):
        cfg, _ = cfgmod.validate({})
        cfg.update(cfg_over)
        snaps = []
        loop = ControlLoop(lambda: sim, cfg, readonly=readonly, cycle_override_s=0.02, ec_wait_timeout_s=0.02,
                           on_snapshot=snaps.append, conflict_check=conflict_check,
                           conflict_interval_s=0.0)
        loop.start()
        deadline = time.time() + 5
        while len([s for s in snaps if s.fans]) < cycles and time.time() < deadline:
            time.sleep(0.01)
        return loop, snaps

    def test_ignored_sensors_are_not_read(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim)
        loop.stop()
        raw = snaps[-1].raw
        self.assertIn("CPU", raw)
        self.assertIn("GPU", raw)
        self.assertNotIn("X7F", raw)
        self.assertNotIn("BAT1", raw)

    def test_cycle_csv_log(self):
        from customfan.service import CycleLog
        import csv as _csv
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cycles.csv"
            sim = SimulatedEC(thermal=False)
            sim.set_cpu(60)
            cfg, _ = cfgmod.validate({})
            loop = ControlLoop(lambda: sim, cfg, cycle_override_s=0.02, ec_wait_timeout_s=0.02,
                               cycle_log=CycleLog(path))
            loop.start()
            time.sleep(0.3)
            loop.stop()
            rows = list(_csv.DictReader(path.open()))
            self.assertGreater(len(rows), 2)
            self.assertEqual(rows[-1]["cpu_c"], "60")
            self.assertEqual(rows[-1]["fan1_level"], "3")
            self.assertEqual(rows[-1]["fan2_level"], "3")

    def test_both_fans_follow_curve_and_exit_restores_auto(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim)
        self.assertEqual(sim.fan_level, {0: 3, 1: 3})          # both fans at level 3
        self.assertEqual(snaps[-1].status, ST_OK)
        loop.stop()
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})

    def test_readonly_never_writes_fan_registers(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(65)
        loop, snaps = self.run_loop(sim, readonly=True)
        loop.stop()
        self.assertFalse([w for w in sim.writes if w[0] == 0x2F])
        self.assertEqual(snaps[-1].status, ST_READONLY)
        self.assertIn("5", snaps[-1].message)

    def test_reasserts_after_ec_reset(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim)
        sim.ec_reset_to_auto()                                 # e.g. after sleep
        time.sleep(0.2)
        self.assertEqual(sim.fan_level, {0: 3, 1: 3})
        loop.stop()

    def test_user_auto_mode(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim)
        loop.set_mode("auto")
        time.sleep(0.2)
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})
        self.assertEqual(loop.snapshot().status, ST_AUTO)
        loop.stop()

    def test_hot_handover(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(95)
        loop, snaps = self.run_loop(sim)
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})
        self.assertEqual(snaps[-1].status, ST_HANDOVER)
        loop.stop()

    def test_stalled_fan_raises_alarm(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        sim.stalled_fans.add(1)                                # fan 2 dead
        loop, snaps = self.run_loop(sim, cycles=3, stall_check_s=10)
        loop._zero_since = {k: v - 100 for k, v in loop._zero_since.items()}  # fast-forward
        time.sleep(0.15)
        s = loop.snapshot()
        self.assertEqual(s.status, ST_FAULT)
        self.assertEqual(s.stalled, (2,))
        loop.stop()

    def test_dead_ec_hands_back_and_recovers(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim, max_ec_errors=2)
        sim.dead = True
        deadline = time.time() + 8
        while loop.snapshot().status != ST_FAULT and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(loop.snapshot().status, ST_FAULT)
        sim.dead = False
        deadline = time.time() + 8
        while loop.snapshot().status != ST_OK and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(loop.snapshot().status, ST_OK)
        loop.stop()

    def test_pauses_while_hwinfo_runs_and_resumes(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        running = []
        loop, snaps = self.run_loop(sim, conflict_check=lambda: list(running))
        self.assertEqual(sim.fan_level, {0: 3, 1: 3})
        running.append("HWiNFO64.exe")
        deadline = time.time() + 3
        while loop.snapshot().status != ST_PAUSED and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(loop.snapshot().status, ST_PAUSED)
        self.assertEqual(sim.fan_level, {0: LEVEL_AUTO, 1: LEVEL_AUTO})   # handed to the EC
        time.sleep(0.1)
        reads_before = sim.reads
        time.sleep(0.2)
        self.assertEqual(sim.reads, reads_before)                        # no EC polling while paused
        running.clear()
        time.sleep(0.3)
        self.assertEqual(loop.snapshot().status, ST_OK)
        self.assertEqual(sim.fan_level, {0: 3, 1: 3})
        loop.stop()

    def test_allow_hwinfo_setting_skips_pause(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim, conflict_check=lambda: ["HWiNFO64.exe"], allow_hwinfo=True)
        self.assertEqual(loop.snapshot().status, ST_OK)
        loop.stop()

    def test_settings_change_applies_live(self):
        sim = SimulatedEC(thermal=False)
        sim.set_cpu(60)
        loop, snaps = self.run_loop(sim)
        cfg = dict(loop.cfg, cpu_curve=[[50, 6], [80, 7]])
        loop.apply_config(cfg)
        time.sleep(0.2)
        self.assertEqual(sim.fan_level, {0: 6, 1: 6})
        loop.stop()


if __name__ == "__main__":
    unittest.main()


class FakeWinreg:
    """In-memory stand-in for winreg, enough for promote_tray_icon()."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_DWORD = 1, 2, 4

    class Key:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def __init__(self, entries):
        self.tree = {"HKCU": {r"Control Panel\NotifyIconSettings": entries}}

    def OpenKey(self, parent, sub, _res=0, _access=0):
        node = self.tree[parent] if isinstance(parent, str) else parent.data
        if sub not in node:
            raise OSError(sub)
        return FakeWinreg.Key(node[sub])

    def EnumKey(self, key, i):
        keys = list(key.data)
        if i >= len(keys):
            raise OSError("no more")
        return keys[i]

    def QueryValueEx(self, key, name):
        if name not in key.data:
            raise OSError(name)
        return key.data[name], 1

    def SetValueEx(self, key, name, _r, _t, value):
        key.data[name] = value


class TestTrayPin(unittest.TestCase):
    def run_promote(self, entries, exe):
        from customfan import winutil
        fake = FakeWinreg(entries)
        old_mod, old_win = sys.modules.get("winreg"), winutil.IS_WINDOWS
        sys.modules["winreg"], winutil.IS_WINDOWS = fake, True
        try:
            return winutil.promote_tray_icon(exe)
        finally:
            winutil.IS_WINDOWS = old_win
            if old_mod is None:
                sys.modules.pop("winreg", None)
            else:
                sys.modules["winreg"] = old_mod

    def test_matches_full_path_and_known_folder_prefix(self):
        from customfan.winutil import path_matches_exe
        self.assertTrue(path_matches_exe(r"C:\Tools\CustomFanController\dist\CustomFanController\CustomFanController.exe",
                                         "CustomFanController.exe"))
        self.assertTrue(path_matches_exe(r"{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}\Programs\Python\Python312\pythonw.exe",
                                         "pythonw.exe"))
        self.assertFalse(path_matches_exe(r"C:\X\NotCustomFanController.exe", "CustomFanController.exe"))
        self.assertFalse(path_matches_exe(r"C:\X\OneDrive.exe", "CustomFanController.exe"))

    def test_promotes_only_our_entry(self):
        entries = {
            "111": {"ExecutablePath": r"{6D809377}\Microsoft OneDrive\OneDrive.exe", "IsPromoted": 1},
            "222": {"ExecutablePath": r"C:\Tools\CustomFanController\dist\CustomFanController\CustomFanController.exe"},
            "333": {"ExecutablePath": r"C:\Other\App.exe", "IsPromoted": 0},
        }
        self.assertEqual(self.run_promote(entries, "CustomFanController.exe"), "promoted")
        self.assertEqual(entries["222"]["IsPromoted"], 1)
        self.assertEqual(entries["333"]["IsPromoted"], 0)          # untouched
        self.assertEqual(self.run_promote(entries, "CustomFanController.exe"), "already")

    def test_not_found_until_windows_registers_icon(self):
        self.assertEqual(self.run_promote({}, "CustomFanController.exe"), "not-found")

    def test_pin_memory_survives_config_roundtrip(self):
        cfg, problems = cfgmod.validate({"tray_pinned_for": ["CustomFanController.EXE"]})
        self.assertEqual(cfg["tray_pinned_for"], ["customfancontroller.exe"])
        self.assertFalse(problems)
