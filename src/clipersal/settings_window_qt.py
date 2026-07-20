"""Settings tab -- the biggest single tab. Cards are laid out in a
two-column split (via two QVBoxLayouts inside a QHBoxLayout) -- Capture +
Save & hotkey on the left, Encoder + Clip management on the right -- for a
roughly-16:9 panel shape.

The Monitor/Window sub-panels under "Capture target" and the bitrate slider
under "Quality preset" are shown/hidden with plain `.setVisible(bool)`; Qt's
QVBoxLayout keeps widgets at a fixed index regardless of visibility, so
toggling one panel never disturbs the position of the others.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from clipersal import autostart, config_store, ffmpeg_utils, monitors, platform_detect, window_capture
from clipersal.config import Config
from clipersal.hotkey_widget_qt import HotkeyField
from clipersal.qt_widgets import SegmentedControl, ToggleSwitch
from clipersal.theme import qfont as _qfont
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_ENCODER_CHOICES = [("NVENC", "h264_nvenc"), ("VAAPI", "h264_vaapi"), ("QSV", "h264_qsv"), ("libx264", "libx264")]
_BUFFER_RANGE = (10, 300)
_BITRATE_RANGE_MBPS = (2, 20)
_RETENTION_RANGE_DAYS = (0, 90)
_VOLUME_RANGE_PERCENT = (0, 200)
_SAVED_MESSAGE_CLEAR_MS = 2500

_QUALITY_PRESET_CHOICES = [
    ("Performance", "performance"),
    ("Balanced", "balanced"),
    ("Quality", "quality"),
    ("Custom", "custom"),
]
_QUALITY_PRESET_DESCRIPTIONS = {
    "performance": "Lower bitrate, fastest encode -- best for weaker GPUs/CPUs or long buffers.",
    "balanced": "Good quality at a moderate file size -- a sensible default for most setups.",
    "quality": "Higher bitrate, slower encode -- best visual quality, larger files.",
}

_CAPTURE_TARGET_LABELS = {"desktop": "Desktop", "monitor": "Monitor", "window": "Window"}
_CAPTURE_TARGET_MODES = {label: mode for mode, label in _CAPTURE_TARGET_LABELS.items()}
_NO_WINDOW_SELECTED = "(none selected)"
_NO_WINDOWS_FOUND = "(no windows found)"


def bitrate_string_to_mbps(text: str) -> int:
    """Parse a bitrate string like '8M', '4000k', or '12000000' into whole
    Mbps, clamped to the slider's range. Falls back to the range midpoint
    for anything unparseable, e.g. a hand-edited config file with a typo --
    the slider should never crash the window over a bad string.
    """
    low, high = _BITRATE_RANGE_MBPS
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$", text or "")
    if not match:
        return (low + high) // 2
    value, suffix = float(match.group(1)), match.group(2).lower()
    if suffix == "k":
        mbps = value / 1000
    elif suffix == "m" or suffix == "":
        mbps = value
    else:
        mbps = value
    return max(low, min(high, round(mbps)))


def _mbps_to_bitrate_string(mbps: int) -> str:
    return f"{mbps}M"


def _format_retention_label(days: int) -> str:
    return "Forever" if days <= 0 else f"{days}d"


class SettingsFrame(QWidget):
    """on_apply(new_values) is called with the validated field values on
    Save; it should live-apply what it can, persist to the config file, and
    return None on success or an error string to display on failure.
    """

    def __init__(
        self,
        config: Config,
        ipc_port: int,
        save_events,
        current_encoder: str,
        on_apply: Callable[[dict], str | None],
        ffmpeg_path: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._on_apply = on_apply
        self._os = platform_detect.get_os()
        # On Wayland the window-title picker can't work: window capture goes
        # through the desktop portal's own share-dialog, which picks the
        # window at capture-start -- our title list can't pre-select one.
        self._on_wayland = (
            self._os == platform_detect.OS.LINUX
            and platform_detect.get_linux_session_type() == platform_detect.LinuxSessionType.WAYLAND
        )

        self._preset_value_by_label = {label: value for label, value in _QUALITY_PRESET_CHOICES}
        self._encoder_label_by_value = {value: label for label, value in _ENCODER_CHOICES}
        self._encoder_value_by_label = {label: value for label, value in _ENCODER_CHOICES}
        self._monitor_index_by_label: dict[str, int] = {}

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(18, 18, 18, 18)
        outer_layout.setSpacing(8)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout.addWidget(scroll, 1)

        columns_container = QWidget()
        columns_layout = QHBoxLayout(columns_container)
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(18)
        scroll.setWidget(columns_container)

        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        columns_layout.addWidget(left_container, 1)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)
        columns_layout.addWidget(right_container, 1)

        self._build_capture_card(left_layout, config, ffmpeg_path)
        self._build_save_hotkey_card(left_layout, config)
        self._build_encoder_card(right_layout, config, current_encoder)
        self._build_clip_management_card(right_layout, config)

        left_layout.addStretch()
        right_layout.addStretch()

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        outer_layout.addWidget(self.status_label)

        footer = QHBoxLayout()
        outer_layout.addLayout(footer)
        view_logs_button = QPushButton("View logs", self)
        view_logs_button.clicked.connect(lambda: open_folder(config_store.default_log_path().parent))
        footer.addWidget(view_logs_button)
        footer.addStretch()
        save_button = QPushButton("Save", self)
        save_button.setObjectName("primary")
        save_button.clicked.connect(self._on_save)
        footer.addWidget(save_button)

        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(lambda: self.status_label.setText(""))

    # ---- small layout helpers -------------------------------------------

    def _make_card(self, column_layout: QVBoxLayout, title: str) -> QVBoxLayout:
        card = QFrame(self)
        card.setObjectName("card")
        column_layout.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(6)
        title_label = QLabel(title.upper(), card)
        title_label.setObjectName("cardTitle")
        card_layout.addWidget(title_label)
        return card_layout

    def _bold_label(self, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _field_row(self, card_layout: QVBoxLayout, label_text: str, value_text: str, parent: QWidget) -> QLabel:
        row = QHBoxLayout()
        card_layout.addLayout(row)
        row.addWidget(self._bold_label(label_text, parent))
        row.addStretch()
        value_label = QLabel(value_text, parent)
        value_label.setObjectName("valueBadge")
        value_label.setFont(_qfont(mono=True))
        row.addWidget(value_label)
        return value_label

    def _hint(self, card_layout: QVBoxLayout, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("hint")
        label.setWordWrap(True)
        card_layout.addWidget(label)
        return label

    # ---- Capture card ----------------------------------------------------

    def _build_capture_card(self, left_layout: QVBoxLayout, config: Config, ffmpeg_path: str) -> None:
        capture_layout = self._make_card(left_layout, "Capture")
        card = capture_layout.parentWidget()

        self.buffer_value_label = self._field_row(capture_layout, "Buffer length", f"{config.buffer_seconds}s", card)
        self.buffer_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.buffer_slider.setRange(*_BUFFER_RANGE)
        self.buffer_slider.setValue(config.buffer_seconds)
        self.buffer_slider.valueChanged.connect(lambda v: self.buffer_value_label.setText(f"{v}s"))
        capture_layout.addWidget(self.buffer_slider)
        self._hint(capture_layout, "How much history stays in the rolling buffer (10s–300s)", card)

        self._build_capture_target(capture_layout, config, card)
        self._build_microphone_picker(capture_layout, config, ffmpeg_path, card)
        self._build_volume_controls(capture_layout, config, ffmpeg_path, card)
        self._build_quality_preset(capture_layout, config, card)

    def _build_capture_target(self, capture_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        detected_monitors = monitors.list_monitors(self._os)

        capture_layout.addWidget(self._bold_label("Capture target", card))

        target_choices = ["Desktop"]
        if len(detected_monitors) > 1:
            target_choices.append("Monitor")
        target_choices.append("Window")

        self.target_control = SegmentedControl(target_choices, card)
        self.target_control.setFont(_qfont(mono=True))
        initial_target = _CAPTURE_TARGET_LABELS.get(config.capture_mode, "Desktop")
        if initial_target not in target_choices:
            initial_target = "Desktop"  # e.g. saved as "Monitor" but only one monitor is attached now
        self.target_control.setCurrent(initial_target)
        capture_layout.addWidget(self.target_control)

        # ---- Monitor sub-panel ----
        self.monitor_container = QWidget(card)
        monitor_layout = QVBoxLayout(self.monitor_container)
        monitor_layout.setContentsMargins(0, 6, 0, 0)
        self.monitor_combo: QComboBox | None = None
        if len(detected_monitors) > 1:
            monitor_label_by_index = {
                mon.index: f"Monitor {mon.index + 1}: {mon.width}x{mon.height}"
                + (" (Primary)" if mon.is_primary else "")
                for mon in detected_monitors
            }
            self._monitor_index_by_label = {label: index for index, label in monitor_label_by_index.items()}
            self.monitor_combo = QComboBox(self.monitor_container)
            self.monitor_combo.addItems(list(monitor_label_by_index.values()))
            initial_monitor_label = monitor_label_by_index.get(
                config.monitor_index, next(iter(monitor_label_by_index.values()))
            )
            self.monitor_combo.setCurrentText(initial_monitor_label)
            monitor_layout.addWidget(self.monitor_combo)
            self._hint(monitor_layout, "Which physical display to capture", self.monitor_container)
        capture_layout.addWidget(self.monitor_container)

        # ---- Window sub-panel ----
        self.window_container = QWidget(card)
        window_layout = QVBoxLayout(self.window_container)
        window_layout.setContentsMargins(0, 6, 0, 0)
        window_row = QHBoxLayout()
        window_layout.addLayout(window_row)
        self.window_combo = QComboBox(self.window_container)
        self.window_combo.addItem(config.window_title or _NO_WINDOW_SELECTED)
        window_row.addWidget(self.window_combo, 1)
        self.window_refresh_button = QPushButton("Refresh", self.window_container)
        self.window_refresh_button.clicked.connect(self._refresh_windows)
        window_row.addWidget(self.window_refresh_button)
        self._hint(window_layout, "Captures just this one window instead of the whole screen", self.window_container)
        self.window_wayland_hint: QLabel | None = None
        if self._on_wayland:
            # The title entry is meaningless on Wayland (see __init__):
            # disable it and say what picks the window instead.
            self.window_combo.setEnabled(False)
            self.window_refresh_button.setEnabled(False)
            self.window_wayland_hint = self._hint(
                window_layout,
                "On Wayland, your desktop's own system dialog asks which window to share when "
                "capture starts -- this list can't pre-select one, so it's disabled.",
                self.window_container,
            )
        capture_layout.addWidget(self.window_container)
        self._refresh_windows()  # populate immediately rather than showing it empty until first click

        self.target_control.currentTextChanged.connect(self._on_target_change)
        self._on_target_change(self.target_control.current())

    def _refresh_windows(self) -> None:
        found = window_capture.list_windows(self._os)
        titles = [w.title for w in found] or [_NO_WINDOWS_FOUND]
        current = self.window_combo.currentText() if self.window_combo.count() else None
        self.window_combo.clear()
        self.window_combo.addItems(titles)
        if current in titles:
            self.window_combo.setCurrentText(current)
        else:
            self.window_combo.setCurrentIndex(0)

    def _on_target_change(self, _choice: str | None = None) -> None:
        selected = self.target_control.current()
        self.monitor_container.setVisible(selected == "Monitor")
        self.window_container.setVisible(selected == "Window")

    def _build_microphone_picker(self, capture_layout: QVBoxLayout, config: Config, ffmpeg_path: str, card: QWidget) -> None:
        # Only shown when at least one real (non-loopback) input device was
        # found -- a machine with no microphone at all never sees a picker
        # with nothing to offer, same reasoning as the monitor picker above.
        mic_names = ffmpeg_utils.list_microphones(ffmpeg_path, self._os)
        self.mic_combo: QComboBox | None = None
        if mic_names:
            capture_layout.addWidget(self._bold_label("Microphone", card))
            self.mic_combo = QComboBox(card)
            self.mic_combo.addItems(["None", *mic_names])
            initial_mic = config.mic_device if config.mic_device in mic_names else "None"
            self.mic_combo.setCurrentText(initial_mic)
            capture_layout.addWidget(self.mic_combo)
            self._hint(capture_layout, "Mixed in alongside system audio, if a loopback source is available", card)

    def _build_volume_controls(self, capture_layout: QVBoxLayout, config: Config, ffmpeg_path: str, card: QWidget) -> None:
        self.desktop_volume_value_label = self._field_row(
            capture_layout, "Desktop volume", f"{config.desktop_volume}%", card
        )
        self.desktop_volume_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.desktop_volume_slider.setRange(*_VOLUME_RANGE_PERCENT)
        self.desktop_volume_slider.setValue(config.desktop_volume)
        self.desktop_volume_slider.valueChanged.connect(
            lambda v: self.desktop_volume_value_label.setText(f"{v}%")
        )
        capture_layout.addWidget(self.desktop_volume_slider)
        if ffmpeg_utils.find_audio_source(ffmpeg_path, self._os) is None:
            # With no loopback source capture has no desktop audio track at
            # all, so the slider would adjust nothing -- the same reasoning
            # that hides the mic picker when no microphone exists.
            self.desktop_volume_slider.setEnabled(False)
            self._hint(
                capture_layout, "No system-audio loopback was detected, so there is no desktop audio to adjust", card
            )
        else:
            self._hint(capture_layout, "Loudness of the captured system audio (100% = unchanged)", card)

        self.mic_volume_value_label = self._field_row(
            capture_layout, "Microphone volume", f"{config.mic_volume}%", card
        )
        self.mic_volume_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.mic_volume_slider.setRange(*_VOLUME_RANGE_PERCENT)
        self.mic_volume_slider.setValue(config.mic_volume)
        self.mic_volume_slider.valueChanged.connect(
            lambda v: self.mic_volume_value_label.setText(f"{v}%")
        )
        capture_layout.addWidget(self.mic_volume_slider)
        self.mic_volume_hint = self._hint(capture_layout, "", card)
        if self.mic_combo is not None:
            self.mic_combo.currentTextChanged.connect(self._on_mic_volume_state_change)
        self._on_mic_volume_state_change()

    def _on_mic_volume_state_change(self, _text: str | None = None) -> None:
        # A mic volume only means something when a microphone is actually
        # mixed in, so the slider tracks the mic picker's availability.
        if self.mic_combo is None:
            self.mic_volume_slider.setEnabled(False)
            self.mic_volume_hint.setText("No microphone was detected on this machine.")
        elif self.mic_combo.currentText() == "None":
            self.mic_volume_slider.setEnabled(False)
            self.mic_volume_hint.setText("Pick a microphone above to adjust its loudness.")
        else:
            self.mic_volume_slider.setEnabled(True)
            self.mic_volume_hint.setText("Loudness of the microphone mixed into the recording (100% = unchanged).")

    def _build_quality_preset(self, capture_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        capture_layout.addWidget(self._bold_label("Quality preset", card))

        self.preset_control = SegmentedControl([label for label, _ in _QUALITY_PRESET_CHOICES], card)
        self.preset_control.setFont(_qfont(mono=True))
        preset_label_by_value = {value: label for label, value in _QUALITY_PRESET_CHOICES}
        initial_preset_label = preset_label_by_value.get(config.quality_preset, "Custom")
        self.preset_control.setCurrent(initial_preset_label)
        capture_layout.addWidget(self.preset_control)

        self.preset_desc_label = QLabel("", card)
        self.preset_desc_label.setObjectName("hint")
        self.preset_desc_label.setWordWrap(True)
        capture_layout.addWidget(self.preset_desc_label)

        initial_mbps = bitrate_string_to_mbps(config.video_bitrate)
        self.bitrate_container = QWidget(card)
        bitrate_layout = QVBoxLayout(self.bitrate_container)
        bitrate_layout.setContentsMargins(0, 6, 0, 0)
        self.bitrate_value_label = self._field_row(
            bitrate_layout, "Video bitrate", f"{initial_mbps} Mbps", self.bitrate_container
        )
        self.bitrate_slider = QSlider(Qt.Orientation.Horizontal, self.bitrate_container)
        self.bitrate_slider.setRange(*_BITRATE_RANGE_MBPS)
        self.bitrate_slider.setValue(initial_mbps)
        self.bitrate_slider.valueChanged.connect(lambda v: self.bitrate_value_label.setText(f"{v} Mbps"))
        bitrate_layout.addWidget(self.bitrate_slider)
        self._hint(bitrate_layout, "Higher bitrate = better quality, larger clip files", self.bitrate_container)
        capture_layout.addWidget(self.bitrate_container)

        self.preset_control.currentTextChanged.connect(self._on_preset_change)
        self._on_preset_change(self.preset_control.current())

    def _on_preset_change(self, _choice: str | None = None) -> None:
        selected = self._preset_value_by_label.get(self.preset_control.current(), "custom")
        if selected == "custom":
            self.preset_desc_label.setText("Manual control -- set your own bitrate below.")
            self.bitrate_container.setVisible(True)
        else:
            spec = ffmpeg_utils.QUALITY_PRESETS[selected]
            self.preset_desc_label.setText(f"{_QUALITY_PRESET_DESCRIPTIONS[selected]} ({spec['bitrate_mbps']} Mbps)")
            self.bitrate_container.setVisible(False)

    # ---- Save & hotkey card ----------------------------------------------

    def _build_save_hotkey_card(self, left_layout: QVBoxLayout, config: Config) -> None:
        save_layout = self._make_card(left_layout, "Save && hotkey")
        card = save_layout.parentWidget()

        save_layout.addWidget(self._bold_label("Clips folder", card))
        clips_row = QHBoxLayout()
        save_layout.addLayout(clips_row)
        self.clips_dir_edit = QLineEdit(str(config.clips_dir), card)
        self.clips_dir_edit.setReadOnly(True)
        self.clips_dir_edit.setFont(_qfont(mono=True))
        clips_row.addWidget(self.clips_dir_edit, 1)
        browse_button = QPushButton("Browse...", card)
        browse_button.clicked.connect(self._browse_clips_dir)
        clips_row.addWidget(browse_button)

        save_layout.addWidget(self._bold_label("Hotkey", card))
        self.hotkey_field = HotkeyField(config.hotkey_combo, card)
        save_layout.addWidget(self.hotkey_field)
        self._hint(
            save_layout, "Click Record and press a key combo, or type pynput format directly, e.g. <ctrl>+<alt>+r", card
        )

        startup_supported = autostart.is_supported(self._os)
        startup_row = QHBoxLayout()
        save_layout.addLayout(startup_row)
        startup_text_col = QVBoxLayout()
        startup_row.addLayout(startup_text_col, 1)
        startup_text_col.addWidget(self._bold_label("Launch on startup", card))
        startup_desc = QLabel(
            "Start automatically when you log in" if startup_supported else "Not supported on this platform yet", card
        )
        startup_desc.setObjectName("hint")
        startup_text_col.addWidget(startup_desc)
        self.launch_on_startup_switch = ToggleSwitch(card, checked=(startup_supported and config.launch_on_startup))
        startup_row.addWidget(self.launch_on_startup_switch)
        if not startup_supported:
            self.launch_on_startup_switch.setEnabled(False)

        update_row = QHBoxLayout()
        save_layout.addLayout(update_row)
        update_text_col = QVBoxLayout()
        update_row.addLayout(update_text_col, 1)
        update_text_col.addWidget(self._bold_label("Check for updates automatically", card))
        update_desc = QLabel("Check GitHub for a newer version once at startup", card)
        update_desc.setObjectName("hint")
        update_text_col.addWidget(update_desc)
        self.check_for_updates_switch = ToggleSwitch(card, checked=config.check_for_updates)
        update_row.addWidget(self.check_for_updates_switch)

    def _browse_clips_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(
            self, "Choose clips folder", self.clips_dir_edit.text() or str(Path.home())
        )
        if chosen:
            self.clips_dir_edit.setText(chosen)

    # ---- Encoder card ------------------------------------------------------

    def _build_encoder_card(self, right_layout: QVBoxLayout, config: Config, current_encoder: str) -> None:
        encoder_layout = self._make_card(right_layout, "Encoder")
        card = encoder_layout.parentWidget()

        autodetect_row = QHBoxLayout()
        encoder_layout.addLayout(autodetect_row)
        autodetect_text_col = QVBoxLayout()
        autodetect_row.addLayout(autodetect_text_col, 1)
        autodetect_text_col.addWidget(self._bold_label("Auto-detect", card))
        autodetect_desc = QLabel("Picks NVENC / VAAPI / QSV, falling back to libx264", card)
        autodetect_desc.setObjectName("hint")
        autodetect_desc.setWordWrap(True)
        autodetect_text_col.addWidget(autodetect_desc)

        initial_manual_label = self._encoder_label_by_value.get(config.encoder_override or current_encoder, "libx264")
        self.encoder_control = SegmentedControl([label for label, _ in _ENCODER_CHOICES], card)
        self.encoder_control.setFont(_qfont(mono=True))
        self.encoder_control.setCurrent(initial_manual_label)
        encoder_layout.addWidget(self.encoder_control)

        self.autodetect_switch = ToggleSwitch(card, checked=(config.encoder_override is None))
        autodetect_row.addWidget(self.autodetect_switch)
        self.autodetect_switch.toggled.connect(self._on_autodetect_toggle)
        self._on_autodetect_toggle(config.encoder_override is None)

    def _on_autodetect_toggle(self, enabled: bool) -> None:
        self.encoder_control.setEnabled(not enabled)

    # ---- Clip management card ---------------------------------------------

    def _build_clip_management_card(self, right_layout: QVBoxLayout, config: Config) -> None:
        clips_layout = self._make_card(right_layout, "Clip management")
        card = clips_layout.parentWidget()

        clips_layout.addWidget(self._bold_label("Filename template", card))
        self.filename_template_edit = QLineEdit(config.filename_template, card)
        self.filename_template_edit.setFont(_qfont(mono=True))
        clips_layout.addWidget(self.filename_template_edit)
        self._hint(clips_layout, "Placeholders: {date}, {time}, {datetime}", card)

        self.retention_value_label = self._field_row(
            clips_layout, "Keep clips for", _format_retention_label(config.clip_retention_days), card
        )
        self.retention_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.retention_slider.setRange(*_RETENTION_RANGE_DAYS)
        self.retention_slider.setValue(config.clip_retention_days)
        self.retention_slider.valueChanged.connect(
            lambda v: self.retention_value_label.setText(_format_retention_label(v))
        )
        clips_layout.addWidget(self.retention_slider)
        self._hint(clips_layout, "0 = keep forever; older clips are deleted automatically otherwise", card)

    # ---- save ---------------------------------------------------------------

    def _set_status(self, text: str, state: str) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def _on_save(self) -> None:
        self._status_clear_timer.stop()

        # Saving while the recorder is mid-capture would read (and persist)
        # its "Press keys..." placeholder as the combo -- cancel the
        # recording first so the pre-record text is what gets read below.
        if self.hotkey_field.is_recording():
            self.hotkey_field.cancel_recording()

        hotkey_text = self.hotkey_field.combo().strip()
        if not hotkey_text:
            self._set_status("Hotkey cannot be empty.", "error")
            return
        clips_text = self.clips_dir_edit.text().strip()
        if not clips_text:
            self._set_status("Clips folder cannot be empty.", "error")
            return
        filename_template_text = self.filename_template_edit.text().strip()
        if not filename_template_text:
            self._set_status("Filename template cannot be empty.", "error")
            return

        is_auto = self.autodetect_switch.isChecked()
        encoder_override = None if is_auto else self._encoder_value_by_label.get(self.encoder_control.current())

        selected_preset = self._preset_value_by_label.get(self.preset_control.current(), "custom")
        if selected_preset == "custom":
            video_bitrate_value = _mbps_to_bitrate_string(self.bitrate_slider.value())
        else:
            video_bitrate_value = f"{ffmpeg_utils.QUALITY_PRESETS[selected_preset]['bitrate_mbps']}M"

        monitor_index_value = (
            self._monitor_index_by_label.get(self.monitor_combo.currentText(), self._config.monitor_index)
            if self.monitor_combo is not None
            else self._config.monitor_index
        )

        capture_mode_value = _CAPTURE_TARGET_MODES.get(self.target_control.current(), "desktop")
        if capture_mode_value == "window":
            candidate_title = self.window_combo.currentText()
            window_title_value = "" if candidate_title in (_NO_WINDOW_SELECTED, _NO_WINDOWS_FOUND) else candidate_title
        else:
            window_title_value = self._config.window_title

        if self.mic_combo is not None:
            selected_mic = self.mic_combo.currentText()
            mic_device_value = None if selected_mic == "None" else selected_mic
        else:
            mic_device_value = self._config.mic_device

        error = self._on_apply(
            {
                "buffer_seconds": self.buffer_slider.value(),
                "clips_dir": clips_text,
                "hotkey_combo": hotkey_text,
                "video_bitrate": video_bitrate_value,
                "quality_preset": selected_preset,
                "capture_mode": capture_mode_value,
                "monitor_index": monitor_index_value,
                "window_title": window_title_value,
                "mic_device": mic_device_value,
                "desktop_volume": self.desktop_volume_slider.value(),
                "mic_volume": self.mic_volume_slider.value(),
                "encoder_override": encoder_override,
                "filename_template": filename_template_text,
                "clip_retention_days": self.retention_slider.value(),
                "launch_on_startup": self.launch_on_startup_switch.isChecked(),
                "check_for_updates": self.check_for_updates_switch.isChecked(),
            }
        )
        if error:
            self._set_status(error, "error")
            return

        self._set_status("Settings saved.", "success")
        self._status_clear_timer.start(_SAVED_MESSAGE_CLEAR_MS)


def build_settings_frame(
    parent: QWidget | None,
    config: Config,
    ipc_port: int,
    save_events,
    current_encoder: str,
    on_apply: Callable[[dict], str | None],
    ffmpeg_path: str,
) -> SettingsFrame:
    """Thin factory function kept for API parity with the CustomTkinter
    original's `build_settings_frame(...)` shape.
    """
    return SettingsFrame(config, ipc_port, save_events, current_encoder, on_apply, ffmpeg_path, parent)
