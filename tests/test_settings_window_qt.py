import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

from clipersal import settings_window_qt
from clipersal.config import Config
from clipersal.ffmpeg_utils import AudioSource
from clipersal.monitors import MonitorInfo
from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.settings_window_qt import SettingsFrame, bitrate_string_to_mbps
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
    yield


def _make_config(tmp_path: Path, **overrides) -> Config:
    kwargs = dict(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    kwargs.update(overrides)
    return Config(**kwargs)


def _build(tmp_path: Path, on_apply=None, **config_overrides) -> SettingsFrame:
    config = _make_config(tmp_path, **config_overrides)
    return SettingsFrame(
        config, ipc_port=51525, save_events=None, current_encoder="libx264",
        on_apply=on_apply or (lambda values: None), ffmpeg_path="ffmpeg",
    )


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
    frame._on_save()
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
    frame = _build(tmp_path)
    frame._browse_clips_dir()
    assert frame.clips_dir_edit.text() == chosen_dir


def test_browse_leaves_clips_dir_unchanged_when_dialog_cancelled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    frame = _build(tmp_path)
    original = frame.clips_dir_edit.text()
    frame._browse_clips_dir()
    assert frame.clips_dir_edit.text() == original


# ---- save validation and payload --------------------------------------------


def test_save_rejects_empty_hotkey(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.hotkey_field.entry.setText("")
    frame._on_save()
    assert frame.status_label.text() == "Hotkey cannot be empty."
    assert frame.status_label.property("state") == "error"


def test_save_rejects_empty_clips_dir(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.clips_dir_edit.setReadOnly(False)
    frame.clips_dir_edit.setText("   ")
    frame._on_save()
    assert frame.status_label.text() == "Clips folder cannot be empty."


def test_save_rejects_empty_filename_template(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame.filename_template_edit.setText("")
    frame._on_save()
    assert frame.status_label.text() == "Filename template cannot be empty."


def test_save_success_shows_confirmation_and_schedules_clear(tmp_path: Path) -> None:
    frame = _build(tmp_path)
    frame._on_save()
    assert frame.status_label.text() == "Settings saved."
    assert frame.status_label.property("state") == "success"
    assert frame._status_clear_timer.isActive()


def test_save_shows_error_returned_by_on_apply(tmp_path: Path) -> None:
    frame = _build(tmp_path, on_apply=lambda values: "could not restart capture")
    frame._on_save()
    assert frame.status_label.text() == "could not restart capture"
    assert frame.status_label.property("state") == "error"


def test_save_payload_reflects_custom_preset_bitrate(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.bitrate_slider.setValue(12)
    frame._on_save()
    assert captured["quality_preset"] == "custom"
    assert captured["video_bitrate"] == "12M"


def test_save_payload_reflects_named_preset_bitrate(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.preset_control._buttons["Performance"].click()
    frame._on_save()
    assert captured["quality_preset"] == "performance"
    assert captured["video_bitrate"] == "4M"


def test_save_payload_reflects_auto_encoder(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame._on_save()
    assert captured["encoder_override"] is None


def test_save_payload_reflects_manual_encoder_override(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.autodetect_switch.click()
    frame.encoder_control._buttons["NVENC"].click()
    frame._on_save()
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
    frame._on_save()
    assert captured["capture_mode"] == "window"
    assert captured["window_title"] == "My App"


def test_save_payload_desktop_mode_keeps_prior_window_title(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, window_title="previously-saved-window", on_apply=lambda values: captured.update(values) or None)
    frame._on_save()
    assert captured["capture_mode"] == "desktop"
    assert captured["window_title"] == "previously-saved-window"


def test_save_payload_mic_none_when_no_picker_shown(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame._on_save()
    assert captured["mic_device"] is None


def test_save_payload_mic_selected_device(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.ffmpeg_utils, "list_microphones", lambda ffmpeg_path, os_: ["Mic A"])
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.mic_combo.setCurrentText("Mic A")
    frame._on_save()
    assert captured["mic_device"] == "Mic A"


def test_save_payload_launch_on_startup_reflects_switch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_window_qt.autostart, "is_supported", lambda os_: True)
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)
    frame.launch_on_startup_switch.click()
    frame._on_save()
    assert captured["launch_on_startup"] is True


def test_save_payload_check_for_updates_reflects_switch(tmp_path: Path) -> None:
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None, check_for_updates=False)
    frame.check_for_updates_switch.click()
    frame._on_save()
    assert captured["check_for_updates"] is True


# ---- saving while the hotkey recorder is mid-capture ------------------------


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


def test_save_while_hotkey_recording_cancels_recorder_instead_of_persisting_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    captured = {}
    frame = _build(tmp_path, on_apply=lambda values: captured.update(values) or None)

    frame.hotkey_field.record_button.click()  # entry now shows "Press keys..."
    assert frame.hotkey_field.is_recording() is True

    frame._on_save()

    # The recording was cancelled and the PRE-RECORD combo is what got
    # applied -- not the placeholder text.
    assert frame.hotkey_field.is_recording() is False
    assert captured["hotkey_combo"] == "<ctrl>+<alt>+r"
    assert frame.status_label.text() == "Settings saved."


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
