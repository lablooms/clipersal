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


def is_valid_combo(combo: str) -> bool:
    """Whether `combo` parses as a pynput hotkey string -- the same format
    HotkeyListener binds. The Settings field and first-run wizard accept
    free-typed combos, so this is the gate that keeps an unparseable string
    (the recorder's "Press keys..." placeholder, or plain garbage) from being
    persisted: a bad combo that reaches GlobalHotKeys raises there instead,
    leaving no hotkey bound -- not just now, but on every future launch, since
    the persisted value is rebound at startup.
    """
    if not combo or not combo.strip():
        return False
    try:
        from pynput import keyboard
    except ImportError:
        # Without pynput there's no parser to validate against -- and no
        # hotkey to bind either, so the combo is inert either way. Don't
        # block saving settings over it.
        return True
    try:
        return bool(keyboard.HotKey.parse(combo))
    except ValueError:
        return False


class HotkeyListener:
    def __init__(self, combo: str, callback: Callable[[], None]):
        self._mapping: dict[str, Callable[[], None]] = {combo: callback}
        self._hotkeys = None

    @classmethod
    def from_mapping(cls, mapping: dict[str, Callable[[], None]]) -> HotkeyListener:
        """Bind several combos at once (the main save combo plus the
        quick-save / screenshot combos -- see cli.py's rebind_hotkey). Empty
        combos are dropped here as well as in cli.py's validation, so a
        disabled ("") binding can never reach pynput's parser.
        """
        listener = cls.__new__(cls)
        listener._mapping = {combo: cb for combo, cb in mapping.items() if combo and combo.strip()}
        listener._hotkeys = None
        return listener

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise HotkeyUnsupportedError(
                "pynput is not installed; global hotkey binding is unavailable"
            ) from exc

        try:
            self._hotkeys = keyboard.GlobalHotKeys(dict(self._mapping))
            self._hotkeys.start()
        except Exception as exc:
            raise HotkeyUnsupportedError(
                f"Could not bind global hotkeys {sorted(self._mapping)}: {exc}"
            ) from exc
        log.info("Bound global hotkeys %s", ", ".join(sorted(self._mapping)))

    def stop(self) -> None:
        if self._hotkeys is not None:
            self._hotkeys.stop()
            self._hotkeys = None
