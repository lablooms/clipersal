import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal.qt_widgets import SegmentedControl, ToggleSwitch


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
