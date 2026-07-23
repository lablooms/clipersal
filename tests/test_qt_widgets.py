import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QSlider, QSpinBox

from clipersal.qt_widgets import (
    SegmentedControl,
    StepperDoubleSpinBox,
    StepperSpinBox,
    ToggleSwitch,
    WheelGuard,
)


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


# ---- StepperSpinBox / StepperDoubleSpinBox ----------------------------------
#
# The themed re-skin of the native spinboxes: a QLineEdit plus two stacked
# ▲/▼ step buttons. These tests pin the Qt-spinbox API subset the call sites
# use (settings' quick-save seconds, the GIF dialog's fields) and the commit
# semantics (typed text clamps/reverts on editingFinished; valueChanged only
# on an actual change, so the Settings rollback's blocked setValue can't
# re-schedule the autosave it undoes).


def _int_spin(minimum: int = 5, maximum: int = 300, value: int = 30) -> StepperSpinBox:
    spin = StepperSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    return spin


def test_stepper_spin_set_value_clamps_into_the_range() -> None:
    spin = _int_spin()
    assert spin.value() == 30
    spin.setValue(999)
    assert spin.value() == 300
    spin.setValue(1)
    assert spin.value() == 5


def test_stepper_spin_range_and_suffix_accessors() -> None:
    spin = StepperSpinBox()
    spin.setRange(5, 300)
    spin.setSuffix(" s")
    assert spin.minimum() == 5
    assert spin.maximum() == 300
    assert spin.suffix() == " s"
    spin.setMinimum(10)
    spin.setMaximum(200)
    assert (spin.minimum(), spin.maximum()) == (10, 200)


def test_stepper_spin_text_shows_the_formatted_value_and_suffix() -> None:
    spin = _int_spin(value=45)
    spin.setSuffix(" s")
    assert spin._edit.text() == "45 s"


def test_stepper_spin_set_value_emits_value_changed_only_on_an_actual_change() -> None:
    spin = _int_spin()
    received = []
    spin.valueChanged.connect(received.append)
    spin.setValue(30)  # already the value -- silent (the rollback path relies on this)
    assert received == []
    spin.setValue(60)
    assert received == [60]


def test_stepper_spin_set_range_pulls_an_out_of_range_value_back_inside() -> None:
    spin = _int_spin(minimum=0, maximum=100, value=50)
    received = []
    spin.valueChanged.connect(received.append)
    spin.setRange(0, 40)
    assert spin.value() == 40
    assert received == [40]


def test_stepper_spin_step_buttons_step_and_emit() -> None:
    spin = _int_spin()
    received = []
    spin.valueChanged.connect(received.append)
    spin._up_button.click()
    assert spin.value() == 31
    spin._down_button.click()
    spin._down_button.click()
    assert spin.value() == 29
    assert received == [31, 30, 29]
    # The display tracks every step.
    assert spin._edit.text() == "29"


def test_stepper_spin_step_buttons_clamp_at_the_range_edges() -> None:
    spin = _int_spin(minimum=5, maximum=6, value=6)
    received = []
    spin.valueChanged.connect(received.append)
    spin._up_button.click()  # already at max -- no change, no signal
    assert spin.value() == 6
    spin._down_button.click()
    spin._down_button.click()  # clamps at min
    assert spin.value() == 5
    assert received == [5]


def test_stepper_spin_single_step_override() -> None:
    spin = StepperSpinBox()
    spin.setRange(200, 1920)
    spin.setSingleStep(20)
    spin.setValue(480)
    spin._up_button.click()
    assert spin.value() == 500
    spin._down_button.click()
    assert spin.value() == 480


def test_stepper_spin_typed_value_commits_on_editing_finished() -> None:
    spin = _int_spin()
    values, finished = [], []
    spin.valueChanged.connect(values.append)
    spin.editingFinished.connect(lambda: finished.append(True))
    spin._edit.setText("72")
    spin._edit.editingFinished.emit()
    assert spin.value() == 72
    assert values == [72]
    assert finished == [True]


def test_stepper_spin_out_of_range_typed_text_clamps_on_commit() -> None:
    # A programmatic setText bypasses the bounded validator (interactive
    # typing can't produce this) -- the commit still clamps.
    spin = _int_spin()
    spin._edit.setText("9999")
    spin._edit.editingFinished.emit()
    assert spin.value() == 300
    assert spin._edit.text() == "300"
    spin._edit.setText("1")
    spin._edit.editingFinished.emit()
    assert spin.value() == 5


