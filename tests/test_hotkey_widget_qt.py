import os

import pytest
from pynput.keyboard import Key, KeyCode

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal.hotkey_widget_qt import HotkeyField, _format_combo, _token_for_key


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeListener:
    """Stands in for pynput.keyboard.Listener -- never actually hooks global
    keyboard input. pynput.keyboard.Controller sends genuinely system-wide
    input, so tests never trigger a real Listener either; the Record/Cancel
    state machine is exercised by invoking the button directly instead.
    """

    instances: list["_FakeListener"] = []

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False
        _FakeListener.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def fake_pynput_listener(monkeypatch):
    _FakeListener.instances.clear()
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    yield


# ---- pure key-mapping logic (identical to hotkey_widget.py's own tests) ---


def test_token_for_char_key_lowercases() -> None:
    assert _token_for_key(KeyCode.from_char("R")) == "r"


def test_token_for_char_key_none_char_returns_none() -> None:
    assert _token_for_key(KeyCode(char=None)) is None


def test_token_for_named_key_passes_through() -> None:
    assert _token_for_key(Key.f5) == "f5"


def test_token_for_left_right_modifier_variants_normalize() -> None:
    assert _token_for_key(Key.ctrl_l) == "ctrl"
    assert _token_for_key(Key.ctrl_r) == "ctrl"
    assert _token_for_key(Key.alt_l) == "alt"
    assert _token_for_key(Key.alt_gr) == "alt"
    assert _token_for_key(Key.shift_l) == "shift"
    assert _token_for_key(Key.cmd_r) == "cmd"


def test_format_combo_orders_modifiers_before_other_keys() -> None:
    assert _format_combo({"r", "ctrl", "alt"}) == "<ctrl>+<alt>+r"


def test_format_combo_modifier_order_is_stable_regardless_of_set_order() -> None:
    assert _format_combo({"cmd", "shift", "alt", "ctrl", "k"}) == "<ctrl>+<alt>+<shift>+<cmd>+k"


def test_format_combo_multiple_non_modifier_keys_sorted() -> None:
    assert _format_combo({"ctrl", "z", "a"}) == "<ctrl>+a+z"


def test_format_combo_empty_set() -> None:
    assert _format_combo(set()) == ""


def test_format_combo_modifiers_only() -> None:
    assert _format_combo({"ctrl", "alt"}) == "<ctrl>+<alt>"


# ---- widget shell: Record/Cancel state machine, exercised via .click() ----


def test_initial_state_shows_initial_combo_and_idle_button() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    assert field.combo() == "<ctrl>+<alt>+r"
    assert field.is_recording() is False
    assert field.record_button.text() == "Record"
    assert field.entry.isEnabled() is True


def test_clicking_record_enters_recording_state_without_a_real_listener() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()

    assert field.is_recording() is True
    assert field.entry.isEnabled() is False
    assert field.entry.text() == "Press keys..."
    assert field.record_button.text() == "Cancel"
    assert len(_FakeListener.instances) == 1
    assert _FakeListener.instances[0].started is True


def test_clicking_record_again_cancels_and_restores_initial_combo() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()  # start recording
    field.record_button.click()  # cancel

    assert field.is_recording() is False
    assert field.entry.isEnabled() is True
    assert field.entry.text() == "<ctrl>+<alt>+r"
    assert field.record_button.text() == "Record"
    assert _FakeListener.instances[0].stopped is True


def test_pressing_and_releasing_full_combo_finalizes_on_last_release() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()

    field._on_key_press("ctrl")
    field._on_key_press("alt")
    field._on_key_press("r")
    assert field.entry.text() == "<ctrl>+<alt>+r"  # live-updates while held

    field._on_key_release("r")
    assert field.is_recording() is True  # ctrl/alt still held -- not finalized yet
    field._on_key_release("alt")
    assert field.is_recording() is True
    field._on_key_release("ctrl")

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"
    assert field.entry.isEnabled() is True
    assert _FakeListener.instances[0].stopped is True


def test_tapping_only_modifiers_keeps_listening_instead_of_finalizing() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()

    field._on_key_press("ctrl")
    field._on_key_release("ctrl")  # only a modifier was tapped -- shouldn't finalize

    assert field.is_recording() is True
    assert field.entry.text() == "Press keys..."

    # a real combo afterward still finalizes normally
    field._on_key_press("shift")
    field._on_key_press("k")
    field._on_key_release("k")
    field._on_key_release("shift")

    assert field.is_recording() is False
    assert field.combo() == "<shift>+k"


