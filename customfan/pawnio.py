"""PawnIO backend (Windows only).

PawnIO is a Microsoft-signed kernel driver that only runs signed modules.
The LpcACPIEC module it loads here allows exactly two operations, on exactly
two ports (0x62/0x66): read a port byte, write a port byte. This mirrors
PawnIOProvider.cpp in TPFanCtrl2 v2.8.2, which was confirmed working on this
laptop.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
from ctypes import wintypes
from pathlib import Path

from .ec import CMD_PORT, DATA_PORT, ECError

log = logging.getLogger(__name__)

ALLOWED_PORTS = (CMD_PORT, DATA_PORT)
# SHA-256 of LpcACPIEC.bin shipped with TPFanCtrl2 v2.8.2 (size 2612 bytes).
KNOWN_MODULE_SHA256 = "c38fd116e7aff4d1fdb0a494e296be0a6708e5a22fc72f14587442fb7f8f7906"
EC_MUTEX_NAMES = ("Global\\Access_EC", "Access_EC")

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080


class PawnIOError(RuntimeError):
    pass


def _find_pawnio_lib() -> Path:
    candidates = [Path(r"C:\Program Files\PawnIO\PawnIOLib.dll")]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO") as key:
            loc, _ = winreg.QueryValueEx(key, "InstallLocation")
            candidates.append(Path(loc) / "PawnIOLib.dll")
    except OSError:
        pass
    for c in candidates:
        if c.is_file():
            return c
    raise PawnIOError("PawnIOLib.dll not found. Install the driver with:  winget install namazso.PawnIO")


class PawnIOPortIO:
    def __init__(self, module_path: Path) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise PawnIOError("64-bit Python is required (PawnIOLib.dll is 64-bit)")

        lib_path = _find_pawnio_lib()
        log.info("Loading %s", lib_path)
        lib = ctypes.WinDLL(str(lib_path))
        HRESULT = ctypes.c_long
        H = wintypes.HANDLE
        lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
        lib.pawnio_version.restype = HRESULT
        lib.pawnio_open.argtypes = [ctypes.POINTER(H)]
        lib.pawnio_open.restype = HRESULT
        lib.pawnio_load.argtypes = [H, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        lib.pawnio_load.restype = HRESULT
        lib.pawnio_execute.argtypes = [H, ctypes.c_char_p,
                                       ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
        lib.pawnio_execute.restype = HRESULT
        lib.pawnio_close.argtypes = [H]
        lib.pawnio_close.restype = HRESULT
        self._lib = lib

        ver = wintypes.ULONG(0)
        if lib.pawnio_version(ctypes.byref(ver)) >= 0:
            log.info("PawnIO driver version 0x%06X", ver.value)

        self._handle = H()
        hr = lib.pawnio_open(ctypes.byref(self._handle))
        if hr < 0 or not self._handle:
            raise PawnIOError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}). "
                              "Is the program running as Administrator?")

        blob = Path(module_path).read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != KNOWN_MODULE_SHA256:
            log.warning("LpcACPIEC.bin hash %s differs from the TPFanCtrl2 v2.8.2 copy; "
                        "PawnIO will still reject it unless it is properly signed", digest)
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        hr = lib.pawnio_load(self._handle, buf, len(blob))
        if hr < 0:
            self.close()
            raise PawnIOError(f"pawnio_load(LpcACPIEC) failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        log.info("LpcACPIEC module loaded (%d bytes)", len(blob))

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = H
        k32.WaitForSingleObject.argtypes = [H, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ReleaseMutex.argtypes = [H]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [H]
        self._k32 = k32
        self._mutex = None
        for name in EC_MUTEX_NAMES:
            m = k32.CreateMutexW(None, False, name)
            if m:
                self._mutex = m
                log.info("Using EC mutex %s", name)
                break
        if not self._mutex:
            raise PawnIOError("could not open the Access_EC mutex")

        self._out = (ctypes.c_uint64 * 1)()
        self._ret = ctypes.c_size_t(0)

    # ------------------------------------------------------------ PortIO API
    def read_port(self, port: int) -> int:
        if port not in ALLOWED_PORTS:
            raise ValueError(f"port 0x{port:X} not allowed")
        inp = (ctypes.c_uint64 * 1)(port)
        hr = self._lib.pawnio_execute(self._handle, b"ioctl_pio_read", inp, 1,
                                      self._out, 1, ctypes.byref(self._ret))
        if hr < 0 or self._ret.value < 1:
            raise ECError(f"PawnIO port read 0x{port:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        return int(self._out[0]) & 0xFF

    def write_port(self, port: int, value: int) -> None:
        if port not in ALLOWED_PORTS:
            raise ValueError(f"port 0x{port:X} not allowed")
        inp = (ctypes.c_uint64 * 2)(port, value & 0xFF)
        ret = ctypes.c_size_t(0)
        hr = self._lib.pawnio_execute(self._handle, b"ioctl_pio_write", inp, 2,
                                      None, 0, ctypes.byref(ret))
        if hr < 0:
            raise ECError(f"PawnIO port write 0x{port:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")

    def acquire_lock(self, timeout_ms: int) -> bool:
        res = self._k32.WaitForSingleObject(self._mutex, timeout_ms)
        return res in (WAIT_OBJECT_0, WAIT_ABANDONED)

    def release_lock(self) -> None:
        self._k32.ReleaseMutex(self._mutex)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._lib.pawnio_close(self._handle)
            self._handle = wintypes.HANDLE()
        if getattr(self, "_mutex", None):
            self._k32.CloseHandle(self._mutex)
            self._mutex = None
