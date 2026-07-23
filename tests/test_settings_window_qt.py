import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox, QPushButton

from clipersal import __version__, settings_window_qt
from clipersal.config import Config, _default_clips_dir
from clipersal.ffmpeg_utils import AudioSource
from clipersal.hotkey import DEFAULT_COMBO
from clipersal.monitors import MonitorInfo
from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.settings_window_qt import SettingsFrame, bitrate_string_to_mbps, default_settings_payload
from clipersal.window_capture import WindowInfo


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def single_monitor_no_mic(monkeypatch):
    # Default environment for most tests: one monitor (no Monitor mode
    # offered), no microphone detected (no mic picker offered) -- matches
    # this dev machine and keeps most tests focused on what they're testing.
    # No system-audio loopback either, so the desktop volume slider starts
    # in its disabled state.
    monkeypatch.setattr(settings_window_qt.monitors, "list_monitors", lambda os_: [])
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: [])
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "find_audio_source", lambda ffmpeg_path, os_: None)
    monkeypatch.setattr(settings_window_qt.window_capture, "list_windows", lambda os_: [])
    # Hermetic launch-on-startup probe: the real is_enabled reads the
    # registry / ~/.config/autostart, which would leak the machine's actual
    # registration state into the toggle tests.
    monkeypatch.setattr(settings_window_qt.autostart, "is_enabled", lambda os_: False)
    yield


