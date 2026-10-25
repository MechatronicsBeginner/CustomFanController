"""The "Copy report" text: everything someone needs to triage a problem, in one paste.

What goes in: program version, laptop model and BIOS (Windows registry), the
settings, the current readings, a few statistics and the newest log lines.
What stays out: your user name (paths are shortened to "~"), serial numbers,
and anything else from the machine. The text is shown for review by pasting it
somewhere you can read it before you post it.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from .events import LogEntry

# SHA-256 of LpcACPIEC.bin from TPFanCtrl2 v2.8.2 (the module this program was tested with).
EXPECTED_MODULE_SHA256 = "C38FD116E7AFF4D1FDB0A494E296BE0A6708E5A22FC72F14587442FB7F8F7906"
_BIOS_KEY = r"HARDWARE\DESCRIPTION\System\BIOS"


def machine_info() -> dict:
    """OS plus, on Windows, the laptop model and BIOS from the registry. Never raises."""
    info = {"os": platform.platform(), "python": platform.python_version()}
    if sys.platform != "win32":
        return info
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BIOS_KEY) as key:
            for field, value_name in (("manufacturer", "SystemManufacturer"), ("family", "SystemFamily"),
                                      ("product", "SystemProductName"), ("bios", "BIOSVersion"),
                                      ("bios_date", "BIOSReleaseDate")):
                try:
                    info[field] = str(winreg.QueryValueEx(key, value_name)[0]).strip()
                except OSError:
                    pass
    except Exception:
        pass
    return info


def sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
        return h.hexdigest().upper()
    except OSError:
        return None


def redact(text: str, home: Optional[str] = None) -> str:
    """Replace the user's home folder (either slash style) with ~ so the report doesn't carry their name."""
    home = home if home is not None else str(Path.home())
    if len(home) > 3:
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
    return text


def _fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _entry_line(e: LogEntry) -> str:
    return f"{_fmt_time(e.t)} {e.level:<7} {e.text}"


def build_report(*, version: str, frozen: bool, machine: dict, module_sha: Optional[str], cfg: dict,
                 snap, stats: dict, sensor_ranges: dict, entries: Iterable[LogEntry],
                 run_mode: str = "normal", now: Optional[float] = None, home: Optional[str] = None) -> str:
    now = time.time() if now is None else now
    entries = list(entries)
    out: list[str] = []
    add = out.append

    add(f"Custom Fan Controller report - {_fmt_time(now)}")
    add("Paths are shortened and no serial numbers are included. Read it before you post it.")
    add("")
    add("== System ==")
    add(f"Program: {version} ({'built exe' if frozen else 'script'}, Python {machine.get('python', '?')})")
    model = " ".join(x for x in (machine.get("manufacturer"), machine.get("family") or machine.get("product")) if x)
    add(f"Laptop: {model or 'unknown (not Windows, or the registry could not be read)'}")
    add(f"Run mode: {run_mode}" + ("  (no real hardware involved)" if run_mode == "simulated" else ""))
    if machine.get("product"):
        add(f"Machine type: {machine['product'][:4]}")
    if machine.get("bios"):
        bios = re.sub(r"\s+\)", ")", machine["bios"])          # Lenovo's string is "N3ZET57W (1.44 )"
        add(f"BIOS: {bios}  {machine.get('bios_date', '')}".rstrip())
    add(f"Windows/OS: {machine.get('os', '?')}")
    if module_sha is None:
        add("EC module (LpcACPIEC.bin): not found")
    else:
        same = module_sha == EXPECTED_MODULE_SHA256
        add(f"EC module (LpcACPIEC.bin): SHA-256 {module_sha[:16]}...  "
            f"{'matches the tested module' if same else 'DIFFERS from the tested module'}")
    add("")

    add("== Now ==")
    status = getattr(snap, "status", "?")
    add(f"Status: {status} - {getattr(snap, 'message', '')}")
    add(f"Mode: {getattr(snap, 'mode', '?')}   EC errors in a row: {getattr(snap, 'ec_errors', 0)}")
    temps = getattr(snap, "temps", None) or {}
    if temps:
        add("Temperatures: " + ", ".join(f"{k} {v}C" for k, v in temps.items()))
    for f in getattr(snap, "fans", ()) or ():
        lvl = "auto" if f.level == 0x80 else f.level
        add(f"Fan {f.fan}: level {lvl}, {f.rpm} RPM")
    add(f"Curve asks for: CPU level {getattr(snap, 'cpu_level', '?')}, GPU level {getattr(snap, 'gpu_level', '?')}")
    add("")

    add(f"== Last {stats.get('window', '30 min')} ==")
    peak = stats.get("peak")
    add("Peak temperature: " + (f"{peak[0]}C at {time.strftime('%H:%M', time.localtime(peak[1]))}" if peak else "-"))
    cph = stats.get("changes_per_hour")
    add("Fan level changes per hour: " + (f"{cph:.0f}" if cph is not None else "-"))
    add(f"Hand-overs to the EC: {stats.get('handovers', 0)}   Pauses (other EC tools): {stats.get('pauses', 0)}")
    if sensor_ranges:
        add("Sensor range since start: " + ", ".join(f"{k} {lo}-{hi}C" for k, (lo, hi) in sorted(sensor_ranges.items())))
    add("")

    add("== Settings ==")
    shown = {k: v for k, v in cfg.items() if k != "tray_pinned_for"}
    add(json.dumps(shown, indent=None, sort_keys=True))
    add("")

    problems = [e for e in entries if e.is_problem][-30:]
    add(f"== Warnings and errors in the log ({len(problems)} newest) ==")
    if problems:
        out.extend(_entry_line(e) for e in problems)
    else:
        add("(none)")
    add("")
    recent = entries[-60:]
    add(f"== Newest {len(recent)} log lines ==")
    if recent:
        out.extend(_entry_line(e) for e in recent)
    else:
        add("(none)")

    return redact("\n".join(out), home) + "\n"
