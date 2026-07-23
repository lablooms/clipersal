"""Single-window enumeration for single-window capture mode, plus the
active (focused) window's title for the {window} clip-name placeholder.

Mirrors monitors.py's shape and failure philosophy: `list_windows` returns
an empty list (never raises) when enumeration isn't possible on this
platform/machine -- the Settings window just hides the window picker when
there's nothing useful to show. `active_window_title` likewise returns None
(never raises) when the focused window's title can't be read, and
concat.render_filename degrades the {window} placeholder to "clip".

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
import sys
from dataclasses import dataclass

from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_WMCTRL_TIMEOUT = 5
_XPROP_TIMEOUT = 5


@dataclass
class WindowInfo:
    handle: str
    title: str
    x: int
    y: int
    width: int
    height: int


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


# Same truncation trap as monitors.py (see its longer note): ctypes defaults
# undeclared arguments to C int, so a 64-bit HWND would be cut to 32 bits on
# its way into IsWindowVisible / GetWindowTextW / GetWindowRect / IsIconic,
# and the ArgumentError raised inside the enum callback is printed and
# swallowed by ctypes -- a silently truncated window list. Declaring
# argtypes/restype once with the real handle types keeps handles intact;
# behavior with today's small handle values is unchanged. (Guarded because
# ctypes.windll and WINFUNCTYPE are Windows-only.)
if sys.platform == "win32":
    from ctypes import wintypes

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    ctypes.windll.user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    ctypes.windll.user32.EnumWindows.restype = wintypes.BOOL
    ctypes.windll.user32.IsWindowVisible.argtypes = [wintypes.HWND]
    ctypes.windll.user32.IsWindowVisible.restype = wintypes.BOOL
    ctypes.windll.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    ctypes.windll.user32.GetWindowTextLengthW.restype = ctypes.c_int
    ctypes.windll.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    ctypes.windll.user32.GetWindowTextW.restype = ctypes.c_int
    ctypes.windll.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    ctypes.windll.user32.GetWindowRect.restype = wintypes.BOOL
    ctypes.windll.user32.IsIconic.argtypes = [wintypes.HWND]
    ctypes.windll.user32.IsIconic.restype = wintypes.BOOL
    # Same truncation trap for the foreground-window handle read by
    # active_window_title: restype stays a full-width HWND (a void*), not
    # the default C int.
    ctypes.windll.user32.GetForegroundWindow.argtypes = []
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND


def list_windows(os_: OS) -> list[WindowInfo]:
    if os_ == OS.WINDOWS:
        return _list_windows_windows()
    if os_ == OS.LINUX:
        return _list_linux_windows()
    return []


def active_window_title(os_: OS, session_type: LinuxSessionType | None = None) -> str | None:
    """Title of the window that currently has keyboard focus, or None when
    it can't be determined -- Wayland (the portal deliberately doesn't
    expose other apps' titles), an unsupported OS, or any probe failure.
    Never raises: cli.py feeds this to the {window} filename placeholder on
    every save, so a probe hiccup must degrade the name to "clip", not fail
    the save.
    """
    if os_ == OS.WINDOWS:
        return _active_window_title_windows()
    if os_ == OS.LINUX and session_type == LinuxSessionType.X11:
        return _active_window_title_x11()
    return None


def _list_windows_windows() -> list[WindowInfo]:
    windows: list[WindowInfo] = []

    def _callback(hwnd, _lparam) -> int:
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return 1
        if user32.IsIconic(hwnd):
            # A MINIMIZED window is still visible per IsWindowVisible, and
            # its GetWindowRect returns the iconic rect -- a (-32000,-32000)
            # origin with a positive size, so it would sail through the
            # width/height check below and show up in the picker, capturing
            # nothing but black/frozen frames. Exclude it explicitly.
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
            return 1  # degenerate (zero-area) rect -- nothing capturable
        windows.append(WindowInfo(handle=str(hwnd), title=title, x=rect.left, y=rect.top, width=width, height=height))
        return 1

    try:
        ctypes.windll.user32.EnumWindows(_EnumWindowsProc(_callback), 0)
    except (AttributeError, OSError) as exc:
        log.warning("Could not enumerate windows via EnumWindows: %s", exc)
        return []
    return windows


def _active_window_title_windows() -> str | None:
    """The foreground window's title via GetForegroundWindow +
    GetWindowTextW -- the same Unicode-safe, full-width-handle calls the
    enumeration path above uses (see the prototype block at the top)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None  # no foreground window (e.g. the desktop has focus)
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None  # foreground window has no title
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
    except (AttributeError, OSError) as exc:
        log.warning("Could not read the foreground window title: %s", exc)
        return None
    return title or None


