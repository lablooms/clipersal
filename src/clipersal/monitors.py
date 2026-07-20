"""Monitor enumeration for multi-monitor capture.

`list_monitors` returns physical displays in the order the OS hands them
over, and ffmpeg_utils' `monitor_index` indexes into exactly that order --
index 0 is whatever Windows/X11 itself considers monitor 0 (usually, but
not guaranteed to be, the primary display). On Windows this order USUALLY
lines up with the `output_idx` numbering ddagrab captures by, but
"usually" is the honest word: EnumDisplayMonitors' order is unspecified by
the OS, and it is a different enumeration from the DXGI EnumOutputs one
ddagrab numbers by, so nothing here may rely on the two matching. Either
way the common cases stay right: a single monitor, or
`Config.monitor_index = 0` (the default), needs zero new code in
ffmpeg_utils' capture-source builders to keep behaving exactly as it did
before monitor selection existed.

Enumeration failures (no xrandr, a ctypes call failing, an unsupported OS)
return an empty list rather than raising -- callers (the Settings window)
just hide the monitor picker when there's nothing useful to show, the same
"degrade quietly, don't crash" pattern as the audio-loopback probe in
ffmpeg_utils.py.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import sys
from dataclasses import dataclass

from clipersal.platform_detect import OS

log = logging.getLogger(__name__)

_XRANDR_TIMEOUT = 5


@dataclass
class MonitorInfo:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


_MONITORINFOF_PRIMARY = 0x1

# ctypes defaults every undeclared argument to C int, which TRUNCATES 64-bit
# handles: an HMONITOR that doesn't fit in 32 bits would arrive mangled back
# into GetMonitorInfoW, and the resulting ctypes.ArgumentError inside the
# enum callback is printed and swallowed by ctypes' foreign-callback
# machinery -- the enumeration silently stops early. Declaring argtypes /
# restype once, with the real handle types, keeps 64-bit values intact end
# to end. With today's small handle values nothing changes; this only
# matters where a handle exceeds 32 bits. (Guarded: ctypes.windll and
# WINFUNCTYPE only exist on Windows, and this module imports fine on Linux.)
if sys.platform == "win32":
    from ctypes import wintypes

    _MonitorEnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_RECT), wintypes.LPARAM
    )
    ctypes.windll.user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        _MonitorEnumProc,
        wintypes.LPARAM,
    ]
    ctypes.windll.user32.EnumDisplayMonitors.restype = wintypes.BOOL
    ctypes.windll.user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEX)]
    ctypes.windll.user32.GetMonitorInfoW.restype = wintypes.BOOL


def list_monitors(os_: OS) -> list[MonitorInfo]:
    if os_ == OS.WINDOWS:
        return _list_windows_monitors()
    if os_ == OS.LINUX:
        return _list_linux_monitors()
    return []


def _list_windows_monitors() -> list[MonitorInfo]:
    monitors: list[MonitorInfo] = []

    def _callback(hmonitor, _hdc, _lprect, _lparam) -> int:
        info = _MONITORINFOEX()
        info.cbSize = ctypes.sizeof(_MONITORINFOEX)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                MonitorInfo(
                    index=len(monitors),
                    name=info.szDevice,
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                    is_primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                )
            )
        return 1  # continue enumeration

    try:
        # None for the HDC and clip-rect args: the declared argtypes turn
        # them into the NULL values the old bare 0s produced.
        ctypes.windll.user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(_callback), 0)
    except (AttributeError, OSError) as exc:
        log.warning("Could not enumerate monitors via EnumDisplayMonitors: %s", exc)
        return []
    return monitors


_XRANDR_LINE_RE = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)")


def _list_linux_monitors() -> list[MonitorInfo]:
    try:
        # encoding/errors, not bare text=True: that decodes with the LOCALE
        # encoding under strict errors, so a non-ASCII output name under a
        # C/POSIX locale would raise UnicodeDecodeError -- escaping this
        # module's never-raises contract. xrandr emits UTF-8 on modern
        # systems; "replace" keeps a stray byte from ever becoming an
        # exception.
        result = subprocess.run(
            ["xrandr", "--query"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_XRANDR_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list monitors via xrandr: %s", exc)
        return []

    monitors: list[MonitorInfo] = []
    for line in result.stdout.splitlines():
        match = _XRANDR_LINE_RE.match(line)
        if not match:
            continue
        name, primary, width, height, x, y = match.groups()
        monitors.append(
            MonitorInfo(
                index=len(monitors),
                name=name,
                x=int(x),
                y=int(y),
                width=int(width),
                height=int(height),
                is_primary=bool(primary),
            )
        )
    return monitors
