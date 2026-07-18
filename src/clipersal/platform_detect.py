"""OS and (on Linux) display-session-type detection.

Capture-source selection in ffmpeg_utils depends on this: Windows picks
ddagrab/gdigrab, Linux picks x11grab on X11 and refuses (with a clear message)
on Wayland until the portal/PipeWire capture phase lands. See the "Wayland
caveat" section in ARCHITECTURE.md for why that's a separate phase.
"""

from __future__ import annotations

import os
import platform
from enum import Enum


class OS(Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    OTHER = "other"


class LinuxSessionType(Enum):
    X11 = "x11"
    WAYLAND = "wayland"
    UNKNOWN = "unknown"


def get_os() -> OS:
    system = platform.system()
    if system == "Windows":
        return OS.WINDOWS
    if system == "Linux":
        return OS.LINUX
    if system == "Darwin":
        return OS.MACOS
    return OS.OTHER


def get_linux_session_type() -> LinuxSessionType:
    """Best-effort detection of X11 vs Wayland.

    Prefers XDG_SESSION_TYPE (set by most login managers). Falls back to
    checking for WAYLAND_DISPLAY vs DISPLAY when that's unset or unrecognized,
    since some minimal/embedded setups don't export it.
    """
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session_type == "x11":
        return LinuxSessionType.X11
    if session_type == "wayland":
        return LinuxSessionType.WAYLAND

    if os.environ.get("WAYLAND_DISPLAY"):
        return LinuxSessionType.WAYLAND
    if os.environ.get("DISPLAY"):
        return LinuxSessionType.X11

    return LinuxSessionType.UNKNOWN