# wmctrl -lG columns: handle, desktop, x, y, width, height, host, title.
# x/y can be NEGATIVE -- a window on another viewport or hanging off the
# left/top screen edge (e.g. "0x0201c24f  0 -2552 96 ...") -- and a plain
# \d+ would fail to match, silently dropping those windows from the picker.
_WMCTRL_LINE_RE = re.compile(r"^(\S+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$")

# xprop prints "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL" when
# the property is set, "_NET_WM_WINDOW_TYPE: not found." when it isn't.
_XPROP_TYPE_RE = re.compile(r"_NET_WM_WINDOW_TYPE\(ATOM\) = (\S+)")


def _xprop_window_type(handle: str) -> str | None:
    """The _NET_WM_WINDOW_TYPE atom of one window (e.g.
    "_NET_WM_WINDOW_TYPE_DESKTOP"), or None when xprop can't say -- not
    installed, property unset, timeout. Best-effort like every other probe
    here; the caller treats None as "unknown", never as a type. Same
    utf-8/replace decoding as the xrandr call in monitors.py (see there).
    """
    try:
        result = subprocess.run(
            ["xprop", "-id", handle, "_NET_WM_WINDOW_TYPE"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_XPROP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not query the window type of %s via xprop: %s", handle, exc)
        return None
    match = _XPROP_TYPE_RE.search(result.stdout)
    return match.group(1) if match else None


def _list_linux_windows() -> list[WindowInfo]:
    try:
        # encoding/errors, not bare text=True -- see the xrandr call in
        # monitors.py for why (a non-ASCII window title under a C/POSIX
        # locale must not escape as UnicodeDecodeError).
        result = subprocess.run(
            ["wmctrl", "-l", "-G"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_WMCTRL_TIMEOUT,
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
            # wmctrl reports a sticky ("always on the visible workspace")
            # window with desktop 0xFFFFFFFF (parsed as -1) -- and that
            # covers BOTH the desktop background itself and genuine sticky
            # user windows. Only the background must go, so ask xprop for
            # the window type and keep NORMAL ones. When the type is
            # unknown (no xprop, no property) keep the old blanket
            # exclusion: a missing sticky window in the picker is
            # recoverable via Refresh, while offering the desktop
            # background as a capturable "window" is not.
            if _xprop_window_type(handle) != "_NET_WM_WINDOW_TYPE_NORMAL":
                continue
        title = title.strip()
        if not title:
            continue
        windows.append(
            WindowInfo(handle=handle, title=title, x=int(x), y=int(y), width=int(width), height=int(height))
        )
    return windows


# "xprop -root _NET_ACTIVE_WINDOW" prints
# "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x03a00011", or window id # 0x0
# when nothing has focus.
_XPROP_ACTIVE_WINDOW_RE = re.compile(r"window id # (0x[0-9a-fA-F]+)")

# "xprop -id <id> _NET_WM_NAME" prints '_NET_WM_NAME(UTF8_STRING) = "Some
# Title"' when the property is set, "_NET_WM_NAME: not found." when it
# isn't.
_XPROP_WM_NAME_RE = re.compile(r'_NET_WM_NAME\(\S+\) = "(.*)"')


def _active_window_title_x11() -> str | None:
    """The focused window's title via two xprop probes: _NET_ACTIVE_WINDOW
    on the root window names the focused window's id, then _NET_WM_NAME on
    that id is the title. Same never-raises, utf-8/replace decoding
    discipline as _xprop_window_type above.
    """
    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_XPROP_TIMEOUT,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not query the active window via xprop: %s", exc)
        return None
    match = _XPROP_ACTIVE_WINDOW_RE.search(result.stdout)
    if not match or match.group(1) == "0x0":
        return None
    try:
        result = subprocess.run(
            ["xprop", "-id", match.group(1), "_NET_WM_NAME"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_XPROP_TIMEOUT,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not query the active window title via xprop: %s", exc)
        return None
    match = _XPROP_WM_NAME_RE.search(result.stdout)
    if not match:
        return None
    title = match.group(1).strip()
    return title or None
