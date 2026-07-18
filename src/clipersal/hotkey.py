"""Real global hotkey binding for Windows and Linux/X11, via pynput.

Per ARCHITECTURE.md's "IPC / hotkey boundary": this layer's only job is deciding
*when* a save should happen and forwarding that to the IPC server (ipc.py) --
it has no knowledge of capture/concat internals, and could be swapped for a
different hotkey library, or moved into a separate sidecar process, without
touching anything else.

Not usable on Wayland: there is no cross-desktop-environment global-hotkey
API there by design (same reason Wayland screen capture needs the portal --
see the Wayland caveat in ARCHITECTURE.md). On Wayland, `is_supported()` returns
False and callers should point the user at binding `clipersal-trigger
save` to a compositor/DE-level custom keybinding instead (trigger.py).
"""

from __future__ import annotations

import logging
from typing import Callable

from clipersal.platform_detect import OS, LinuxSessionType

log = logging.getLogger(__name__)

# pynput hotkey-string format, same across the Windows and X11 backends.
DEFAULT_COMBO = "<ctrl>+<alt>+r"


class HotkeyUnsupportedError(RuntimeError):
    pass


def is_supported(os_: OS, session_type: LinuxSessionType | None) -> bool:
    """Whether a real global hotkey can plausibly be registered here."""
    if os_ == OS.WINDOWS:
        return True
    if os_ == OS.LINUX:
        return session_type == LinuxSessionType.X11
    return False


class HotkeyListener:
    def __init__(self, combo: str, callback: Callable[[], None]):
        self._combo = combo
        self._callback = callback
        self._hotkeys = None

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise HotkeyUnsupportedError(
                "pynput is not installed; global hotkey binding is unavailable"
            ) from exc

        try:
            self._hotkeys = keyboard.GlobalHotKeys({self._combo: self._callback})
            self._hotkeys.start()
        except Exception as exc:
            raise HotkeyUnsupportedError(f"Could not bind global hotkey {self._combo!r}: {exc}") from exc
        log.info("Bound global hotkey %s", self._combo)

    def stop(self) -> None:
        if self._hotkeys is not None:
            self._hotkeys.stop()
            self._hotkeys = None
