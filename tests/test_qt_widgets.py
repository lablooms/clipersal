import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QSlider, QSpinBox

from clipersal.qt_widgets import SegmentedControl, ToggleSwitch, WheelGuard


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_toggle_switch_starts_at_off_position_by_default() -> None:
    switch = ToggleSwitch()
    assert switch.isChecked() is False
    assert switch._knob_position == switch._MARGIN


def test_toggle_switch_starts_at_on_position_when_constructed_checked() -> None:
    switch = ToggleSwitch(checked=True)
    assert switch.isChecked() is True
    assert switch._knob_position == switch._end_position()
    assert switch._knob_position > switch._MARGIN


def test_toggle_switch_end_position_tracks_checked_state() -> None:
    switch = ToggleSwitch(checked=False)
    off_position = switch._end_position()
    switch.setChecked(True)
    on_position = switch._end_position()
    assert on_position > off_position


def test_toggle_switch_click_toggles_checked_state() -> None:
    switch = ToggleSwitch(checked=False)
    switch.click()
    assert switch.isChecked() is True
    switch.click()
    assert switch.isChecked() is False


def test_segmented_control_defaults_to_first_value() -> None:
    control = SegmentedControl(["Desktop", "Monitor", "Window"])
    assert control.current() == "Desktop"
    assert control._buttons["Desktop"].isChecked() is True


def test_segmented_control_click_changes_current_and_emits_signal() -> None:
    control = SegmentedControl(["Performance", "Balanced", "Quality", "Custom"])
    received = []
    control.currentTextChanged.connect(received.append)

    control._buttons["Quality"].click()

    assert control.current() == "Quality"
    assert received == ["Quality"]


def test_segmented_control_clicking_same_value_does_not_re_emit() -> None:
    control = SegmentedControl(["A", "B"])
    received = []
    control.currentTextChanged.connect(received.append)

    control._buttons["A"].click()  # already current -- no real change

    assert received == []


def test_segmented_control_set_current_updates_without_emitting_signal() -> None:
    # Mirrors CTkSegmentedButton semantics: setting the value programmatically
    # (e.g. loading a saved Config) shouldn't fire the same callback a user
    # click would.
    control = SegmentedControl(["Desktop", "Monitor", "Window"])
    received = []
    control.currentTextChanged.connect(received.append)

    control.setCurrent("Window")

    assert control.current() == "Window"
    assert control._buttons["Window"].isChecked() is True
    assert received == []


def test_segmented_control_only_one_button_checked_at_a_time() -> None:
    control = SegmentedControl(["A", "B", "C"])
    control._buttons["B"].click()

    checked = [value for value, button in control._buttons.items() if button.isChecked()]
    assert checked == ["B"]


def test_segmented_control_set_current_ignores_unknown_value() -> None:
    control = SegmentedControl(["A", "B"])
    control.setCurrent("nonexistent")
    assert control.current() == "A"  # unchanged


# ---- WheelGuard -------------------------------------------------------------
#
# Scrolling a page must never silently rewrite the value widget the cursor
# happens to cross: combos/spinboxes/sliders get their wheel events consumed,
# focused or not (a clicked combo keeps focus, so a focus exception would
# still corrupt it later), and events delivered to a guarded widget's child
# (QSpinBox's inner line edit) are caught via the ancestry walk.


def _wheel_event() -> QWheelEvent:
    # angleDelta (0, 120) = one notch up. Direction is irrelevant to these
    # tests -- what matters is whether the widget's value moves AT ALL.
    return QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


class _FocusComboBox(QComboBox):
    """Deterministically "focused": real keyboard focus is flaky under the
    offscreen platform, so the guard's hasFocus() check is stubbed instead."""

    def hasFocus(self) -> bool:
        return True


class _FocusSlider(QSlider):
    def hasFocus(self) -> bool:
        return True


