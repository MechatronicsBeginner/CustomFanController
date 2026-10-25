"""Settings file (config.json next to the program).

Everything is validated on load, so a hand edit with a typo can't produce an
unsafe curve; bad values are replaced with defaults and the problem is logged.
Saves are atomic (write temp file, then rename) so a crash mid-save can't
leave a half-written file.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

from .control import PolicySettings

log = logging.getLogger(__name__)

PROTECTED_SENSORS = ("CPU", "GPU")   # always used, can't be ignored

DEFAULTS: dict = {
    "mode": "curve",                    # "curve" or "auto"
    "cycle_s": 3,
    "deadband_c": 4,
    "step_down_delay_s": 15,
    "cpu_curve": [[52, 1], [58, 3], [64, 5], [70, 7]],
    "gpu_curve": [[60, 1], [66, 3], [72, 5], [78, 7]],
    "handover_c": 92,
    "resume_c": 85,
    "ignore_sensors": ["APS", "BAT1", "BUS", "PCI", "PWR", "X7F", "XC3"],
    "stall_check_s": 20,
    "max_ec_errors": 5,
    "allow_hwinfo": False,              # True only if HWiNFO's EC support is turned off
    "tray_pinned_for": [],              # exe names whose tray icon we've pinned once
}

LIMITS = {
    "cycle_s": (1, 10),
    "deadband_c": (1, 10),
    "step_down_delay_s": (0, 120),
    "handover_c": (80, 99),
    "resume_c": (60, 95),
    "stall_check_s": (10, 120),
    "max_ec_errors": (2, 30),
}
TEMP_RANGE = (35, 95)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _valid_curve(raw) -> bool:
    try:
        steps = [(int(t), int(l)) for t, l in raw]
    except (TypeError, ValueError):
        return False
    if not 1 <= len(steps) <= 8:
        return False
    temps = [t for t, _ in steps]
    levels = [l for _, l in steps]
    return (all(TEMP_RANGE[0] <= t <= TEMP_RANGE[1] for t in temps)
            and all(a < b for a, b in zip(temps, temps[1:]))
            and all(0 <= l <= 7 for l in levels)
            and all(a <= b for a, b in zip(levels, levels[1:])))


def validate(cfg: dict) -> tuple[dict, list[str]]:
    """Return (clean config, list of problems fixed)."""
    out = copy.deepcopy(DEFAULTS)
    problems: list[str] = []
    for key, default in DEFAULTS.items():
        if key not in cfg:
            continue
        val = cfg[key]
        if key in LIMITS:
            try:
                lo, hi = LIMITS[key]
                v = int(val)
                if v != _clamp(v, lo, hi):
                    problems.append(f"{key}={v} out of range {lo}-{hi}, clamped")
                out[key] = _clamp(v, lo, hi)
            except (TypeError, ValueError):
                problems.append(f"{key}={val!r} invalid, using {default}")
        elif key in ("cpu_curve", "gpu_curve"):
            if _valid_curve(val):
                out[key] = [[int(t), int(l)] for t, l in val]
            else:
                problems.append(f"{key} invalid (temps must increase, levels 0-7 and non-decreasing), using default")
        elif key == "mode":
            if val in ("curve", "auto"):
                out[key] = val
            else:
                problems.append(f"mode={val!r} invalid, using curve")
        elif key == "allow_hwinfo":
            out[key] = bool(val)
        elif key == "tray_pinned_for":
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                out[key] = sorted({x.lower() for x in val})
        elif key == "ignore_sensors":
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                out[key] = sorted({x.upper() for x in val} - set(PROTECTED_SENSORS))
            else:
                problems.append("ignore_sensors invalid, using default")
    if out["resume_c"] > out["handover_c"] - 3:
        out["resume_c"] = out["handover_c"] - 3
        problems.append("resume_c must be at least 3°C below handover_c, adjusted")
    return out, problems


def to_policy(cfg: dict) -> PolicySettings:
    return PolicySettings(
        cpu_steps=[tuple(s) for s in cfg["cpu_curve"]],
        gpu_steps=[tuple(s) for s in cfg["gpu_curve"]],
        deadband=cfg["deadband_c"],
        down_delay_s=cfg["step_down_delay_s"],
        handover_c=cfg["handover_c"],
        resume_c=cfg["resume_c"],
        ignore=set(cfg["ignore_sensors"]),
    )


def load(path: Path) -> dict:
    if not path.exists():
        cfg = copy.deepcopy(DEFAULTS)
        save(path, cfg)
        log.info("Created default settings at %s", path)
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object")
    except (OSError, ValueError) as e:
        backup = path.with_suffix(".bad.json")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        log.error("Settings file unreadable (%s); moved to %s and using defaults", e, backup.name)
        cfg = copy.deepcopy(DEFAULTS)
        save(path, cfg)
        return cfg
    cfg, problems = validate(raw)
    for p in problems:
        log.warning("Settings: %s", p)
    return cfg


def save(path: Path, cfg: dict) -> None:
    clean, _ = validate(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    os.replace(tmp, path)