def _make_config(tmp_path: Path, **overrides) -> Config:
    kwargs = dict(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    kwargs.update(overrides)
    return Config(**kwargs)


def _build(tmp_path: Path, on_apply=None, on_update_found=None, **config_overrides) -> SettingsFrame:
    config = _make_config(tmp_path, **config_overrides)
    return SettingsFrame(
        config, ipc_port=51525, save_events=None, current_encoder="libx264",
        on_apply=on_apply or (lambda values: None), ffmpeg_path="ffmpeg",
        on_update_found=on_update_found,
    )


def _apply(frame: SettingsFrame) -> None:
    """Fire the autosave debounce immediately. Payload/validation tests don't
    care about the 500 ms wait, only about what the fire produces."""
    frame._apply_timer.stop()
    frame._apply_now()


def _flush_autosave(frame: SettingsFrame) -> None:
    """Pretend the debounce elapsed: demand that a change actually scheduled
    an apply, then fire it."""
    assert frame._apply_timer.isActive(), "expected a scheduled autosave"
    frame._apply_timer.stop()
    frame._apply_now()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Wait for an async (worker-thread) condition, e.g. the check-now
    worker's answer arriving via the queued check_now_responded signal.
    Pumps sendPostedEvents(), NOT processEvents() -- see test_main_window_qt.py's
    _wait_for for why."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.sendPostedEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---- pure bitrate parsing (identical to settings_window.py's own tests) ---


def test_parses_plain_megabit_suffix() -> None:
    assert bitrate_string_to_mbps("8M") == 8


def test_parses_lowercase_megabit_suffix() -> None:
    assert bitrate_string_to_mbps("8m") == 8


def test_parses_kilobit_suffix() -> None:
    assert bitrate_string_to_mbps("4000k") == 4


def test_parses_bare_number_as_megabits() -> None:
    assert bitrate_string_to_mbps("6") == 6


def test_clamps_to_slider_minimum() -> None:
    assert bitrate_string_to_mbps("0.5M") == 2


def test_clamps_to_slider_maximum() -> None:
    assert bitrate_string_to_mbps("99M") == 20


def test_falls_back_to_midpoint_for_unparseable_string() -> None:
    assert bitrate_string_to_mbps("not-a-bitrate") == 11


def test_falls_back_to_midpoint_for_empty_string() -> None:
    assert bitrate_string_to_mbps("") == 11


# ---- initial state reflects config ----------------------------------------


def test_initial_state_reflects_config(tmp_path: Path) -> None:
    frame = _build(
        tmp_path, buffer_seconds=90, hotkey_combo="<ctrl>+<shift>+s", filename_template="{datetime}-clip",
        clip_retention_days=14, check_for_updates=False,
    )
    assert frame.buffer_slider.value() == 90
    assert frame.hotkey_field.combo() == "<ctrl>+<shift>+s"
    assert frame.filename_template_edit.text() == "{datetime}-clip"
    assert frame.retention_slider.value() == 14
    assert frame.check_for_updates_switch.isChecked() is False


def test_monitor_picker_hidden_with_only_one_monitor(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert "Monitor" not in frame.target_control._buttons
    assert frame.monitor_combo is None


# ---- out-of-range config values clamp consistently ------------------------------


def test_out_of_range_config_values_show_the_same_clamped_number_everywhere(tmp_path: Path) -> None:
    # What's shown is what's saved: label and slider must agree on the SAME
    # clamped number -- not label=original while Qt clamps only the slider.
    frame = _build(tmp_path, buffer_seconds=600, desktop_volume=300, mic_volume=-10, clip_retention_days=120)
    assert frame.buffer_slider.value() == 300
    assert frame.buffer_value_label.text() == "300s"
    assert frame.desktop_volume_slider.value() == 200
    assert frame.desktop_volume_value_label.text() == "200%"
    assert frame.mic_volume_slider.value() == 0
    assert frame.mic_volume_value_label.text() == "0%"
    assert frame.retention_slider.value() == 90
    assert frame.retention_value_label.text() == "90d"


def test_save_persists_the_clamped_displayed_value(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, buffer_seconds=600, on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert captured["buffer_seconds"] == 300


def test_monitor_picker_shown_with_multiple_monitors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_window_qt.monitors, "list_monitors",
        lambda os_: [
            MonitorInfo(index=0, name="A", x=0, y=0, width=1920, height=1080, is_primary=True),
            MonitorInfo(index=1, name="B", x=1920, y=0, width=2560, height=1440, is_primary=False),
        ],
    )
    frame = _build(tmp_path, capture_mode="monitor", monitor_index=1)
    assert "Monitor" in frame.target_control._buttons
    assert frame.monitor_combo is not None
    assert frame.target_control.current() == "Monitor"
    assert frame.monitor_container.isHidden() is False


def test_mic_picker_hidden_when_no_devices_found(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert frame.mic_combo is None


def test_mic_picker_shown_when_devices_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A", "Mic B"])
    frame = _build(tmp_path, mic_device="Mic B")
    assert frame.mic_combo is not None
    assert frame.mic_combo.currentText() == "Mic B"


def test_mic_picker_defaults_to_none_when_configured_device_not_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A"])
    frame = _build(tmp_path, mic_device="Unplugged Mic")
    assert frame.mic_combo.currentText() == "None"


# ---- volume sliders -----------------------------------------------------------


def _fake_loopback(ffmpeg_path, os_):
    return AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")


def test_volume_sliders_have_percent_range_and_reflect_config(tmp_path: Path) -> None:
    frame = _build(tmp_path, desktop_volume=150, mic_volume=50)
    assert frame.desktop_volume_slider.minimum() == 0
    assert frame.desktop_volume_slider.maximum() == 200
    assert frame.mic_volume_slider.minimum() == 0
    assert frame.mic_volume_slider.maximum() == 200
    assert frame.desktop_volume_slider.value() == 150
    assert frame.mic_volume_slider.value() == 50
    assert frame.desktop_volume_value_label.text() == "150%"
    assert frame.mic_volume_value_label.text() == "50%"


def test_desktop_volume_slider_disabled_with_hint_when_no_loopback_detected(tmp_path: Path) -> None:
    frame = _build(tmp_path)  # fixture: find_audio_source -> None
    assert frame.desktop_volume_slider.isEnabled() is False


def test_desktop_volume_slider_enabled_when_loopback_detected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "find_audio_source", _fake_loopback)
    frame = _build(tmp_path)
    assert frame.desktop_volume_slider.isEnabled() is True


def test_mic_volume_slider_disabled_with_hint_when_no_microphone_detected(tmp_path: Path) -> None:
    frame = _build(tmp_path)  # fixture: no mics -> no picker
    assert frame.mic_volume_slider.isEnabled() is False
    assert "No microphone" in frame.mic_volume_hint.text()


def test_mic_volume_slider_follows_the_mic_picker_selection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A"])
    frame = _build(tmp_path)
    # "None" selected by default: nothing is mixed in, so the slider adjusts nothing.
    assert frame.mic_volume_slider.isEnabled() is False
    frame.mic_combo.setCurrentText("Mic A")
    assert frame.mic_volume_slider.isEnabled() is True
    frame.mic_combo.setCurrentText("None")
    assert frame.mic_volume_slider.isEnabled() is False


def test_save_payload_collects_both_volumes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A"])
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "find_audio_source", _fake_loopback)
    captured = {}
    frame = _build(tmp_path, mic_device="Mic A", on_apply=lambda values: captured.update(values) or None)
    frame.desktop_volume_slider.setValue(150)
    frame.mic_volume_slider.setValue(50)
    _apply(frame)
    assert captured["desktop_volume"] == 150
    assert captured["mic_volume"] == 50


# ---- capture target show/hide ----------------------------------------------


def test_capture_target_defaults_to_desktop_with_both_sub_panels_hidden(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert frame.target_control.current() == "Desktop"
    assert frame.monitor_container.isHidden() is True
    assert frame.window_container.isHidden() is True


def test_switching_to_window_shows_window_panel_only(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.target_control._buttons["Window"].click()
    assert frame.window_container.isHidden() is False
    assert frame.monitor_container.isHidden() is True


def test_switching_back_to_desktop_hides_both_panels(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.target_control._buttons["Window"].click()
    frame.target_control._buttons["Desktop"].click()
    assert frame.window_container.isHidden() is True
    assert frame.monitor_container.isHidden() is True


def test_refresh_windows_populates_combo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_window_qt.window_capture, "list_windows",
        lambda os_: [WindowInfo(handle="1", title="My App", x=0, y=0, width=800, height=600)],
    )
    frame = _build(tmp_path)
    frame._refresh_windows()
    assert frame.window_combo.currentText() == "My App"


def test_refresh_windows_keeps_current_selection_if_still_present(tmp_path: Path, monkeypatch) -> None:
    windows = [WindowInfo(handle="1", title="App A", x=0, y=0, width=800, height=600)]
    monkeypatch.setattr(settings_window_qt.window_capture, "list_windows", lambda os_: windows)
    frame = _build(tmp_path)
    frame._refresh_windows()
    windows.append(WindowInfo(handle="2", title="App B", x=0, y=0, width=800, height=600))
    frame.window_combo.setCurrentText("App A")
    frame._refresh_windows()
    assert frame.window_combo.currentText() == "App A"


# ---- quality preset show/hide ----------------------------------------------


def test_quality_preset_defaults_to_custom_with_slider_visible(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert frame.preset_control.current() == "Custom"
    assert frame.bitrate_container.isHidden() is False


def test_switching_to_balanced_hides_bitrate_slider(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.preset_control._buttons["Balanced"].click()
    assert frame.bitrate_container.isHidden() is True
    assert "8 Mbps" in frame.preset_desc_label.text()


def test_switching_back_to_custom_shows_bitrate_slider_again(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.preset_control._buttons["Balanced"].click()
    frame.preset_control._buttons["Custom"].click()
    assert frame.bitrate_container.isHidden() is False


# ---- encoder autodetect toggle ----------------------------------------------


def test_autodetect_enabled_by_default_disables_encoder_picker(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert frame.autodetect_switch.isChecked() is True
    assert frame.encoder_control.isEnabled() is False


def test_encoder_override_disables_autodetect_and_enables_picker(tmp_path: Path) -> None:
    frame = _build(tmp_path, encoder_override="libx264")
    assert frame.autodetect_switch.isChecked() is False
    assert frame.encoder_control.isEnabled() is True


def test_toggling_autodetect_off_enables_encoder_picker(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.autodetect_switch.click()
    assert frame.encoder_control.isEnabled() is True


# ---- browse clips folder ----------------------------------------------------


def test_browse_updates_clips_dir_when_a_folder_is_chosen(tmp_path: Path, monkeypatch) -> None:
    chosen_dir = str(tmp_path / "new_clips")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: chosen_dir))
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame._browse_clips_dir()
    assert frame.clips_dir_edit.text() == chosen_dir
    _flush_autosave(frame)  # a confirmed pick autosaves like any other edit
    assert captured["clips_dir"] == chosen_dir


def test_browse_leaves_clips_dir_unchanged_when_dialog_cancelled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    frame = _build(tmp_path)
    original = frame.clips_dir_edit.text()
    frame._browse_clips_dir()
    assert frame.clips_dir_edit.text() == original
    assert frame._apply_timer.isActive() is False  # a cancelled dialog schedules nothing


# ---- apply validation and payload --------------------------------------------


def test_apply_rejects_empty_hotkey(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(values) or None)
    frame.hotkey_field.entry.setText("")
    _apply(frame)
    assert applied == []  # a guard rejection never reaches on_apply
    assert frame.status_label.text() == "Hotkey cannot be empty."
    assert frame.status_label.property("state") == "error"
    # ...and the field keeps its text -- the user may still be mid-edit.
    assert frame.hotkey_field.combo() == ""


def test_apply_rejects_empty_clips_dir(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(values) or None)
    frame.clips_dir_edit.setReadOnly(False)
    frame.clips_dir_edit.setText("   ")
    _apply(frame)
    assert applied == []
    assert frame.status_label.text() == "Clips folder cannot be empty."


def test_apply_rejects_empty_filename_template(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(values) or None)
    frame.filename_template_edit.setText("")
    _apply(frame)
    assert applied == []
    assert frame.status_label.text() == "Filename template cannot be empty."
    assert frame.filename_template_edit.text() == ""  # left as-is, mid-edit


def test_apply_success_shows_confirmation_and_schedules_clear(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    _apply(frame)
    assert frame.status_label.text() == "Saved ✓"
    assert frame.status_label.property("state") == "success"
    assert frame._status_clear_timer.isActive()


def test_apply_shows_error_returned_by_on_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path, on_apply=lambda values: "could not restart capture")
    _apply(frame)
    assert frame.status_label.text() == "could not restart capture"
    assert frame.status_label.property("state") == "error"


# ---- autosave: footer, scheduling, debounce ------------------------------------


def test_footer_has_no_save_button_and_shows_the_autosave_hint(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    button_texts = [button.text() for button in frame.findChildren(QPushButton)]
    assert "Save" not in button_texts
    hints = [label for label in frame.findChildren(QLabel) if label.text() == "Changes save automatically."]
    assert len(hints) == 1
    assert hints[0].objectName() == "hint"


def test_construction_never_applies(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(values) or None)
    assert applied == []
    assert frame._apply_timer.isActive() is False


def test_rapid_changes_across_fields_coalesce_into_one_debounced_apply(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)

    frame.framerate_combo.setCurrentIndex(frame.framerate_combo.findData(60))
    frame.save_sound_switch.click()
    frame.quick_save_seconds_1_spin.setValue(45)

    assert applied == []  # still debouncing
    _flush_autosave(frame)

    assert len(applied) == 1  # three edits, ONE apply, full merged payload
    payload = applied[0]
    assert payload["framerate"] == 60
    assert payload["save_sound_enabled"] is True
    assert payload["quick_save_seconds_1"] == 45
    assert set(payload) == set(default_settings_payload())


def test_combo_change_schedules_an_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.resolution_scale_combo.setCurrentIndex(frame.resolution_scale_combo.findData("720p"))
    assert frame._apply_timer.isActive() is True


def test_segmented_control_change_schedules_an_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.target_control._buttons["Window"].click()
    assert frame._apply_timer.isActive() is True


def test_toggle_switch_change_schedules_an_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.check_for_updates_switch.click()
    assert frame._apply_timer.isActive() is True


def test_slider_schedules_on_release_not_mid_drag(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.buffer_slider.setValue(90)  # valueChanged fires -- the mid-drag signal
    assert frame._apply_timer.isActive() is False
    frame.buffer_slider.sliderReleased.emit()
    assert frame._apply_timer.isActive() is True


def test_spinbox_change_schedules_an_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.quick_save_seconds_2_spin.setValue(120)
    assert frame._apply_timer.isActive() is True


def test_filename_template_applies_on_editing_finished(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)
    frame.filename_template_edit.setText("clip-{date}")
    frame.filename_template_edit.editingFinished.emit()
    _flush_autosave(frame)
    assert applied[-1]["filename_template"] == "clip-{date}"


def test_editing_finished_without_an_actual_change_schedules_nothing(tmp_path: Path) -> None:
    # A plain focus-out fires editingFinished too -- don't flash "Saved ✓" over nothing.
    frame = _build(tmp_path)
    frame.filename_template_edit.editingFinished.emit()
    frame.hotkey_field.entry.editingFinished.emit()
    assert frame._apply_timer.isActive() is False


def test_save_payload_reflects_custom_preset_bitrate(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.bitrate_slider.setValue(12)
    _apply(frame)
    assert captured["quality_preset"] == "custom"
    assert captured["video_bitrate"] == "12M"


def test_save_payload_reflects_named_preset_bitrate(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.preset_control._buttons["Performance"].click()
    _apply(frame)
    assert captured["quality_preset"] == "performance"
    assert captured["video_bitrate"] == "4M"


def test_save_payload_reflects_auto_encoder(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert captured["encoder_override"] is None


def test_save_payload_reflects_manual_encoder_override(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.autodetect_switch.click()
    frame.encoder_control._buttons["NVENC"].click()
    _apply(frame)
    assert captured["encoder_override"] == "h264_nvenc"


def test_save_payload_window_mode_uses_selected_title(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_window_qt.window_capture, "list_windows",
        lambda os_: [WindowInfo(handle="1", title="My App", x=0, y=0, width=800, height=600)],
    )
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.target_control._buttons["Window"].click()
    frame._refresh_windows()
    frame.window_combo.setCurrentText("My App")
    _apply(frame)
    assert captured["capture_mode"] == "window"
    assert captured["window_title"] == "My App"


def test_save_payload_desktop_mode_keeps_prior_window_title(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, window_title="previously-saved-window", on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert captured["capture_mode"] == "desktop"
    assert captured["window_title"] == "previously-saved-window"


def test_save_payload_mic_none_when_no_picker_shown(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert captured["mic_device"] is None


def test_save_payload_mic_selected_device(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A"])
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.mic_combo.setCurrentText("Mic A")
    _apply(frame)
    assert captured["mic_device"] == "Mic A"


def test_save_payload_launch_on_startup_reflects_switch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.autostart, "is_supported", lambda os_: True)
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.launch_on_startup_switch.click()
    _apply(frame)
    assert captured["launch_on_startup"] is True


# ---- launch-on-startup toggle reconciles with real registration -------------------


def test_startup_switch_initializes_from_real_registration_state(tmp_path: Path, monkeypatch) -> None:
    # Registered at the OS level even though the config file says off --
    # the OS registration is the source of truth, not the persisted belief.
    monkeypatch.setattr(settings_window_qt.autostart, "is_supported", lambda os_: True)
    monkeypatch.setattr(settings_window_qt.autostart, "is_enabled", lambda os_: True)
    frame = _build(tmp_path, launch_on_startup=False)
    assert frame.launch_on_startup_switch.isChecked() is True


def test_startup_switch_initializes_unchecked_when_registration_was_removed_externally(
    tmp_path: Path, monkeypatch
) -> None:
    # The config file believes "on", but the Run value / .desktop file is
    # gone (deleted outside the app, or the registration failed last Save)
    # -- the toggle must not keep showing the stale belief, or it would
    # never diff and re-register.
    monkeypatch.setattr(settings_window_qt.autostart, "is_supported", lambda os_: True)
    # (the autouse fixture's is_enabled already returns False)
    frame = _build(tmp_path, launch_on_startup=True)
    assert frame.launch_on_startup_switch.isChecked() is False


def test_startup_switch_falls_back_to_config_when_probe_fails(tmp_path: Path, monkeypatch) -> None:
    # Best-effort, like every other probe in the codebase: a registration
    # probe that itself fails must not break the Settings tab -- fall back
    # to the persisted value.
    def boom(os_):
        raise OSError("registry unavailable (fake)")

    monkeypatch.setattr(settings_window_qt.autostart, "is_supported", lambda os_: True)
    monkeypatch.setattr(settings_window_qt.autostart, "is_enabled", boom)
    assert _build(tmp_path, launch_on_startup=True).launch_on_startup_switch.isChecked() is True
    assert _build(tmp_path, launch_on_startup=False).launch_on_startup_switch.isChecked() is False


def test_save_payload_check_for_updates_reflects_switch(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None, check_for_updates=False)
    frame.check_for_updates_switch.click()
    _apply(frame)
    assert captured["check_for_updates"] is True


# ---- appearance (theme mode) ----------------------------------------------------


def test_theme_control_reflects_config(tmp_path: Path) -> None:
    assert _build(tmp_path).theme_control.current() == "System"
    assert _build(tmp_path, theme_mode="light").theme_control.current() == "Light"
    assert _build(tmp_path, theme_mode="dark").theme_control.current() == "Dark"


def test_theme_control_unknown_mode_shows_system(tmp_path: Path) -> None:
    # A hand-edited config value the control has no segment for falls back to
    # "System" -- the mode apply_settings would reject anyway.
    assert _build(tmp_path, theme_mode="blue").theme_control.current() == "System"


def test_save_payload_theme_mode_reflects_control(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.theme_control._buttons["Dark"].click()
    _apply(frame)
    assert captured["theme_mode"] == "dark"
    assert "dark_mode" not in captured


# ---- autosave vs. the hotkey recorder -------------------------------------------


class _FakeListener:
    """Stands in for pynput.keyboard.Listener -- tests never hook real global
    keyboard input (same rule as test_hotkey_widget_qt.py).
    """

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_apply_fire_is_deferred_while_a_hotkey_recording_is_in_progress(tmp_path: Path, monkeypatch) -> None:
    # Autosave never cancels (or reads) a recorder mid-capture: the fire
    # reschedules instead, so the "Press keys..." placeholder can never be
    # persisted as a combo.
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)

    frame.quick_save_seconds_1_spin.setValue(45)  # schedules the autosave
    frame.hotkey_field.record_button.click()  # entry now shows "Press keys..."
    assert frame.hotkey_field.is_recording() is True

    frame._apply_timer.stop()
    frame._apply_now()  # the debounce fires mid-record

    assert applied == []  # deferred, not fired
    assert frame._apply_timer.isActive() is True  # re-armed for later

    frame.hotkey_field.cancel_recording()
    assert frame.hotkey_field.is_recording() is False
    frame._apply_timer.stop()
    frame._apply_now()

    # The earlier field change applied, with the PRE-RECORD combo in the
    # payload -- never the placeholder text.
    assert len(applied) == 1
    assert applied[0]["quick_save_seconds_1"] == 45
    assert applied[0]["hotkey_combo"] == "<ctrl>+<alt>+r"


def test_hotkey_recording_finished_with_a_new_combo_schedules_an_apply(tmp_path: Path, monkeypatch) -> None:
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)

    field = frame.hotkey_field
    field.record_button.click()
    field._on_key_press("ctrl")
    field._on_key_press("s")
    field._on_key_release("s")
    field._on_key_release("ctrl")

    assert field.is_recording() is False
    assert field.combo() == "<ctrl>+s"
    _flush_autosave(frame)
    assert applied[-1]["hotkey_combo"] == "<ctrl>+s"


def test_cancelled_hotkey_recording_schedules_nothing(tmp_path: Path, monkeypatch) -> None:
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    frame = _build(tmp_path)

    frame.hotkey_field.record_button.click()  # start
    frame.hotkey_field.record_button.click()  # cancel -- the pre-record combo is restored

    assert frame.hotkey_field.is_recording() is False
    assert frame.hotkey_field.combo() == "<ctrl>+<alt>+r"
    assert frame._apply_timer.isActive() is False  # nothing changed, nothing to save


def test_manually_typed_hotkey_applies_on_editing_finished(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)
    frame.hotkey_field.entry.setText("<ctrl>+<shift>+x")
    frame.hotkey_field.entry.editingFinished.emit()
    _flush_autosave(frame)
    assert applied[-1]["hotkey_combo"] == "<ctrl>+<shift>+x"


# ---- autosave: failure rollback, in-flight dirty -------------------------------


def test_failed_apply_shows_error_and_restores_last_good_values(tmp_path: Path) -> None:
    calls = []

    def fail(values):
        calls.append(dict(values))
        return "could not restart capture"

    frame = _build(tmp_path, on_apply=fail)
    frame.theme_control._buttons["Dark"].click()
    frame.quick_save_seconds_1_spin.setValue(45)
    _flush_autosave(frame)

    assert frame.status_label.text() == "could not restart capture"
    assert frame.status_label.property("state") == "error"
    # Every touched control rolled back to the last-known-good payload...
    assert frame.theme_control.current() == "System"
    assert frame.quick_save_seconds_1_spin.value() == 30
    assert frame._last_good["theme_mode"] == "system"
    # ...without re-triggering the autosave it undid.
    assert frame._apply_timer.isActive() is False
    assert len(calls) == 1


def test_failed_apply_then_a_retried_change_applies_cleanly(tmp_path: Path) -> None:
    attempts = []

    def fail_once(values):
        attempts.append(dict(values))
        return "could not restart capture" if len(attempts) == 1 else None

    frame = _build(tmp_path, on_apply=fail_once)
    frame.quick_save_seconds_1_spin.setValue(45)
    _flush_autosave(frame)
    assert frame.quick_save_seconds_1_spin.value() == 30  # rolled back

    frame.quick_save_seconds_1_spin.setValue(45)  # the user retries the same edit
    _flush_autosave(frame)
    assert frame.status_label.text() == "Saved ✓"
    assert frame.quick_save_seconds_1_spin.value() == 45
    assert frame._last_good["quick_save_seconds_1"] == 45


def test_successful_apply_shows_saved_and_advances_the_snapshot(tmp_path: Path) -> None:
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(dict(values)) or None)
    frame.theme_control._buttons["Dark"].click()
    _flush_autosave(frame)
    assert frame.status_label.text() == "Saved ✓"
    assert frame.status_label.property("state") == "success"
    assert frame._status_clear_timer.isActive()
    assert frame._last_good["theme_mode"] == "dark"


def test_change_during_an_in_flight_apply_gets_exactly_one_follow_up(tmp_path: Path) -> None:
    calls = []
    holder = {}

    def apply_and_edit_midway(values):
        calls.append(dict(values))
        if len(calls) == 1:
            # A field change landing while on_apply runs (it can restart
            # capture -- synchronous and slow) must not be lost.
            holder["frame"].quick_save_seconds_1_spin.setValue(99)
        return None

    frame = _build(tmp_path, on_apply=apply_and_edit_midway)
    holder["frame"] = frame

    frame.check_for_updates_switch.click()
    _flush_autosave(frame)
    assert len(calls) == 1
    assert frame._apply_timer.isActive() is True  # the dirty follow-up is scheduled

    frame._apply_timer.stop()
    frame._apply_now()
    assert len(calls) == 2  # exactly one follow-up...
    assert calls[1]["quick_save_seconds_1"] == 99  # ...carrying the mid-apply edit
    assert frame._apply_timer.isActive() is False  # and no third apply


# ---- Wayland window-capture hint ----------------------------------------------
#
# On Wayland the desktop portal's own share-dialog picks the window at
# capture-start, so the title picker can't pre-select one: it is disabled and
# a hint explains why. X11 keeps the picker exactly as before.


def test_wayland_window_mode_disables_title_entry_with_explanatory_hint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.platform_detect, "get_os", lambda: OS.LINUX)
    monkeypatch.setattr(
        settings_window_qt.platform_detect, "get_linux_session_type", lambda: LinuxSessionType.WAYLAND
    )
    frame = _build(tmp_path, capture_mode="window")

    assert frame.target_control.current() == "Window"
    assert frame.window_container.isHidden() is False
    assert frame.window_combo.isEnabled() is False
    assert frame.window_refresh_button.isEnabled() is False
    assert frame.window_wayland_hint is not None
    assert frame.window_wayland_hint.isHidden() is False
    assert "system dialog" in frame.window_wayland_hint.text()


def test_x11_window_mode_keeps_title_entry_enabled_without_hint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.platform_detect, "get_os", lambda: OS.LINUX)
    monkeypatch.setattr(settings_window_qt.platform_detect, "get_linux_session_type", lambda: LinuxSessionType.X11)
    frame = _build(tmp_path, capture_mode="window")

    assert frame.target_control.current() == "Window"
    assert frame.window_container.isHidden() is False
    assert frame.window_combo.isEnabled() is True
    assert frame.window_refresh_button.isEnabled() is True
    assert frame.window_wayland_hint is None


# ---- capture card: frame rate + resolution scale ------------------------------


def test_framerate_and_resolution_scale_combos_reflect_config(tmp_path: Path) -> None:
    frame = _build(tmp_path, framerate=60, resolution_scale="720p")
    assert frame.framerate_combo.currentData() == 60
    assert frame.resolution_scale_combo.currentData() == "720p"


def test_framerate_combo_offers_the_documented_choices(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    values = [frame.framerate_combo.itemData(i) for i in range(frame.framerate_combo.count())]
    assert values == [15, 24, 30, 60]
    assert frame.framerate_combo.currentData() == 30  # default


def test_framerate_combo_falls_back_to_30_for_a_non_choice_config_value(tmp_path: Path) -> None:
    frame = _build(tmp_path, framerate=23)  # hand-edited config
    assert frame.framerate_combo.currentData() == 30


def test_resolution_scale_combo_defaults_to_native(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    values = [frame.resolution_scale_combo.itemData(i) for i in range(frame.resolution_scale_combo.count())]
    assert values == ["native", "1080p", "720p"]
    assert frame.resolution_scale_combo.currentData() == "native"


def test_save_payload_includes_framerate_and_resolution_scale(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.framerate_combo.setCurrentIndex(frame.framerate_combo.findData(24))
    frame.resolution_scale_combo.setCurrentIndex(frame.resolution_scale_combo.findData("1080p"))
    _apply(frame)
    assert captured["framerate"] == 24
    assert captured["resolution_scale"] == "1080p"


# ---- save & hotkey card: quick-save + screenshot hotkeys -----------------------


def test_quick_save_and_screenshot_fields_reflect_config(tmp_path: Path) -> None:
    frame = _build(
        tmp_path,
        quick_save_hotkey_1="<ctrl>+1", quick_save_seconds_1=45,
        quick_save_hotkey_2="<ctrl>+2", quick_save_seconds_2=120,
        screenshot_hotkey="<ctrl>+<shift>+p",
    )
    assert frame.quick_save_hotkey_1_field.combo() == "<ctrl>+1"
    assert frame.quick_save_seconds_1_spin.value() == 45
    assert frame.quick_save_hotkey_2_field.combo() == "<ctrl>+2"
    assert frame.quick_save_seconds_2_spin.value() == 120
    assert frame.screenshot_hotkey_field.combo() == "<ctrl>+<shift>+p"


def test_quick_save_spinboxes_cover_the_documented_range(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert frame.quick_save_seconds_1_spin.minimum() == 5
    assert frame.quick_save_seconds_1_spin.maximum() == 300
    assert frame.quick_save_seconds_1_spin.suffix() == " s"


def test_save_payload_includes_quick_save_and_screenshot_hotkeys(tmp_path: Path) -> None:
    captured = {}
    frame = _build(
        tmp_path, on_apply=lambda values: captured.update(values) or None,
        quick_save_hotkey_1="<ctrl>+1", quick_save_seconds_1=45,
    )
    frame.quick_save_seconds_2_spin.setValue(120)
    frame.screenshot_hotkey_field.entry.setText("<ctrl>+<shift>+p")
    _apply(frame)
    assert captured["quick_save_hotkey_1"] == "<ctrl>+1"
    assert captured["quick_save_seconds_1"] == 45
    assert captured["quick_save_hotkey_2"] == ""  # empty = disabled, passed through
    assert captured["quick_save_seconds_2"] == 120
    assert captured["screenshot_hotkey"] == "<ctrl>+<shift>+p"


def test_apply_settings_error_for_a_bad_extra_hotkey_surfaces_on_the_status_label(tmp_path: Path) -> None:
    # Validation lives in apply_settings -- the frame shows its error string
    # like any other failure, and rolls the field back to the last-good value.
    frame = _build(
        tmp_path,
        on_apply=lambda values: "Invalid quick-save hotkey 1: 'bogus' -- use pynput format, e.g. <ctrl>+<alt>+r",
    )
    frame.quick_save_hotkey_1_field.entry.setText("bogus")
    _apply(frame)
    assert "Invalid quick-save hotkey 1" in frame.status_label.text()
    assert frame.status_label.property("state") == "error"
    assert frame.quick_save_hotkey_1_field.combo() == ""  # rolled back to the last-good (disabled)


# ---- check now -----------------------------------------------------------------


def test_last_checked_label_reads_the_update_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.update_check, "load_cache", lambda: {})
    assert _build(tmp_path).update_last_checked_label.text() == "Last checked: never"

    monkeypatch.setattr(settings_window_qt.update_check, "load_cache", lambda: {"last_checked": 1_700_000_000.0})
    label = _build(tmp_path).update_last_checked_label.text()
    assert label.startswith("Last checked: 20")  # %Y-%m-%d %H:%M of the epoch value


def test_check_now_update_found_raises_the_banner_callback(tmp_path: Path, monkeypatch) -> None:
    seen_kwargs = {}

    def fake_check(**kwargs):
        seen_kwargs.update(kwargs)
        return ("v9.9.9", "https://example.invalid/v9.9.9")

    monkeypatch.setattr(settings_window_qt.update_check, "check_for_update_once", fake_check)
    found = []
    frame = _build(tmp_path, on_update_found=lambda version, url: found.append((version, url)))

    frame._on_check_now()

    assert _wait_for(lambda: len(found) == 1)
    assert found == [("v9.9.9", "https://example.invalid/v9.9.9")]
    assert seen_kwargs["force"] is True  # a manual check bypasses the 24h throttle
    assert "9.9.9" in frame.status_label.text()
    assert frame.status_label.property("state") == "success"
    assert frame.check_now_button.isEnabled() is True  # re-enabled after the answer


def test_check_now_up_to_date(tmp_path: Path, monkeypatch) -> None:
    cache = {"last_checked": 1000.0}
    monkeypatch.setattr(settings_window_qt.update_check, "load_cache", lambda: dict(cache))

    def fake_check(**kwargs):
        cache["last_checked"] = 2000.0  # a completed check stamps the cache
        return None

    monkeypatch.setattr(settings_window_qt.update_check, "check_for_update_once", fake_check)
    frame = _build(tmp_path)

    frame._on_check_now()

    assert _wait_for(lambda: frame.status_label.text() == "You're up to date.")
    assert frame.status_label.property("state") == "success"


def test_check_now_network_failure_reports_check_failed(tmp_path: Path, monkeypatch) -> None:
    # check_for_update_once returns None and never stamped last_checked:
    # the fetch failed, which the worker distinguishes from "no update".
    monkeypatch.setattr(settings_window_qt.update_check, "load_cache", lambda: {})
    monkeypatch.setattr(settings_window_qt.update_check, "check_for_update_once", lambda **kwargs: None)
    frame = _build(tmp_path)

    frame._on_check_now()

    assert _wait_for(lambda: frame.status_label.text() == "Check failed (network?)")
    assert frame.status_label.property("state") == "error"
    assert frame.check_now_button.isEnabled() is True


def test_check_now_worker_raising_still_reports_failure(tmp_path: Path, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("unexpected (fake)")

    monkeypatch.setattr(settings_window_qt.update_check, "check_for_update_once", boom)
    frame = _build(tmp_path)

    frame._on_check_now()

    assert _wait_for(lambda: frame.status_label.text() == "Check failed (network?)")


# ---- 0.1.4: size cap / save sound ---------------------------------------------------


def test_initial_state_reflects_size_cap_and_sound(tmp_path: Path) -> None:
    frame = _build(tmp_path, clips_max_gb=12, save_sound_enabled=True)
    assert frame.size_cap_slider.value() == 12
    assert frame.size_cap_value_label.text() == "12 GB"
    assert frame.save_sound_switch.isChecked() is True


def test_size_cap_zero_shows_unlimited_and_follows_the_slider(tmp_path: Path) -> None:
    frame = _build(tmp_path, clips_max_gb=0)
    assert frame.size_cap_slider.value() == 0
    assert frame.size_cap_value_label.text() == "Unlimited"
    frame.size_cap_slider.setValue(25)
    assert frame.size_cap_value_label.text() == "25 GB"
    frame.size_cap_slider.setValue(0)
    assert frame.size_cap_value_label.text() == "Unlimited"


def test_out_of_range_size_cap_is_clamped_like_the_other_sliders(tmp_path: Path) -> None:
    frame = _build(tmp_path, clips_max_gb=99)
    assert frame.size_cap_slider.value() == 50
    assert frame.size_cap_value_label.text() == "50 GB"


def test_save_payload_collects_size_cap_and_sound(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.size_cap_slider.setValue(20)
    frame.save_sound_switch.setChecked(True)
    _apply(frame)
    assert captured["clips_max_gb"] == 20
    assert captured["save_sound_enabled"] is True


def test_save_payload_reports_defaults_when_untouched(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert captured["clips_max_gb"] == 0
    assert captured["save_sound_enabled"] is False


# ---- tab structure --------------------------------------------------------------


def _tab_labels(frame: SettingsFrame) -> list[str]:
    return [frame.tabs.tabText(i) for i in range(frame.tabs.count())]


def _tab_page(frame: SettingsFrame, label: str):
    return frame.tabs.widget(_tab_labels(frame).index(label))


def _is_descendant(widget, ancestor) -> bool:
    node = widget
    while node is not None:
        if node is ancestor:
            return True
        node = node.parentWidget()
    return False


def test_settings_fields_are_grouped_into_the_expected_tabs(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert _tab_labels(frame) == ["Capture", "Saving", "Encoder", "Clips", "Appearance", "About"]

    for widget in (
        frame.buffer_slider, frame.target_control, frame.window_combo,
        frame.framerate_combo, frame.resolution_scale_combo,
        frame.desktop_volume_slider, frame.mic_volume_slider,
    ):
        assert _is_descendant(widget, _tab_page(frame, "Capture")), widget

    for widget in (
        frame.clips_dir_edit, frame.hotkey_field,
        frame.quick_save_hotkey_1_field, frame.quick_save_seconds_1_spin,
        frame.quick_save_hotkey_2_field, frame.quick_save_seconds_2_spin,
        frame.screenshot_hotkey_field, frame.save_sound_switch,
        frame.launch_on_startup_switch,
    ):
        assert _is_descendant(widget, _tab_page(frame, "Saving")), widget

    for widget in (frame.autodetect_switch, frame.encoder_control, frame.preset_control, frame.bitrate_slider):
        assert _is_descendant(widget, _tab_page(frame, "Encoder")), widget

    for widget in (frame.filename_template_edit, frame.retention_slider, frame.size_cap_slider):
        assert _is_descendant(widget, _tab_page(frame, "Clips")), widget

    assert _is_descendant(frame.theme_control, _tab_page(frame, "Appearance"))

    for widget in (
        frame.check_for_updates_switch, frame.check_now_button,
        frame.update_last_checked_label, frame.about_version_label, frame.github_button,
    ):
        assert _is_descendant(widget, _tab_page(frame, "About")), widget


def test_every_tab_page_scrolls(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QScrollArea

    frame = _build(tmp_path)
    for i in range(frame.tabs.count()):
        assert isinstance(frame.tabs.widget(i), QScrollArea)


def test_about_tab_shows_version_license_and_github_link(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    assert __version__ in frame.about_version_label.text()
    label_texts = [label.text() for label in _tab_page(frame, "About").findChildren(QLabel)]
    assert any("GPL-3.0" in text for text in label_texts)
    assert any("github.com/lablooms/clipersal" in text for text in label_texts)
    assert frame.github_button.text() != ""


def test_reset_button_is_a_plain_button_left_of_the_status_label(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    # Resetting settings touches no files, so it is neither #primary nor #danger.
    assert frame.reset_button.objectName() == ""
    frame.resize(900, 600)
    frame.show()
    assert frame.reset_button.x() < frame.status_label.x()
    frame.close()


# ---- reset to defaults ----------------------------------------------------------


def _stub_confirm(monkeypatch, answer) -> None:
    monkeypatch.setattr(
        settings_window_qt, "quiet_message", lambda *args, **kwargs: answer
    )


def test_default_settings_payload_matches_config_field_defaults() -> None:
    payload = default_settings_payload()
    assert payload["buffer_seconds"] == 60
    assert payload["clips_dir"] == str(_default_clips_dir())
    assert payload["hotkey_combo"] == DEFAULT_COMBO
    assert payload["video_bitrate"] == "8M"
    assert payload["quality_preset"] == "custom"
    assert payload["capture_mode"] == "desktop"
    assert payload["monitor_index"] == 0
    assert payload["window_title"] == ""
    assert payload["mic_device"] is None
    assert payload["desktop_volume"] == 100
    assert payload["mic_volume"] == 100
    assert payload["encoder_override"] is None
    assert payload["filename_template"] == "{window}-{date}-{time}"
    assert payload["clip_retention_days"] == 0
    assert payload["launch_on_startup"] is False
    assert payload["check_for_updates"] is True
    assert payload["theme_mode"] == "system"
    assert payload["framerate"] == 30
    assert payload["resolution_scale"] == "native"
    assert payload["quick_save_hotkey_1"] == ""
    assert payload["quick_save_seconds_1"] == 30
    assert payload["quick_save_hotkey_2"] == ""
    assert payload["quick_save_seconds_2"] == 60
    assert payload["screenshot_hotkey"] == ""
    assert payload["clips_max_gb"] == 0
    assert payload["save_sound_enabled"] is False


def test_default_payload_keys_match_the_apply_payload_keys(tmp_path: Path) -> None:
    # Drift guard: a field added to the apply payload but not to the defaults
    # table (or vice versa) would silently survive a reset.
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    _apply(frame)
    assert set(captured) == set(default_settings_payload())


def test_reset_to_defaults_applies_defaults_and_repopulates_the_ui(tmp_path: Path, monkeypatch) -> None:
    _stub_confirm(monkeypatch, QMessageBox.StandardButton.Yes)
    captured = {}
    frame = _build(
        tmp_path,
        on_apply=lambda values: captured.update(values) or None,
        buffer_seconds=120, filename_template="custom-{date}", clip_retention_days=30,
        theme_mode="dark", check_for_updates=False, clips_max_gb=25,
    )
    assert frame.buffer_slider.value() == 120  # starts at the configured value

    frame.reset_button.click()

    defaults = default_settings_payload()
    assert captured == defaults
    # ...and the REBUILT fields show the defaults, not the old values.
    assert frame.buffer_slider.value() == defaults["buffer_seconds"]
    assert frame.buffer_value_label.text() == f"{defaults['buffer_seconds']}s"
    assert frame.filename_template_edit.text() == defaults["filename_template"]
    assert frame.retention_slider.value() == defaults["clip_retention_days"]
    assert frame.size_cap_slider.value() == defaults["clips_max_gb"]
    assert frame.theme_control.current() == "System"
    assert frame.check_for_updates_switch.isChecked() is True
    assert frame.hotkey_field.combo() == defaults["hotkey_combo"]
    assert frame.clips_dir_edit.text() == defaults["clips_dir"]
    assert _tab_labels(frame) == ["Capture", "Saving", "Encoder", "Clips", "Appearance", "About"]
    assert frame.status_label.text() == "Saved ✓"
    assert frame.status_label.property("state") == "success"
    # The reset payload is the new last-known-good snapshot...
    assert frame._last_good == defaults
    # ...and the rebuilt widgets are wired for autosave like the first build.
    frame.check_for_updates_switch.click()
    _flush_autosave(frame)
    assert captured["check_for_updates"] is False


def test_reset_to_defaults_cancelled_applies_nothing(tmp_path: Path, monkeypatch) -> None:
    _stub_confirm(monkeypatch, QMessageBox.StandardButton.No)
    applied = []
    frame = _build(tmp_path, on_apply=lambda values: applied.append(values) or None, buffer_seconds=120)
    old_tabs = frame.tabs
    frame.reset_button.click()
    assert applied == []
    assert frame.tabs is old_tabs  # untouched
    assert frame.buffer_slider.value() == 120


def test_reset_to_defaults_apply_error_is_shown_without_rebuilding(tmp_path: Path, monkeypatch) -> None:
    _stub_confirm(monkeypatch, QMessageBox.StandardButton.Yes)
    frame = _build(tmp_path, on_apply=lambda values: "could not restart capture", buffer_seconds=120)
    old_tabs = frame.tabs
    frame.reset_button.click()
    assert frame.status_label.text() == "could not restart capture"
    assert frame.status_label.property("state") == "error"
    assert frame.tabs is old_tabs  # no rebuild on failure
    assert frame.buffer_slider.value() == 120  # old values still shown


def test_reset_confirmation_calls_out_the_clips_folder_reset(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def fake_question(parent, title, text, *args, **kwargs):
        seen["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(settings_window_qt, "quiet_message", fake_question)
    frame = _build(tmp_path)
    frame.reset_button.click()
    assert str(_default_clips_dir()) in seen["text"]
    assert "not deleted" in seen["text"]


def test_tab_pages_do_not_autofill_an_unthemed_background(tmp_path: Path) -> None:
    # QScrollArea.setWidget flips the page's autoFillBackground ON, which
    # fills it with the unthemed palette Window grey in both modes -- the
    # "rogue dark background" report. Every tab page must have it off.
    frame = _build(tmp_path)
    for i in range(frame.tabs.count()):
        scroll = frame.tabs.widget(i)
        assert scroll.widget().autoFillBackground() is False