def test_stepper_spin_unparseable_text_reverts_to_the_current_value() -> None:
    spin = _int_spin(value=45)
    spin.setSuffix(" s")
    spin._edit.setText("abc")
    spin._edit.editingFinished.emit()
    assert spin.value() == 45
    assert spin._edit.text() == "45 s"  # reverted AND reformatted


def test_stepper_spin_suffix_is_stripped_when_parsing_a_commit() -> None:
    spin = _int_spin(value=30)
    spin.setSuffix(" s")
    spin._edit.setText("72 s")
    spin._edit.editingFinished.emit()
    assert spin.value() == 72
    assert spin._edit.text() == "72 s"
    # A bare number commits too, and picks the suffix up in the reformat.
    spin._edit.setText("45")
    spin._edit.editingFinished.emit()
    assert spin.value() == 45
    assert spin._edit.text() == "45 s"


def test_stepper_spin_plain_focus_out_emits_editing_finished_without_value_changed() -> None:
    spin = _int_spin()
    values, finished = [], []
    spin.valueChanged.connect(values.append)
    spin.editingFinished.connect(lambda: finished.append(True))
    spin._edit.editingFinished.emit()  # no edit happened
    assert values == []
    assert finished == [True]


def test_stepper_spin_arrow_keys_step() -> None:
    spin = _int_spin()
    up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(spin._edit, up)
    assert spin.value() == 31
    QApplication.sendEvent(spin._edit, down)
    QApplication.sendEvent(spin._edit, down)
    assert spin.value() == 29


def test_stepper_spin_wheel_is_a_no_op_not_a_step(qapp) -> None:
    # The widget is NOT a QAbstractSpinBox, so the app-wide WheelGuard leaves
    # its wheel events alone -- they must bubble up to scroll the page, and
    # the line edit itself must never step on them.
    spin = _int_spin()
    guard = WheelGuard()
    assert guard.eventFilter(spin._edit, _wheel_event()) is False  # page scroll stays possible
    QApplication.sendEvent(spin._edit, _wheel_event())
    assert spin.value() == 30


def test_stepper_spin_disabled_state_propagates_to_the_children() -> None:
    spin = _int_spin()
    spin.setEnabled(False)
    assert spin._edit.isEnabled() is False
    assert spin._up_button.isEnabled() is False
    assert spin._down_button.isEnabled() is False
    spin.setEnabled(True)
    assert spin._edit.isEnabled() is True
    assert spin._up_button.isEnabled() is True


def _double_spin(minimum: float = 0.5, maximum: float = 30.0, value: float = 3.0) -> StepperDoubleSpinBox:
    spin = StepperDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(1)
    spin.setValue(value)
    return spin


def test_stepper_double_formats_with_decimals_and_suffix() -> None:
    spin = _double_spin()
    spin.setSuffix(" s")
    assert spin.value() == 3.0
    assert spin._edit.text() == "3.0 s"


def test_stepper_double_default_step_is_a_half() -> None:
    spin = _double_spin()
    received = []
    spin.valueChanged.connect(received.append)
    spin._up_button.click()
    assert spin.value() == 3.5
    spin._down_button.click()
    assert spin.value() == 3.0
    assert received == [3.5, 3.0]


def test_stepper_double_typed_value_commits_clamped_and_reformatted() -> None:
    spin = _double_spin()
    spin.setSuffix(" s")
    spin._edit.setText("99.9")
    spin._edit.editingFinished.emit()
    assert spin.value() == 30.0
    assert spin._edit.text() == "30.0 s"
    spin._edit.setText("7.25 s")
    spin._edit.editingFinished.emit()
    assert spin.value() == 7.2 or spin.value() == 7.3  # rounded to the display precision
    assert spin._edit.text().endswith(" s")


def test_stepper_double_unparseable_and_non_finite_text_reverts() -> None:
    spin = _double_spin(value=1.5)
    for bad_text in ("abc", "nan", "inf", ""):
        spin._edit.setText(bad_text)
        spin._edit.editingFinished.emit()
        assert spin.value() == 1.5
        assert spin._edit.text() == "1.5"


def test_stepper_double_arrow_keys_step_by_the_half_step() -> None:
    spin = _double_spin(value=1.0)
    up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(spin._edit, up)
    assert spin.value() == 1.5
