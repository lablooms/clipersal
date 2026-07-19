"""Hotkey combo widget. Shared by settings_window_qt.py and first_run_qt.py,
so there's exactly one implementation.

Capturing works via pynput: a `keyboard.Listener` runs on its own thread
while "Record" is active, and its on_press/on_release callbacks never touch
Qt widgets directly -- key events are delivered via a real Qt signal
(`_ListenerBridge.pressed`/`.released`, connected with the automatic
cross-thread QueuedConnection), the same signal-bridge pattern toast_qt.py
establishes.

The pure key-mapping logic (_token_for_key/_format_combo) has no import-time
dependency on any GUI toolkit, and is covered directly by unit tests
(tests/test_hotkey_widget_qt.py).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

# pynput reports specific left/right variants (ctrl_l, ctrl_r, ...) on
# keypress, but the app's stored combo format (and its own DEFAULT_COMBO)
# uses the generic modifier name so either physical key works -- without
# this normalization, recording would produce e.g. "<ctrl_l>+<alt_l>+r",
# which only matches the left-hand keys.
_MODIFIER_ALIASES = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
}
_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd"}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")


def _token_for_key(key) -> str | None:
    """pynput Key/KeyCode -> a token matching pynput's own GlobalHotKeys
    string format (e.g. "ctrl", "alt", "r", "f5"). Returns None for a key
    that can't be represented as a plain token (rare -- unnamed media keys).
    """
    from pynput import keyboard

    if isinstance(key, keyboard.KeyCode):
        return key.char.lower() if key.char else None
    return _MODIFIER_ALIASES.get(key.name, key.name)


def _format_combo(tokens: set[str]) -> str:
    mods = [f"<{m}>" for m in _MODIFIER_ORDER if m in tokens]
    others = sorted(t for t in tokens if t not in _MODIFIER_NAMES)
    return "+".join(mods + others)


class _ListenerBridge(QObject):
    """Constructed on the GUI thread; pynput's Listener callbacks (running
    on pynput's own thread) call `.emit()` on these directly -- automatically
    delivered to slots on the GUI thread via Qt.QueuedConnection.
    """

    pressed = Signal(str)
    released = Signal(str)


class HotkeyField(QWidget):
    """An always-editable combo entry (manual mode) + a Record button
    (press-the-combo mode). `.combo()` returns the current combo regardless
    of which mode set it -- the direct equivalent of the CTk version's
    `combo_var.get()`.
    """

    def __init__(self, initial_combo: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initial_combo = initial_combo
        self._listener = None
        self._recording = False
        self._held: set[str] = set()
        self._captured: set[str] = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.entry = QLineEdit(initial_combo, self)
        layout.addWidget(self.entry, 1)

        self.record_button = QPushButton("Record", self)
        self.record_button.setObjectName("recordButton")
        self.record_button.setFixedWidth(90)
        self.record_button.setProperty("recording", False)
        self.record_button.clicked.connect(self._on_record_clicked)
        layout.addWidget(self.record_button)

        self._bridge = _ListenerBridge()
        self._bridge.pressed.connect(self._on_key_press)
        self._bridge.released.connect(self._on_key_release)

    def combo(self) -> str:
        return self.entry.text()

    def is_recording(self) -> bool:
        return self._recording

    def cancel_recording(self) -> None:
        """Aborts an in-progress recording from outside the widget -- same as
        clicking the Cancel button. Settings/wizard Save paths call this when
        they're invoked mid-record, so the "Press keys..." placeholder (or a
        half-captured combo) is never read as the combo to persist.
        """
        if self._recording:
            self._finish_recording(None)

    def _restyle_record_button(self, recording: bool) -> None:
        # setProperty() alone doesn't force Qt to re-evaluate QSS selectors
        # keyed on it (e.g. #recordButton[recording="true"]) -- unpolish()
        # then polish() is the standard way to force that re-evaluation.
        self.record_button.setText("Cancel" if recording else "Record")
        self.record_button.setProperty("recording", recording)
        style = self.record_button.style()
        style.unpolish(self.record_button)
        style.polish(self.record_button)

    def _stop_listener(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _finish_recording(self, final_combo: str | None) -> None:
        self._recording = False
        self._stop_listener()
        self.entry.setEnabled(True)
        self._restyle_record_button(False)
        if final_combo is not None:
            self.entry.setText(final_combo)
        elif not self.entry.text().strip() or self.entry.text() == "Press keys...":
            self.entry.setText(self._initial_combo)

    def _on_key_press(self, token: str) -> None:
        if not self._recording:
            return
        self._held.add(token)
        self._captured |= self._held
        self.entry.setText(_format_combo(self._captured) or "Press keys...")

    def _on_key_release(self, token: str) -> None:
        if not self._recording:
            return
        self._held.discard(token)
        if not self._held and self._captured:
            non_modifiers = [t for t in self._captured if t not in _MODIFIER_NAMES]
            if non_modifiers:
                self._finish_recording(_format_combo(self._captured))
                return
            # only modifiers were tapped and released -- keep listening for
            # a fuller combo instead of finalizing
            self._captured = set()
            self.entry.setText("Press keys...")

    def _on_record_clicked(self) -> None:
        if self._recording:
            self._finish_recording(None)  # Record clicked again mid-capture -- cancel
            return

        from pynput import keyboard

        self._recording = True
        self._held = set()
        self._captured = set()
        self.entry.setEnabled(False)
        self.entry.setText("Press keys...")
        self._restyle_record_button(True)

        def on_press(key) -> None:
            token = _token_for_key(key)
            if token is not None:
                self._bridge.pressed.emit(token)

        def on_release(key) -> None:
            token = _token_for_key(key)
            if token is not None:
                self._bridge.released.emit(token)

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()
