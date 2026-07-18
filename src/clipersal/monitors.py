"""Monitor enumeration for multi-monitor capture.

`list_monitors` returns physical displays in an order that lines up with
ffmpeg_utils' `monitor_index` -- index 0 is whatever Windows/X11 itself
considers monitor 0 (usually, but not guaranteed to be, the primary
display), same as ddagrab's own `output_idx` numbering. That's deliberate:
it means `Config.monitor_index = 0` (the default) needs zero new code in
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


def list_monitors(os_: OS) -> list[MonitorInfo]:
    if os_ == OS.WINDOWS:
        return _list_windows_monitors()
    if os_ == OS.LINUX:
        return _list_linux_monitors()
    return []


def _list_windows_monitors() -> list[MonitorInfo]:
    monitors: list[MonitorInfo] = []

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
    _MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_ssize_t
    )

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
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, _MonitorEnumProc(_callback), 0)
    except (AttributeError, OSError) as exc:
        log.warning("Could not enumerate monitors via EnumDisplayMonitors: %s", exc)
        return []
    return monitors


_XRANDR_LINE_RE = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)")


def _list_linux_monitors() -> list[MonitorInfo]:
    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=_XRANDR_TIMEOUT
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
