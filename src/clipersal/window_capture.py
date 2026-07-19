"""Single-window enumeration for single-window capture mode.

Mirrors monitors.py's shape and failure philosophy: `list_windows` returns
an empty list (never raises) when enumeration isn't possible on this
platform/machine -- the Settings window just hides the window picker when
there's nothing useful to show.

Windows are matched by *title* (gdigrab's own `-i title=<title>` capture
mode has no handle-based alternative in stock ffmpeg), so two windows that
happen to share an exact title are ambiguous -- ffmpeg picks whichever it
finds first. That's a real, documented limitation, not an oversight; most
windows (browsers with a page title, editors with a filename) don't collide
in practice.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
from dataclasses import dataclass

from clipersal.platform_detect import OS

log = logging.getLogger(__name__)

_WMCTRL_TIMEOUT = 5


@dataclass
class WindowInfo:
    handle: str
    title: str
    x: int
    y: int
    width: int
    height: int


def list_windows(os_: OS) -> list[WindowInfo]:
    if os_ == OS.WINDOWS:
        return _list_windows_windows()
    if os_ == OS.LINUX:
        return _list_linux_windows()
    return []


def _list_windows_windows() -> list[WindowInfo]:
    windows: list[WindowInfo] = []

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    _EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

    def _callback(hwnd, _lparam) -> int:
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return 1
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return 1
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return 1
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 1
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return 1  # minimized or degenerate -- nothing capturable
        windows.append(WindowInfo(handle=str(hwnd), title=title, x=rect.left, y=rect.top, width=width, height=height))
        return 1

    try:
        ctypes.windll.user32.EnumWindows(_EnumWindowsProc(_callback), 0)
    except (AttributeError, OSError) as exc:
        log.warning("Could not enumerate windows via EnumWindows: %s", exc)
        return []
    return windows


# wmctrl -lG columns: handle, desktop, x, y, width, height, host, title.
# x/y can be NEGATIVE -- a window on another viewport or hanging off the
# left/top screen edge (e.g. "0x0201c24f  0 -2552 96 ...") -- and a plain
# \d+ would fail to match, silently dropping those windows from the picker.
_WMCTRL_LINE_RE = re.compile(r"^(\S+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$")


def _list_linux_windows() -> list[WindowInfo]:
    try:
        result = subprocess.run(
            ["wmctrl", "-l", "-G"], capture_output=True, text=True, timeout=_WMCTRL_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list windows via wmctrl (is it installed?): %s", exc)
        return []

    windows: list[WindowInfo] = []
    for line in result.stdout.splitlines():
        match = _WMCTRL_LINE_RE.match(line)
        if not match:
            continue
        handle, desktop, x, y, width, height, _host, title = match.groups()
        if int(desktop) < 0:
            continue  # sticky/special windows (e.g. the desktop background itself)
        title = title.strip()
        if not title:
            continue
        windows.append(
            WindowInfo(handle=handle, title=title, x=int(x), y=int(y), width=int(width), height=int(height))
        )
    return windows