def test_key_events_ignored_when_not_recording() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field._on_key_press("z")  # never clicked Record
    assert field.combo() == "<ctrl>+<alt>+r"  # unchanged


def test_cancel_recording_aborts_and_restores_initial_combo() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()  # start recording
    assert field.is_recording() is True

    field.cancel_recording()

    # Same outcome as clicking Cancel: idle state, placeholder dropped, the
    # pre-record combo is what .combo() returns.
    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"
    assert field.entry.isEnabled() is True
    assert field.record_button.text() == "Record"
    assert _FakeListener.instances[0].stopped is True


def test_cancel_recording_mid_capture_drops_partial_capture() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()
    # A lone modifier tap doesn't finalize -- the entry goes back to the
    # "Press keys..." placeholder with the recorder still listening.
    field._on_key_press("ctrl")
    field._on_key_release("ctrl")

    field.cancel_recording()

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"


def test_cancel_with_a_key_still_held_restores_the_pre_record_combo() -> None:
    # The partial-capture leak: press AND HOLD a key, so the entry is showing
    # the half-captured "<ctrl>", then cancel. The half-captured modifier-only
    # text is not a recordable combo -- left in the entry, a host's autosave
    # would persist a bare "<ctrl>" that fires on every ctrl press. A cancel
    # must always restore the pre-record combo.
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()
    field._on_key_press("ctrl")  # held -- the entry now shows "<ctrl>"
    assert field.entry.text() == "<ctrl>"

    field.record_button.click()  # cancel with the key still held

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"


def test_cancel_recording_with_a_key_still_held_restores_the_pre_record_combo() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.record_button.click()
    field._on_key_press("ctrl")
    field._on_key_press("alt")
    assert field.entry.text() == "<ctrl>+<alt>"

    field.cancel_recording()

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"


def test_hide_mid_capture_with_a_key_held_restores_and_stops() -> None:
    # Same cancel path as a tab switch away from Settings mid-record: the
    # hide must both tear the listener down AND drop the partial capture.
    field = HotkeyField("<ctrl>+<alt>+r")
    field.show()
    field.record_button.click()
    field._on_key_press("shift")

    field.hide()

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"


def test_cancel_recording_is_a_noop_when_idle() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.cancel_recording()
    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"
    assert _FakeListener.instances == []


def test_hide_event_cancels_an_in_progress_recording() -> None:
    # Every host hides this widget on its close path (the first-run wizard,
    # the main window hiding to the tray, a tab switch away from Settings)
    # and Qt propagates the hide event to children -- hiding must tear down
    # the OS-wide listener even if the host's own close path forgets to.
    field = HotkeyField("<ctrl>+<alt>+r")
    field.show()
    field.record_button.click()  # hideEvent only fires on a visible->hidden transition
    assert field.is_recording() is True

    field.hide()

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"
    assert field.record_button.text() == "Record"
    assert _FakeListener.instances[0].stopped is True


def test_hide_event_while_idle_is_harmless() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.show()
    field.hide()
    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+<alt>+r"
    assert _FakeListener.instances == []


# ---- recording_finished (the autosave hook) ------------------------------------


def test_recording_finished_fires_on_accept_and_cancel() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    emissions = []
    field.recording_finished.connect(lambda: emissions.append(1))

    field.record_button.click()
    field._on_key_press("ctrl")
    field._on_key_press("s")
    field._on_key_release("s")
    field._on_key_release("ctrl")
    assert len(emissions) == 1  # accepted with <ctrl>+s

    field.record_button.click()
    field.record_button.click()  # cancel
    assert len(emissions) == 2  # cancel fires too -- hosts compare the combo
    # ...and the cancel restored the PRE-RECORD text, not the constructor's.
    assert field.combo() == "<ctrl>+s"


def test_cancel_restores_a_manually_typed_combo_not_the_initial_one() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    field.entry.setText("<ctrl>+m")
    field.record_button.click()
    field.cancel_recording()
    assert field.combo() == "<ctrl>+m"


def test_recording_finished_does_not_fire_when_never_recording() -> None:
    field = HotkeyField("<ctrl>+<alt>+r")
    emissions = []
    field.recording_finished.connect(lambda: emissions.append(1))
    field.cancel_recording()  # no-op while idle
    assert emissions == []