def test_wheel_guard_consumes_wheel_for_guarded_widgets_and_lets_the_rest_through() -> None:
    guard = WheelGuard()

    assert guard.eventFilter(QComboBox(), _wheel_event()) is True
    assert guard.eventFilter(QSpinBox(), _wheel_event()) is True
    assert guard.eventFilter(QSlider(), _wheel_event()) is True

    # Focus doesn't matter -- a once-clicked combo keeps focus, and the wheel
    # still must not change it...
    assert guard.eventFilter(_FocusComboBox(), _wheel_event()) is True
    # ...wheel delivered to a guarded widget's CHILD (a spinbox's inner line
    # edit -- the real delivery path) is caught via the ancestry walk...
    spin = QSpinBox()
    assert guard.eventFilter(spin.lineEdit(), _wheel_event()) is True
    # ...non-guarded widget classes are never touched...
    assert guard.eventFilter(QPushButton("x"), _wheel_event()) is False
    # ...and non-wheel events pass through even on guarded classes.
    from PySide6.QtCore import QEvent

    assert guard.eventFilter(QComboBox(), QEvent(QEvent.Type.FocusIn)) is False


@pytest.fixture()
def installed_guard(qapp):
    guard = WheelGuard(qapp)
    qapp.installEventFilter(guard)
    yield guard
    qapp.removeEventFilter(guard)


def test_wheel_event_on_unfocused_combo_does_not_change_index(qapp, installed_guard) -> None:
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(1)
    QApplication.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 1


def test_wheel_event_on_focused_combo_does_not_change_index_either(qapp, installed_guard) -> None:
    combo = _FocusComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(1)
    QApplication.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 1  # focused stays guarded -- see WheelGuard


def test_wheel_event_on_spinbox_line_edit_does_not_change_value(qapp, installed_guard) -> None:
    # The real-app delivery path: Qt sends wheel events for a spinbox to its
    # inner QLineEdit, not to the spinbox itself.
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    QApplication.sendEvent(spin.lineEdit(), _wheel_event())
    assert spin.value() == 50


def test_wheel_event_on_unfocused_slider_does_not_change_value(qapp, installed_guard) -> None:
    slider = QSlider()
    slider.setRange(0, 100)
    slider.setValue(50)
    QApplication.sendEvent(slider, _wheel_event())
    assert slider.value() == 50


def test_wheel_event_on_focused_slider_does_not_change_value_either(qapp, installed_guard) -> None:
    slider = _FocusSlider()
    slider.setRange(0, 100)
    slider.setValue(50)
    QApplication.sendEvent(slider, _wheel_event())
    assert slider.value() == 50


# ---- ElidedLabel ---------------------------------------------------------------


def test_elided_label_preserves_the_full_string_while_displaying_an_elided_copy() -> None:
    from PySide6.QtWidgets import QLabel

    from clipersal.qt_widgets import ElidedLabel

    full = "a-very-long-text-that-cannot-possibly-fit-in-sixty-pixels" * 2
    label = ElidedLabel(full)
    label.resize(60, 20)
    label.show()
    assert label.text() == full  # callers always see the FULL string
    displayed = QLabel.text(label)  # base-class read: the on-screen copy
    assert len(displayed) < len(full)
    assert "…" in displayed
    label.close()


def test_elided_label_shows_everything_when_wide_enough() -> None:
    from PySide6.QtWidgets import QLabel

    from clipersal.qt_widgets import ElidedLabel

    label = ElidedLabel("short")
    label.resize(600, 20)
    label.show()
    assert QLabel.text(label) == "short"
    label.close()


def test_elided_label_reelides_on_resize_and_set_text() -> None:
    from PySide6.QtWidgets import QLabel

    from clipersal.qt_widgets import ElidedLabel

    label = ElidedLabel("x" * 40)
    label.resize(600, 20)
    label.show()
    assert QLabel.text(label) == "x" * 40
    label.resize(50, 20)
    assert "…" in QLabel.text(label)  # narrowed -> elided
    label.setText("y" * 40)
    assert label.text() == "y" * 40
    assert "…" in QLabel.text(label)
    label.resize(600, 20)
    assert QLabel.text(label) == "y" * 40  # widened -> full again
    label.close()
