"""Launch-on-startup registration.

Windows: a value under the per-user Run registry key
(``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``), which the OS
runs silently at login -- no visible console/window flash regardless of how
it's registered, as long as the registered command itself launches a
``--windowed`` exe directly rather than a ``.bat``/``cmd`` wrapper (a wrapper
is exactly the kind of thing that would reintroduce a console flash).

Linux: a ``.desktop`` file in ``~/.config/autostart/``, the XDG autostart
convention nearly every desktop environment honors.

macOS is not yet supported (capture itself isn't implemented there yet);
``is_supported`` gates this the same way ``hotkey.py`` gates global-hotkey
support per platform.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from clipersal.platform_detect import OS

log = logging.getLogger(__name__)

_REGISTRY_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REGISTRY_VALUE_NAME = "Clipersal"


def is_supported(os_: OS) -> bool:
    return os_ in (OS.WINDOWS, OS.LINUX)


def launch_command() -> list[str]:
    """The command that re-launches clipersal: the frozen exe's own
    path when packaged (``sys.frozen``, set by PyInstaller), or `python -m
    clipersal.cli` when running from source. The from-source form is a
    dev convenience tied to this interpreter/venv -- documented as such,
    since a venv that gets moved or removed would silently break it.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "clipersal.cli"]


def _autostart_desktop_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / "clipersal.desktop"


def is_enabled(os_: OS) -> bool:
    if os_ == OS.WINDOWS:
        return _windows_read_value() is not None
    if os_ == OS.LINUX:
        return _autostart_desktop_path().exists()
    return False


def enable(os_: OS) -> None:
    if os_ == OS.WINDOWS:
        _windows_set_value(subprocess.list2cmdline(launch_command()))
    elif os_ == OS.LINUX:
        _linux_write_desktop_file(launch_command())
    else:
        raise NotImplementedError(f"Launch on startup is not supported on {os_}")
    log.info("Launch on startup enabled")


def disable(os_: OS) -> None:
    if os_ == OS.WINDOWS:
        _windows_delete_value()
    elif os_ == OS.LINUX:
        _autostart_desktop_path().unlink(missing_ok=True)
    else:
        raise NotImplementedError(f"Launch on startup is not supported on {os_}")
    log.info("Launch on startup disabled")


def _windows_set_value(command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY_PATH) as key:
        winreg.SetValueEx(key, _REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, command)


def _windows_read_value() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, _REGISTRY_VALUE_NAME)
            return value
    except FileNotFoundError:
        return None


def _windows_delete_value() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _REGISTRY_VALUE_NAME)
    except FileNotFoundError:
        pass


# Characters the Desktop Entry Spec reserves in Exec arguments (space is
# the argument separator, so it is what actually forces quoting).
_DESKTOP_ENTRY_RESERVED = " \t\n\"\\'`$<>~|&;*?#()"


def _desktop_entry_quote(arg: str) -> str:
    """Quote one Exec argument per the Desktop Entry Spec, which does NOT
    shell-parse the Exec line: quoting is double-quotes-only with the
    double quote, backtick, dollar sign, and backslash escaped by a
    preceding backslash -- a single quote is a reserved character treated
    LITERALLY, so ``shlex.quote`` would turn a space-containing AppImage
    path into two bogus arguments and login autostart would silently fail.
    A literal ``%`` must also be doubled everywhere, quoted or not, because
    ``%<char>`` sequences are field codes the launcher expands.
    https://specifications.freedesktop.org/desktop-entry-spec/latest/exec-variables.html
    """
    arg = arg.replace("%", "%%")
    if arg and not any(char in arg for char in _DESKTOP_ENTRY_RESERVED):
        return arg
    for char in ("\\", '"', "`", "$"):
        arg = arg.replace(char, "\\" + char)
    return f'"{arg}"'


def _linux_write_desktop_file(command: list[str]) -> None:
    path = _autostart_desktop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(_desktop_entry_quote(part) for part in command)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Clipersal\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    path.write_text(content, encoding="utf-8")
