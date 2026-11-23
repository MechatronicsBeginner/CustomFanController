"""Small Windows helpers. All functions are harmless no-ops on other OSes."""
from __future__ import annotations

import ctypes
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
ERROR_ALREADY_EXISTS = 183

# Other programs that write to the EC. Running two controllers at once is
# exactly the contention problem this project set out to fix.
OTHER_CONTROLLERS = ("TPFanCtrl2.exe", "TPFanCtrl2-top.exe", "TPFanControl.exe", "fancontrol.exe")

# Monitors that read the EC without honouring the Access_EC lock. Their reads
# interleave with ours in the EC's one shared mailbox and corrupt both sides
# (observed: fan 2 reading level 0x90 / 37008 RPM while HWiNFO was open).
EC_MONITORS = ("HWiNFO64.exe", "HWiNFO32.exe", "HWiNFO.exe")

_instance_handle = None


def is_admin() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def claim_single_instance(name: str = "Local\\CustomFanController.SingleInstance") -> bool:
    """True if we're the only copy running. Keeps the mutex for process lifetime."""
    global _instance_handle
    if not IS_WINDOWS:
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    h = k32.CreateMutexW(None, False, name)
    if not h:
        return True                         # can't tell; don't block startup
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    _instance_handle = h
    return True


# ------------------------------------------------------------ tray pinning
# Windows 11 (22H2+) keeps one subkey per tray-icon program under
#   HKCU\Control Panel\NotifyIconSettings\<random id>
# with ExecutablePath (the program) and IsPromoted (DWORD: 1 = always shown on
# the taskbar, 0 = in the ^ overflow). The subkey only appears after the
# program has shown its icon once. Windows identifies the icon by the
# executable's FULL PATH: the same exe copied to a new folder is a new entry
# (unpinned), so pin memory and matching must use the path, not the file name.
# Under pythonw.exe pinning would pin every pythonw tray icon; the built
# CustomFanController.exe gets its own entry.
NOTIFY_ICON_KEY = r"Control Panel\NotifyIconSettings"

PIN_PROMOTED = "promoted"        # we just set IsPromoted=1
PIN_ALREADY = "already"          # it was already pinned
PIN_NOT_FOUND = "not-found"      # Windows hasn't created the entry yet
PIN_UNSUPPORTED = "unsupported"  # not Windows 11 22H2+, or no registry access


def _norm(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


def path_matches_exe(executable_path: str, exe: str) -> bool:
    """Does a NotifyIconSettings ExecutablePath refer to `exe`?

    `exe` is normally a full path, and then only that exact file matches - a
    copy of the same program in another folder is a different tray entry.
    ExecutablePath may also start with a known-folder GUID instead of a drive,
    e.g. '{F1B32785-...}\\Programs\\Python\\Python312\\pythonw.exe'; then the part
    after the GUID must be the tail of `exe`. A bare file name (no folder)
    keeps the old match-by-name behaviour."""
    p, target = _norm(executable_path), _norm(exe)
    if "\\" not in target:                       # bare name
        return p == target or p.endswith("\\" + target)
    if p == target:
        return True
    if p.startswith("{") and "}\\" in p:          # known-folder form
        rest = p.split("}\\", 1)[1]
        return bool(rest) and target.endswith("\\" + rest)
    return False


def needs_pin(exe_path: str, pinned_for: list[str]) -> bool:
    """True unless this exact exe path was pinned before. Entries from older
    versions that stored only the file name do not count, because Windows
    treats each folder's copy as a separate tray icon."""
    return _norm(exe_path) not in {_norm(x) for x in pinned_for}


def promote_tray_icon(exe_path: str) -> str:
    if not IS_WINDOWS:
        return PIN_UNSUPPORTED
    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_ICON_KEY, 0, winreg.KEY_READ)
    except OSError:
        return PIN_UNSUPPORTED
    found, changed = 0, 0
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    path, _ = winreg.QueryValueEx(k, "ExecutablePath")
                    if not path_matches_exe(str(path), exe_path):
                        continue
                    found += 1
                    try:
                        current, _ = winreg.QueryValueEx(k, "IsPromoted")
                    except OSError:
                        current = 0
                    if current != 1:
                        winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        changed += 1
            except OSError:
                continue
    if not found:
        return PIN_NOT_FOUND
    return PIN_PROMOTED if changed else PIN_ALREADY


def running_controllers() -> list[str]:
    """Names of other fan-control programs currently running."""
    return running_processes(OTHER_CONTROLLERS)


def running_ec_monitors() -> list[str]:
    return running_processes(EC_MONITORS)


def running_processes(names: tuple[str, ...]) -> list[str]:
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
    except Exception:
        return []
    found = set()
    for line in out.splitlines():
        exe = line.split(",")[0].strip('"')
        for name in names:
            if exe.lower() == name.lower():
                found.add(exe)
    return sorted(found)
