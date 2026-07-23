"""OS and (on Linux) display-session-type detection.

Capture-source selection in ffmpeg_utils depends on this: Windows picks
ddagrab/gdigrab, Linux picks x11grab on X11 and refuses (with a clear message)
on Wayland until the portal/PipeWire capture phase lands. See the "Wayland
caveat" section in ARCHITECTURE.md for why that's a separate phase.

The theme side uses system_dark_preferred(): the "system" theme mode's
best-effort read of whether the OS is currently in dark mode.
"""

from __future__ import annotations

import os
import platform
import subprocess
from enum import Enum

from clipersal.subprocess_utils import NO_WINDOW_KWARGS


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


def system_dark_preferred(os_: OS) -> bool:
    """Best-effort hint for the "system" theme mode: is the OS currently in
    dark mode? Read once at startup (and again on every Settings apply), not
    tracked live -- an OS theme flip mid-session only takes effect on the
    next apply/relaunch. Any probe failure reads as light: desktops without
    a dark-scheme setting (or without gsettings/the registry value at all)
    simply get the light theme, which was the only theme before dark mode
    existed. Never raises -- a theme hint is never worth crashing over.
    """
    if os_ == OS.WINDOWS:
        return _windows_dark_preferred()
    if os_ == OS.LINUX:
        return _linux_dark_preferred()
    # macOS capture isn't started and other OSes have no probe here at all.
    return False


# The per-user "apps use light theme" DWORD under Personalize -- 0 means the
# user switched apps to dark mode. This is the same setting Windows itself
# flips in Settings -> Personalization -> Colors.
_WINDOWS_THEME_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_WINDOWS_LIGHT_THEME_VALUE = "AppsUseLightTheme"


def _windows_dark_preferred() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_THEME_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, _WINDOWS_LIGHT_THEME_VALUE)
        return value == 0
    except Exception:  # noqa: BLE001 -- best-effort hint: any probe failure reads as light
        return False


def _linux_dark_preferred() -> bool:
    # GNOME (and GTK-ish desktops following it) expose the dark preference as
    # an enum whose dark value prints as 'prefer-dark'. Missing gsettings, a
    # missing schema, or a non-zero exit all leave stdout without "dark".
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            timeout=3,
            **NO_WINDOW_KWARGS,
        )
    except Exception:  # noqa: BLE001 -- best-effort hint: any probe failure reads as light
        return False
    return "dark" in result.stdout.lower()
