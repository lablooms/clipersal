"""Settings tab -- a themed QTabWidget grouping the existing fields into
Capture / Saving / Encoder / Clips / Appearance / About, plus a Logs
sub-tab at the end (the log viewer moved here from its old top-level page;
see LogsTab). The old two-column card scroll mixed unrelated concerns onto
one long page; the update-check machinery moved off the hotkey card into
About to declutter it. Every field-tab page scrolls, since Capture alone
is taller than the window minimum.

The Monitor/Window sub-panels under "Capture target" and the bitrate slider
under "Quality preset" are shown/hidden with plain `.setVisible(bool)`; Qt's
QVBoxLayout keeps widgets at a fixed index regardless of visibility, so
toggling one panel never disturbs the position of the others.

There is no Save button: every field AUTOSAVES. Settled-change signals (a
combo pick, a toggled switch, a released slider, a finished hotkey
recording, ...) restart a ~500 ms debounce QTimer whose fire applies the
whole payload through the same on_apply the old Save button used -- so
capture-restarting fields can never restart ffmpeg mid-drag or mid-
keystroke. A failed apply rolls every control back to the last-known-good
payload; an invalid one (empty hotkey / clips folder / filename template)
just shows the guard's error and waits for the next edit.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from clipersal import (
    __version__,
    autostart,
    config_store,
    diagnostics,
    ffmpeg_utils,
    monitors,
    platform_detect,
    theme,
    update_check,
    window_capture,
)
from clipersal.config import Config, _default_clips_dir
from clipersal.hotkey_widget_qt import HotkeyField
from clipersal.qt_widgets import SegmentedControl, StepperSpinBox, ToggleSwitch, quiet_message
from clipersal.theme import qfont as _qfont
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_ENCODER_CHOICES = [("NVENC", "h264_nvenc"), ("VAAPI", "h264_vaapi"), ("QSV", "h264_qsv"), ("libx264", "libx264")]
_BUFFER_RANGE = (10, 300)
_BITRATE_RANGE_MBPS = (2, 20)
_RETENTION_RANGE_DAYS = (0, 90)
_VOLUME_RANGE_PERCENT = (0, 200)
_QUICK_SAVE_SECONDS_RANGE = (5, 300)
_SAVED_MESSAGE_CLEAR_MS = 2500
# How long after the last field change the autosave fires. Long enough that
# spinbox arrow repeats and line-edit typing coalesce into one apply, short
# enough that a change feels immediate.
_AUTOSAVE_DEBOUNCE_MS = 500

_FRAMERATE_CHOICES = (15, 24, 30, 60)
_RESOLUTION_SCALE_CHOICES = [("Native", "native"), ("1080p", "1080p"), ("720p", "720p")]
_SIZE_CAP_RANGE_GB = (0, 50)

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
_THEME_MODE_LABELS = {"system": "System", "light": "Light", "dark": "Dark"}
_THEME_MODES = {label: mode for mode, label in _THEME_MODE_LABELS.items()}
_NO_WINDOW_SELECTED = "(none selected)"
_NO_WINDOWS_FOUND = "(no windows found)"

# The Logs sub-tab's tail poll -- the same 2s cadence the old top-level Logs
# page ran (the timer lives on the LogsTab widget, see below).
_LOG_TAIL_POLL_MS = 2000
# Higher than the old 200 now that the search/level filters cut the visible
# volume down -- a filtered view should still reach reasonably far back.
_LOG_TAIL_LINES = 500
_LOG_LEVEL_CHOICES = ("All", "INFO", "WARNING", "ERROR")

# The Settings-editable Config fields, i.e. exactly the keys of the
# _build_payload dict -- the reset-to-defaults payload is built from the same
# list so the two can never drift apart.
_PAYLOAD_FIELD_NAMES = (
    "buffer_seconds",
    "clips_dir",
    "hotkey_combo",
    "video_bitrate",
    "quality_preset",
    "capture_mode",
    "monitor_index",
    "window_title",
    "mic_device",
    "desktop_volume",
    "mic_volume",
    "encoder_override",
    "filename_template",
    "clip_retention_days",
    "launch_on_startup",
    "check_for_updates",
    "theme_mode",
    "framerate",
    "resolution_scale",
    "quick_save_hotkey_1",
    "quick_save_seconds_1",
    "quick_save_hotkey_2",
    "quick_save_seconds_2",
    "screenshot_hotkey",
    "clips_max_gb",
    "save_sound_enabled",
)


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


def _format_size_cap_label(gb: int) -> str:
    return "Unlimited" if gb <= 0 else f"{gb} GB"


def _clamped(value: int, bounds: tuple[int, int]) -> int:
    """What's shown must be what's saved: an out-of-range config value
    (e.g. --buffer-seconds 600 against the slider's 10-300) is clamped
    HERE, for the value label and the slider alike -- not left for Qt's
    setValue to clamp only the slider, which would display the original
    number while the next apply silently persists the clamp.
    """
    low, high = bounds
    return max(low, min(high, value))


def default_settings_payload() -> dict:
    """The Reset-to-defaults payload: every Settings-editable field at its
    hardcoded Config default. Read from the dataclass's field metadata rather
    than by instantiating Config() -- Config.__post_init__ mkdirs clips_dir
    and a temp buffer_dir, which answering "what are the defaults?" must not
    do. Keyed by _PAYLOAD_FIELD_NAMES so it carries exactly what
    _build_payload would.
    """
    defaults: dict = {}
    for field_info in dataclasses.fields(Config):
        if field_info.name not in _PAYLOAD_FIELD_NAMES:
            continue
        if field_info.default is not dataclasses.MISSING:
            value = field_info.default
        else:
            value = field_info.default_factory()
        defaults[field_info.name] = value
    # The payload carries the clips folder as a string, like _build_payload's.
    defaults["clips_dir"] = str(defaults["clips_dir"])
    return defaults


class LogsTab(QWidget):
    """The log viewer, living as the Settings tab widget's LAST sub-tab
    (after About) -- regular users shouldn't see Logs as a top-level
    destination, so the old MainWindow Logs page moved here wholesale. All
    of its features came along unchanged: the 2s tail poll, the search and
    level filters, the auto-scroll toggle, Copy, and Export diagnostics
    with its status label. The only deliberate loss is the old H1 page
    header -- the settings tab bar carries the "Logs" name now.

    Unlike the field tabs this page is NOT wrapped in a QScrollArea: its
    textbox fills the tab and scrolls itself, exactly like the old page.

    Owns its own poll timer, parented to itself, so a Settings
    reset-rebuild (which swaps the whole QTabWidget) retires the old timer
    with the old page and starts a fresh one with the new.
    """

    def __init__(
        self,
        config: Config,
        log_path: Path,
        diagnostics_facts_provider: Callable[[], dict[str, str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._log_path = log_path
        # Live system facts for the diagnostics zip (OS, session, ffmpeg
        # version, monitors, ...) -- a provider because the encoder can
        # change via apply_settings after the window is built.
        self._diagnostics_facts_provider = diagnostics_facts_provider

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        layout.addLayout(controls)
        self._log_search_edit = QLineEdit(self)
        self._log_search_edit.setPlaceholderText("Search logs...")
        self._log_search_edit.textChanged.connect(self._refresh_log_tail)
        controls.addWidget(self._log_search_edit, 1)
        self._log_level_combo = QComboBox(self)
        self._log_level_combo.addItems(_LOG_LEVEL_CHOICES)
        self._log_level_combo.currentTextChanged.connect(self._refresh_log_tail)
        controls.addWidget(self._log_level_combo)
        autoscroll_label = QLabel("Auto-scroll", self)
        autoscroll_label.setObjectName("hint")
        controls.addWidget(autoscroll_label)
        self._log_autoscroll_switch = ToggleSwitch(self, checked=True)
        controls.addWidget(self._log_autoscroll_switch)
        copy_button = QPushButton("Copy", self)
        copy_button.clicked.connect(self._on_copy_logs)
        controls.addWidget(copy_button)

        # A second, right-aligned row for the two file actions: with the
        # sidebar and the settings pane eating width, one row for all six
        # controls clipped the Export button at the 1000px window minimum.
        actions = QHBoxLayout()
        layout.addLayout(actions)
        actions.addStretch()
        export_button = QPushButton("Export diagnostics...", self)
        export_button.clicked.connect(self._on_export_diagnostics)
        actions.addWidget(export_button)
        open_log_folder_button = QPushButton("Open log folder", self)
        open_log_folder_button.clicked.connect(lambda: open_folder(self._log_path.parent))
        actions.addWidget(open_log_folder_button)

        self._log_textbox = QPlainTextEdit(self)
        self._log_textbox.setReadOnly(True)
        self._log_textbox.setFont(_qfont(size=theme.FONT_MONO, weight="normal", mono=True))
        self._log_textbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._log_textbox, 1)

        # Export-diagnostics feedback, same state pattern as the Settings
        # footer's status label (#statusLabel[state=...]).
        self._diagnostics_status_label = QLabel("", self)
        self._diagnostics_status_label.setObjectName("statusLabel")
        self._diagnostics_status_label.setWordWrap(True)
        layout.addWidget(self._diagnostics_status_label)

        self._refresh_log_tail()

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._refresh_log_tail)
        self._log_timer.start(_LOG_TAIL_POLL_MS)

    def _log_line_matches(self, line: str, search: str, level: str) -> bool:
        if level != "All":
            # Log lines look like "2026-07-22 01:43:36,076 INFO clipersal.cli:
            # ..." -- the level is the third whitespace-separated token (the
            # asctime itself contains a space). Lines without one (traceback
            # continuations) only pass the "All" filter.
            parts = line.split()
            if len(parts) < 3 or parts[2] != level:
                return False
        if search and search.lower() not in line.lower():
            return False
        return True

    def _refresh_log_tail(self) -> None:
        search = self._log_search_edit.text() if hasattr(self, "_log_search_edit") else ""
        level = self._log_level_combo.currentText() if hasattr(self, "_log_level_combo") else "All"
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-_LOG_TAIL_LINES:]
            content = "".join(line for line in lines if self._log_line_matches(line, search, level))
            if not lines:
                content = "(log file is empty)"
            elif not content:
                content = "(no log lines match)"
        except OSError:
            content = f"(log file not found yet: {self._log_path})"
        scrollbar = self._log_textbox.verticalScrollBar()
        previous_value = scrollbar.value()
        self._log_textbox.setPlainText(content)
        if self._log_autoscroll_switch.isChecked():
            cursor = self._log_textbox.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._log_textbox.setTextCursor(cursor)
        else:
            # With auto-scroll off a refresh must not yank the view back to
            # wherever the new content's cursor landed -- keep the position.
            scrollbar.setValue(previous_value)

    def _on_copy_logs(self) -> None:
        # The textbox already holds exactly the filtered view, so copying it
        # copies what the user actually sees.
        QGuiApplication.clipboard().setText(self._log_textbox.toPlainText())

    def _set_diagnostics_status(self, text: str, state: str) -> None:
        self._diagnostics_status_label.setText(text)
        self._diagnostics_status_label.setProperty("state", state)
        style = self._diagnostics_status_label.style()
        style.unpolish(self._diagnostics_status_label)
        style.polish(self._diagnostics_status_label)

    def export_diagnostics_with_dialog(self) -> Path | None:
        """Save-dialog + zip export, shared by this tab's "Export
        diagnostics..." button and the main window's crash-report prompt
        ("Export zip", via MainWindow._export_diagnostics_with_dialog) so
        the flow lives in exactly one place. The outcome is reported on
        this tab's status label either way. Returns the exported path, or
        None when the user cancelled or the export failed.
        """
        from PySide6.QtWidgets import QFileDialog

        target, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export diagnostics",
            str(Path.home() / "clipersal-diagnostics.zip"),
            "Zip files (*.zip)",
        )
        if not target:
            return None
        facts: dict[str, str] = {}
        if self._diagnostics_facts_provider is not None:
            try:
                facts = self._diagnostics_facts_provider()
            except Exception:  # noqa: BLE001 -- facts are best-effort; the zip matters more
                log.exception("Diagnostics facts provider failed; exporting without system facts")
        result = diagnostics.export_diagnostics_zip(
            Path(target),
            self._log_path,
            config_store.default_config_path(),
            self._config.buffer_dir,
            facts,
        )
        if result is None:
            self._set_diagnostics_status("Export failed -- see the log for details.", "error")
            return None
        self._set_diagnostics_status(f"Diagnostics exported to {result}", "success")
        return result

    def _on_export_diagnostics(self) -> None:
        # The tab's own button; the flow itself is shared with the main
        # window's crash-report prompt (see export_diagnostics_with_dialog).
        self.export_diagnostics_with_dialog()


class SettingsFrame(QWidget):
    """on_apply(new_values) is called -- debounced, on every settled field
    change -- with the validated field values; it should live-apply what it
    can, persist to the config file, and return None on success or an error
    string to display on failure. A failed apply rolls every control back to
    the last-known-good payload.

    on_update_found(version, url), when given, is the main window's
    show_update_banner -- the "Check now" button raises the Home tab's
    banner through it when a newer release turns up.
    """

    # ("update", (version, url)) / ("ok", None) / ("failed", None) from the
    # check-now worker thread -- the network call must stay off the GUI
    # thread, so the result comes back through this queued signal (the same
    # cross-thread rule as signals.py's AppSignals).
    check_now_responded = Signal(object)

    def __init__(
        self,
        config: Config,
        ipc_port: int,
        save_events,
        current_encoder: str,
        on_apply: Callable[[dict], str | None],
        ffmpeg_path: str,
        parent: QWidget | None = None,
        on_update_found: Callable[[str, str], None] | None = None,
        log_path: Path | None = None,
        diagnostics_facts_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._on_apply = on_apply
        self._on_update_found = on_update_found
        self._ffmpeg_path = ffmpeg_path
        self._current_encoder = current_encoder
        # The Logs sub-tab's inputs: the app's log file (the same
        # config_store.default_log_path() cli.py configures logging into --
        # injectable so tests stay hermetic) and the live system-facts
        # provider for its diagnostics zip.
        self._log_path = log_path if log_path is not None else config_store.default_log_path()
        self._diagnostics_facts_provider = diagnostics_facts_provider
        self.check_now_responded.connect(self._on_check_now_responded)
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

        # Autosave machinery -- created BEFORE the tabs are built so the
        # per-field signal wiring (connected at the end of _build_tabs, once
        # every initial value is in place) always has a timer to restart.
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(_AUTOSAVE_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self._apply_now)
        # on_apply runs synchronously on the GUI thread and can be slow (a
        # capture restart smoke-encodes). Changes landing mid-apply only mark
        # _dirty and get exactly one follow-up pass when it returns.
        self._applying = False
        self._dirty = False
        # The last successfully applied payload -- a failed apply rolls every
        # control back to it. Filled from the initial fields at the end of
        # __init__; construction itself never applies.
        self._last_good: dict = {}

        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(18, 18, 18, 18)
        self._outer_layout.setSpacing(8)

        # The page title, matching Home ("Home") and Clips ("Clips") -- H1
        # per theme.py's typography rules.
        title_label = QLabel("Settings", self)
        title_label.setFont(_qfont(size=theme.FONT_H1))
        self._outer_layout.addWidget(title_label)

        self.tabs = QTabWidget(self)
        self._outer_layout.addWidget(self.tabs, 1)
        self._build_tabs(config)

        footer = QHBoxLayout()
        self._outer_layout.addLayout(footer)

        # Plain (non-primary) button: resetting SETTINGS touches no files and
        # every field simply autosaves again afterwards, so it gets neither
        # the primary accent nor the destructive #danger look.
        self.reset_button = QPushButton("Reset to defaults", self)
        self.reset_button.setToolTip("Restore every setting to its factory default")
        self.reset_button.clicked.connect(self._on_reset_to_defaults)
        footer.addWidget(self.reset_button)

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)

        autosave_hint = QLabel("Changes save automatically.", self)
        autosave_hint.setObjectName("hint")
        footer.addWidget(autosave_hint)

        view_logs_button = QPushButton("View logs", self)
        # Jumps to the Logs sub-tab at the end of the tab widget (the log
        # viewer moved here from its old top-level page).
        view_logs_button.clicked.connect(lambda: self.select_subtab("logs"))
        footer.addWidget(view_logs_button)

        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(lambda: self.status_label.setText(""))

        # The initial last-known-good snapshot. Doubles as construction-time
        # proof the fields can produce a payload at all: a broken config
        # (e.g. an emptied hotkey) leaves it empty, in which case a failed
        # apply simply has nothing to roll back to. No apply is fired here --
        # building the window must be inert.
        self._last_good = self._build_payload() or {}

    # ---- tab scaffolding ---------------------------------------------------

    def _make_tab(self, title: str) -> QVBoxLayout:
        """One scrollable tab page; returns the page's content layout. The
        page widget itself stays transparent -- the pane and the cards paint
        the surfaces (see build_stylesheet's scoped-background rule)."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        scroll.setWidget(page)
        # QScrollArea.setWidget flips the page's autoFillBackground ON, which
        # fills it with the UNTHEMED palette Window grey in both modes -- the
        # "rogue dark background" report. Turn it back off; the page stays
        # transparent so the pane/cards paint the surfaces.
        page.setAutoFillBackground(False)
        self.tabs.addTab(scroll, title)
        return layout

    def _build_tabs(self, config) -> None:
        """Populate self.tabs. `config` is the live Config on first build and
        a SimpleNamespace of the just-applied payload on a reset rebuild --
        the build methods only ever read attributes, so both fit (and
        Config() itself must not be instantiated for a rebuild: its
        __post_init__ mkdirs).
        """
        capture_tab = self._make_tab("Capture")
        self._build_capture_card(capture_tab, config, self._ffmpeg_path)
        self._build_audio_card(capture_tab, config, self._ffmpeg_path)
        capture_tab.addStretch()

        saving_tab = self._make_tab("Saving")
        self._build_save_hotkey_card(saving_tab, config)
        saving_tab.addStretch()

        encoder_tab = self._make_tab("Encoder")
        self._build_encoder_card(encoder_tab, config, self._current_encoder)
        self._build_quality_card(encoder_tab, config)
        encoder_tab.addStretch()

        clips_tab = self._make_tab("Clips")
        self._build_clip_management_card(clips_tab, config)
        clips_tab.addStretch()

        appearance_tab = self._make_tab("Appearance")
        self._build_appearance_card(appearance_tab, config)
        appearance_tab.addStretch()

        about_tab = self._make_tab("About")
        self._build_about_card(about_tab)
        self._build_update_card(about_tab, config)
        about_tab.addStretch()

        # Logs comes LAST and is added directly (not via _make_tab): its
        # textbox fills the page and scrolls itself, so a scroll wrapper
        # would only nest a second scrollbar. It always reads the LIVE
        # config -- on a reset rebuild `config` here is a SimpleNamespace of
        # the payload, which has no buffer_dir for the diagnostics zip.
        self.logs_tab = LogsTab(
            self._config,
            self._log_path,
            diagnostics_facts_provider=self._diagnostics_facts_provider,
        )
        self.tabs.addTab(self.logs_tab, "Logs")

        # Wired LAST, once every field holds its initial value: the
        # population above fires most of these very signals, so connecting
        # any earlier would schedule applies off the constructor. _reload()
        # re-wires its fresh widgets through this same call.
        self._connect_autosave()

    def _reload(self, values: dict) -> None:
        """Rebuild the tab pages from `values` (the just-applied reset
        payload) so every field shows the value now in effect. Rebuilding
        reuses the exact build path -- including its clamping and probes --
        rather than a second set-every-widget pass that could drift out of
        sync with the first.
        """
        old_tabs = self.tabs
        new_tabs = QTabWidget(self)
        # replaceWidget() needs the old widget visible, and this frame sits
        # hidden inside the main window's QStackedWidget half the time -- so
        # swap manually (remove + insert at the same index).
        index = self._outer_layout.indexOf(old_tabs)
        self._outer_layout.removeWidget(old_tabs)
        old_tabs.hide()
        old_tabs.deleteLater()
        self.tabs = new_tabs
        self._outer_layout.insertWidget(index, new_tabs, 1)
        self._build_tabs(SimpleNamespace(**values))

    # ---- sub-tab routing + diagnostics --------------------------------------

    def select_subtab(self, name: str) -> None:
        """Switch to the sub-tab whose label matches `name`
        (case-insensitive, e.g. "logs"). MainWindow routes every legacy
        "show me the logs" entry point here via select_settings_subtab."""
        wanted = name.lower()
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).lower() == wanted:
                self.tabs.setCurrentIndex(i)
                return

    def export_diagnostics_with_dialog(self) -> Path | None:
        """The diagnostics zip flow lives on the Logs sub-tab; MainWindow's
        crash-report prompt ("Export zip") reaches it through here so both
        entry points share the one implementation."""
        return self.logs_tab.export_diagnostics_with_dialog()

    # ---- small layout helpers -------------------------------------------

    def _make_card(self, parent_layout: QVBoxLayout, title: str | None) -> QVBoxLayout:
        card = QFrame(self)
        card.setObjectName("card")
        parent_layout.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(6)
        if title is not None:
            title_label = QLabel(title.upper(), card)
            title_label.setObjectName("cardTitle")
            card_layout.addWidget(title_label)
        return card_layout

    def _field_label(self, text: str, parent: QWidget) -> QLabel:
        """A field name label -- plain BODY text (theme.py's typography
        rules: bold is reserved for titles, the status word, and clip names)."""
        return QLabel(text, parent)

    def _field_row(self, card_layout: QVBoxLayout, label_text: str, value_text: str, parent: QWidget) -> QLabel:
        row = QHBoxLayout()
        card_layout.addLayout(row)
        row.addWidget(self._field_label(label_text, parent))
        row.addStretch()
        value_label = QLabel(value_text, parent)
        value_label.setObjectName("valueBadge")
        # The badge is a code-ish readout ("60s", "100%") -- MONO per
        # theme.py's typography rules, never bold.
        value_label.setFont(_qfont(size=theme.FONT_MONO, weight="normal", mono=True))
        row.addWidget(value_label)
        return value_label

    def _hint(self, card_layout: QVBoxLayout, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("hint")
        label.setWordWrap(True)
        card_layout.addWidget(label)
        return label

    # ---- Capture tab ----------------------------------------------------

    def _build_capture_card(self, tab_layout: QVBoxLayout, config: Config, ffmpeg_path: str) -> None:
        capture_layout = self._make_card(tab_layout, "Capture")
        card = capture_layout.parentWidget()

        initial_buffer = _clamped(config.buffer_seconds, _BUFFER_RANGE)
        self.buffer_value_label = self._field_row(capture_layout, "Buffer length", f"{initial_buffer}s", card)
        self.buffer_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.buffer_slider.setRange(*_BUFFER_RANGE)
        self.buffer_slider.setValue(initial_buffer)
        self.buffer_slider.valueChanged.connect(lambda v: self.buffer_value_label.setText(f"{v}s"))
        capture_layout.addWidget(self.buffer_slider)
        self._hint(capture_layout, "How much history stays in the rolling buffer (10s–300s)", card)

        self._build_capture_target(capture_layout, config, card)
        self._build_framerate_and_scale(capture_layout, config, card)

    def _build_audio_card(self, tab_layout: QVBoxLayout, config: Config, ffmpeg_path: str) -> None:
        audio_layout = self._make_card(tab_layout, "Audio")
        card = audio_layout.parentWidget()
        self._build_microphone_picker(audio_layout, config, ffmpeg_path, card)
        self._build_volume_controls(audio_layout, config, ffmpeg_path, card)

    def _build_framerate_and_scale(self, capture_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        capture_layout.addWidget(self._field_label("Frame rate", card))
        self.framerate_combo = QComboBox(card)
        for fps in _FRAMERATE_CHOICES:
            self.framerate_combo.addItem(str(fps), fps)
        initial_fps = self.framerate_combo.findData(config.framerate)
        # A hand-edited config with a non-choice framerate falls back to the
        # 30 fps default rather than a blank combo.
        self.framerate_combo.setCurrentIndex(initial_fps if initial_fps >= 0 else self.framerate_combo.findData(30))
        capture_layout.addWidget(self.framerate_combo)
        self._hint(capture_layout, "Frames per second captured (applies automatically; restarts capture)", card)

        capture_layout.addWidget(self._field_label("Resolution scale", card))
        self.resolution_scale_combo = QComboBox(card)
        for label, value in _RESOLUTION_SCALE_CHOICES:
            self.resolution_scale_combo.addItem(label, value)
        initial_scale = self.resolution_scale_combo.findData(config.resolution_scale)
        self.resolution_scale_combo.setCurrentIndex(initial_scale if initial_scale >= 0 else 0)
        capture_layout.addWidget(self.resolution_scale_combo)
        self._hint(capture_layout, "Downscale the saved video -- Native keeps the capture resolution", card)

    def _build_capture_target(self, capture_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        detected_monitors = monitors.list_monitors(self._os)

        capture_layout.addWidget(self._field_label("Capture target", card))

        target_choices = ["Desktop"]
        if len(detected_monitors) > 1:
            target_choices.append("Monitor")
        target_choices.append("Window")

        self.target_control = SegmentedControl(target_choices, card)
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
        # Repopulating fires currentIndexChanged -- block it so a Refresh
        # click (or the build-time call) never schedules an autosave by
        # itself. Only a genuine user pick should.
        self.window_combo.blockSignals(True)
        try:
            self.window_combo.clear()
            self.window_combo.addItems(titles)
            if current in titles:
                self.window_combo.setCurrentText(current)
            else:
                self.window_combo.setCurrentIndex(0)
        finally:
            self.window_combo.blockSignals(False)

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
            capture_layout.addWidget(self._field_label("Microphone", card))
            self.mic_combo = QComboBox(card)
            self.mic_combo.addItems(["None", *mic_names])
            initial_mic = config.mic_device if config.mic_device in mic_names else "None"
            self.mic_combo.setCurrentText(initial_mic)
            capture_layout.addWidget(self.mic_combo)
            self._hint(capture_layout, "Mixed in alongside system audio, if a loopback source is available", card)

    def _build_volume_controls(self, capture_layout: QVBoxLayout, config: Config, ffmpeg_path: str, card: QWidget) -> None:
        initial_desktop_volume = _clamped(config.desktop_volume, _VOLUME_RANGE_PERCENT)
        self.desktop_volume_value_label = self._field_row(
            capture_layout, "Desktop volume", f"{initial_desktop_volume}%", card
        )
        self.desktop_volume_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.desktop_volume_slider.setRange(*_VOLUME_RANGE_PERCENT)
        self.desktop_volume_slider.setValue(initial_desktop_volume)
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

        initial_mic_volume = _clamped(config.mic_volume, _VOLUME_RANGE_PERCENT)
        self.mic_volume_value_label = self._field_row(
            capture_layout, "Microphone volume", f"{initial_mic_volume}%", card
        )
        self.mic_volume_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.mic_volume_slider.setRange(*_VOLUME_RANGE_PERCENT)
        self.mic_volume_slider.setValue(initial_mic_volume)
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

    def _build_quality_card(self, tab_layout: QVBoxLayout, config: Config) -> None:
        quality_layout = self._make_card(tab_layout, "Quality")
        card = quality_layout.parentWidget()
        self._build_quality_preset(quality_layout, config, card)

    def _build_quality_preset(self, capture_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        capture_layout.addWidget(self._field_label("Quality preset", card))

        self.preset_control = SegmentedControl([label for label, _ in _QUALITY_PRESET_CHOICES], card)
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

    # ---- Saving tab ------------------------------------------------------

    def _build_save_hotkey_card(self, tab_layout: QVBoxLayout, config: Config) -> None:
        save_layout = self._make_card(tab_layout, None)
        card = save_layout.parentWidget()

        save_layout.addWidget(self._field_label("Clips folder", card))
        clips_row = QHBoxLayout()
        save_layout.addLayout(clips_row)
        self.clips_dir_edit = QLineEdit(str(config.clips_dir), card)
        self.clips_dir_edit.setReadOnly(True)
        clips_row.addWidget(self.clips_dir_edit, 1)
        browse_button = QPushButton("Browse...", card)
        browse_button.clicked.connect(self._browse_clips_dir)
        clips_row.addWidget(browse_button)

        save_layout.addWidget(self._field_label("Hotkey", card))
        self.hotkey_field = HotkeyField(config.hotkey_combo, card)
        save_layout.addWidget(self.hotkey_field)
        self._hint(
            save_layout, "Click Record and press a key combo, or type pynput format directly, e.g. <ctrl>+<alt>+r", card
        )

        self.quick_save_hotkey_1_field, self.quick_save_seconds_1_spin = self._build_quick_save_row(
            save_layout, "Quick-save hotkey 1", config.quick_save_hotkey_1, config.quick_save_seconds_1, card
        )
        self.quick_save_hotkey_2_field, self.quick_save_seconds_2_spin = self._build_quick_save_row(
            save_layout, "Quick-save hotkey 2", config.quick_save_hotkey_2, config.quick_save_seconds_2, card
        )

        save_layout.addWidget(self._field_label("Screenshot hotkey", card))
        self.screenshot_hotkey_field = HotkeyField(config.screenshot_hotkey, card)
        save_layout.addWidget(self.screenshot_hotkey_field)
        self._hint(save_layout, "Grabs a still frame from the buffer -- leave empty to disable", card)

        # Same toggle-row shape as the launch-on-startup row below;
        # cli.py's toast path plays the beep.
        sound_row = QHBoxLayout()
        save_layout.addLayout(sound_row)
        sound_text_col = QVBoxLayout()
        sound_row.addLayout(sound_text_col, 1)
        sound_text_col.addWidget(self._field_label("Play a sound when a clip is saved", card))
        sound_desc = QLabel("Short system beep on every successful save", card)
        sound_desc.setObjectName("hint")
        sound_text_col.addWidget(sound_desc)
        self.save_sound_switch = ToggleSwitch(card, checked=config.save_sound_enabled)
        sound_row.addWidget(self.save_sound_switch)

        self._build_startup_row(save_layout, config, card)

    def _build_quick_save_row(
        self, save_layout: QVBoxLayout, label: str, combo: str, seconds: int, card: QWidget
    ) -> tuple[HotkeyField, StepperSpinBox]:
        """One HotkeyField + seconds spinbox row for a "save just the last N
        seconds" binding. An empty combo means disabled (apply_settings
        skips validation and pynput binding for it); the spinbox range
        matches apply_settings' 5-300s clamp.
        """
        save_layout.addWidget(self._field_label(label, card))
        row = QHBoxLayout()
        save_layout.addLayout(row)
        field = HotkeyField(combo, card)
        row.addWidget(field, 1)
        spin = StepperSpinBox(card)
        spin.setRange(*_QUICK_SAVE_SECONDS_RANGE)
        spin.setSuffix(" s")
        spin.setValue(max(_QUICK_SAVE_SECONDS_RANGE[0], min(_QUICK_SAVE_SECONDS_RANGE[1], seconds)))
        row.addWidget(spin)
        self._hint(save_layout, "One-tap save of just the last N seconds -- leave empty to disable", card)
        return field, spin

    def _build_startup_row(self, save_layout: QVBoxLayout, config: Config, card: QWidget) -> None:
        startup_supported = autostart.is_supported(self._os)
        startup_checked = config.launch_on_startup
        if startup_supported:
            try:
                # Reconcile the toggle with reality at build time: the OS
                # registration is the source of truth, so a Run value /
                # .desktop file deleted outside the app (or a registration
                # that failed last apply) doesn't leave the toggle
                # permanently showing what we merely BELIEVE -- and an
                # unchanged toggle position never re-triggers registration.
                startup_checked = autostart.is_enabled(self._os)
            except Exception as exc:  # noqa: BLE001 -- a probe hiccup must never break the Settings tab
                # Best-effort like every other probe in this codebase: fall
                # back to the persisted value when the OS can't answer.
                log.warning("Could not probe launch-on-startup registration (%s); using the configured value", exc)
        startup_row = QHBoxLayout()
        save_layout.addLayout(startup_row)
        startup_text_col = QVBoxLayout()
        startup_row.addLayout(startup_text_col, 1)
        startup_text_col.addWidget(self._field_label("Launch on startup", card))
        startup_desc = QLabel(
            "Start automatically when you log in" if startup_supported else "Not supported on this platform yet", card
        )
        startup_desc.setObjectName("hint")
        startup_text_col.addWidget(startup_desc)
        self.launch_on_startup_switch = ToggleSwitch(card, checked=(startup_supported and startup_checked))
        startup_row.addWidget(self.launch_on_startup_switch)
        if not startup_supported:
            self.launch_on_startup_switch.setEnabled(False)

    # ---- About tab (app info + the update-check machinery) -----------------

    def _build_about_card(self, tab_layout: QVBoxLayout) -> None:
        about_layout = self._make_card(tab_layout, "About Clipersal")
        card = about_layout.parentWidget()

        name_label = QLabel("Clipersal", card)
        name_label.setFont(_qfont(size=theme.FONT_H1))
        about_layout.addWidget(name_label)
        tagline_label = QLabel("Catch the moment you bloomed.", card)
        tagline_label.setObjectName("hint")
        about_layout.addWidget(tagline_label)

        self.about_version_label = QLabel(f"Version {__version__}", card)
        self.about_version_label.setObjectName("hint")
        about_layout.addWidget(self.about_version_label)
        # The license change itself lands separately -- this is just the label.
        license_label = QLabel("License: GPL-3.0", card)
        license_label.setObjectName("hint")
        about_layout.addWidget(license_label)

        github_row = QHBoxLayout()
        about_layout.addLayout(github_row)
        self.github_button = QPushButton("View on GitHub", card)
        self.github_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{update_check.GITHUB_REPO}"))
        )
        github_row.addWidget(self.github_button)
        github_hint = QLabel(f"github.com/{update_check.GITHUB_REPO}", card)
        github_hint.setObjectName("hint")
        github_row.addWidget(github_hint, 1)

    def _build_update_card(self, tab_layout: QVBoxLayout, config: Config) -> None:
        update_layout = self._make_card(tab_layout, "Updates")
        card = update_layout.parentWidget()

        update_row = QHBoxLayout()
        update_layout.addLayout(update_row)
        update_text_col = QVBoxLayout()
        update_row.addLayout(update_text_col, 1)
        update_text_col.addWidget(self._field_label("Check for updates automatically", card))
        update_desc = QLabel("Check GitHub for a newer version once at startup", card)
        update_desc.setObjectName("hint")
        update_text_col.addWidget(update_desc)
        self.update_last_checked_label = QLabel(self._last_checked_text(), card)
        self.update_last_checked_label.setObjectName("hint")
        update_text_col.addWidget(self.update_last_checked_label)
        self.check_now_button = QPushButton("Check now", card)
        self.check_now_button.clicked.connect(self._on_check_now)
        update_row.addWidget(self.check_now_button)
        self.check_for_updates_switch = ToggleSwitch(card, checked=config.check_for_updates)
        update_row.addWidget(self.check_for_updates_switch)

    def _last_checked_text(self) -> str:
        try:
            last_checked = update_check.load_cache().get("last_checked")
        except Exception:  # noqa: BLE001 -- a cache hiccup must never break the Settings tab
            last_checked = None
        if not isinstance(last_checked, (int, float)):
            return "Last checked: never"
        return f"Last checked: {datetime.fromtimestamp(last_checked).strftime('%Y-%m-%d %H:%M')}"

    # ---- check-now (manual update check) -------------------------------------

    def _on_check_now(self) -> None:
        self._status_clear_timer.stop()
        # One check at a time -- the button is re-enabled when the worker's
        # answer comes back through check_now_responded.
        self.check_now_button.setEnabled(False)
        self._set_status("Checking for updates...", "")
        threading.Thread(target=self._check_now_worker, daemon=True).start()

    def _check_now_worker(self) -> None:
        # force=True bypasses the 24h throttle -- the user explicitly asked.
        # check_for_update_once never raises and returns None for BOTH "no
        # newer release" and "fetch failed"; the two are told apart via the
        # cache's last_checked stamp, which is only written by a completed
        # (i.e. successfully fetched) check.
        try:
            checked_before = update_check.load_cache().get("last_checked")
            result = update_check.check_for_update_once(
                repo=update_check.GITHUB_REPO,
                current_version=__version__,
                force=True,
            )
            checked_after = update_check.load_cache().get("last_checked")
            if result is not None:
                message = ("update", result)
            elif isinstance(checked_after, (int, float)) and checked_after != checked_before:
                message = ("ok", None)
            else:
                message = ("failed", None)
        except Exception:  # noqa: BLE001 -- a manual check must never crash the Settings tab
            log.exception("Manual update check failed")
            message = ("failed", None)
        self.check_now_responded.emit(message)

    def _on_check_now_responded(self, message) -> None:
        self.check_now_button.setEnabled(True)
        self.update_last_checked_label.setText(self._last_checked_text())
        kind, payload = message
        if kind == "update":
            version, url = payload
            if self._on_update_found is not None:
                self._on_update_found(version, url)
            self._set_status(f"Clipersal {version} is available -- see the banner on the Home tab.", "success")
        elif kind == "ok":
            self._set_status("You're up to date.", "success")
        else:
            self._set_status("Check failed (network?)", "error")

    def _browse_clips_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(
            self, "Choose clips folder", self.clips_dir_edit.text() or str(Path.home())
        )
        if chosen:
            self.clips_dir_edit.setText(chosen)
            # The entry is read-only, so this is the one path that changes
            # it -- treat a confirmed pick like any other field edit.
            self._schedule_apply()

    # ---- Encoder tab ------------------------------------------------------

    def _build_encoder_card(self, tab_layout: QVBoxLayout, config: Config, current_encoder: str) -> None:
        encoder_layout = self._make_card(tab_layout, "Encoder")
        card = encoder_layout.parentWidget()

        autodetect_row = QHBoxLayout()
        encoder_layout.addLayout(autodetect_row)
        autodetect_text_col = QVBoxLayout()
        autodetect_row.addLayout(autodetect_text_col, 1)
        autodetect_text_col.addWidget(self._field_label("Auto-detect", card))
        autodetect_desc = QLabel("Picks NVENC / VAAPI / QSV, falling back to libx264", card)
        autodetect_desc.setObjectName("hint")
        autodetect_desc.setWordWrap(True)
        autodetect_text_col.addWidget(autodetect_desc)

        initial_manual_label = self._encoder_label_by_value.get(config.encoder_override or current_encoder, "libx264")
        self.encoder_control = SegmentedControl([label for label, _ in _ENCODER_CHOICES], card)
        self.encoder_control.setCurrent(initial_manual_label)
        encoder_layout.addWidget(self.encoder_control)

        self.autodetect_switch = ToggleSwitch(card, checked=(config.encoder_override is None))
        autodetect_row.addWidget(self.autodetect_switch)
        self.autodetect_switch.toggled.connect(self._on_autodetect_toggle)
        self._on_autodetect_toggle(config.encoder_override is None)

    def _on_autodetect_toggle(self, enabled: bool) -> None:
        self.encoder_control.setEnabled(not enabled)

    # ---- Clips tab --------------------------------------------------------

    def _build_clip_management_card(self, tab_layout: QVBoxLayout, config: Config) -> None:
        clips_layout = self._make_card(tab_layout, None)
        card = clips_layout.parentWidget()

        clips_layout.addWidget(self._field_label("Filename template", card))
        self.filename_template_edit = QLineEdit(config.filename_template, card)
        clips_layout.addWidget(self.filename_template_edit)
        self._hint(clips_layout, "Placeholders: {date}, {time}, {datetime}, {window} (active window title)", card)

        initial_retention = _clamped(config.clip_retention_days, _RETENTION_RANGE_DAYS)
        self.retention_value_label = self._field_row(
            clips_layout, "Keep clips for", _format_retention_label(initial_retention), card
        )
        self.retention_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.retention_slider.setRange(*_RETENTION_RANGE_DAYS)
        self.retention_slider.setValue(initial_retention)
        self.retention_slider.valueChanged.connect(
            lambda v: self.retention_value_label.setText(_format_retention_label(v))
        )
        clips_layout.addWidget(self.retention_slider)
        self._hint(clips_layout, "0 = keep forever; older clips are deleted automatically otherwise", card)

        # Same slider + value-badge shape as "Keep clips for" above; the
        # sweep itself (concat.enforce_size_cap) runs on save and on apply.
        initial_size_cap = _clamped(config.clips_max_gb, _SIZE_CAP_RANGE_GB)
        self.size_cap_value_label = self._field_row(
            clips_layout, "Max clips folder size", _format_size_cap_label(initial_size_cap), card
        )
        self.size_cap_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.size_cap_slider.setRange(*_SIZE_CAP_RANGE_GB)
        self.size_cap_slider.setValue(initial_size_cap)
        self.size_cap_slider.valueChanged.connect(
            lambda v: self.size_cap_value_label.setText(_format_size_cap_label(v))
        )
        clips_layout.addWidget(self.size_cap_slider)
        self._hint(
            clips_layout,
            "0 = Unlimited; past the cap the oldest clips are deleted -- favorites are always protected",
            card,
        )

    # ---- Appearance tab ---------------------------------------------------

    def _build_appearance_card(self, tab_layout: QVBoxLayout, config: Config) -> None:
        appearance_layout = self._make_card(tab_layout, None)
        card = appearance_layout.parentWidget()

        # Applying the flip live (no app restart) is cli.py's apply_settings
        # job via theme.apply_theme + the theme_changed signal -- from this
        # widget's side it's just another field in the autosave payload. An
        # unknown persisted mode (a hand-edited config) shows as "System",
        # and the next apply normalizes it to a real mode.
        appearance_layout.addWidget(self._field_label("Theme", card))
        self.theme_control = SegmentedControl(list(_THEME_MODES), card)
        self.theme_control.setCurrent(_THEME_MODE_LABELS.get(config.theme_mode, "System"))
        appearance_layout.addWidget(self.theme_control)
        self._hint(appearance_layout, "System follows your OS dark-mode setting", card)

    # ---- reset to defaults --------------------------------------------------

    def _on_reset_to_defaults(self) -> None:
        answer = quiet_message(
            self,
            "Reset to defaults",
            "Reset every setting to its factory default?\n\n"
            "This resets: buffer length, capture target, microphone and volumes, "
            "frame rate and resolution scale, quality preset and bitrate, encoder, "
            "all hotkeys, filename template, clip retention and folder size cap, "
            "save sound, theme (follows the system), launch on startup (turns off), "
            "and update checks (turn on).\n\n"
            "The clips folder also resets to the default:\n"
            f"{_default_clips_dir()}\n\n"
            "Your saved clips are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._status_clear_timer.stop()
        # A pending autosave would fire after the reload and re-apply
        # whatever the OLD fields held -- the reset supersedes it.
        self._apply_timer.stop()
        self._dirty = False
        payload = default_settings_payload()
        error = self._on_apply(payload)
        if error:
            self._set_status(error, "error")
            return
        # The reset payload becomes the new last-known-good BEFORE the
        # rebuild, so anything the fresh widgets report is compared against
        # the values actually in effect.
        self._last_good = payload
        self._reload(payload)
        self._set_status("Saved ✓", "success")
        self._status_clear_timer.start(_SAVED_MESSAGE_CLEAR_MS)

    # ---- autosave -------------------------------------------------------------

    def _set_status(self, text: str, state: str) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def _hotkey_fields(self) -> list[tuple[HotkeyField, str]]:
        """Every HotkeyField on the tab, paired with its payload key."""
        return [
            (self.hotkey_field, "hotkey_combo"),
            (self.quick_save_hotkey_1_field, "quick_save_hotkey_1"),
            (self.quick_save_hotkey_2_field, "quick_save_hotkey_2"),
            (self.screenshot_hotkey_field, "screenshot_hotkey"),
        ]

    def _connect_autosave(self) -> None:
        """Wire every field's settled-change signal to the debounced autosave.

        Signal choice per widget: settled values only -- a combo's
        currentIndexChanged, a segmented control's currentTextChanged, a
        switch's toggled, a spinbox's valueChanged (the debounce coalesces
        arrow-key repeats and typed digits), a line edit's editingFinished.
        Sliders use sliderReleased, NOT valueChanged: bitrate/buffer-class
        fields restart capture on apply, and valueChanged fires per pixel of
        drag. The "Check now" button is an action, not a field, and stays
        unwired.
        """
        for slider in (
            self.buffer_slider,
            self.desktop_volume_slider,
            self.mic_volume_slider,
            self.bitrate_slider,
            self.retention_slider,
            self.size_cap_slider,
        ):
            slider.sliderReleased.connect(self._schedule_apply)

        for combo in (self.framerate_combo, self.resolution_scale_combo, self.window_combo):
            combo.currentIndexChanged.connect(self._schedule_apply)
        if self.monitor_combo is not None:
            self.monitor_combo.currentIndexChanged.connect(self._schedule_apply)
        if self.mic_combo is not None:
            self.mic_combo.currentIndexChanged.connect(self._schedule_apply)

        for control in (self.target_control, self.preset_control, self.encoder_control, self.theme_control):
            control.currentTextChanged.connect(self._schedule_apply)

        for switch in (
            self.autodetect_switch,
            self.launch_on_startup_switch,
            self.check_for_updates_switch,
            self.save_sound_switch,
        ):
            switch.toggled.connect(self._schedule_apply)

        self.quick_save_seconds_1_spin.valueChanged.connect(self._schedule_apply)
        self.quick_save_seconds_2_spin.valueChanged.connect(self._schedule_apply)

        self.filename_template_edit.editingFinished.connect(self._on_template_editing_finished)
        for field, key in self._hotkey_fields():
            field.recording_finished.connect(lambda f=field, k=key: self._on_hotkey_field_settled(f, k))
            field.entry.editingFinished.connect(lambda f=field, k=key: self._on_hotkey_field_settled(f, k))

    def _schedule_apply(self) -> None:
        """(Re)start the debounce. Changes landing while an apply runs
        synchronously can't be serviced re-entrantly, so they just mark the
        dirty flag for one follow-up pass when the apply returns."""
        if self._applying:
            self._dirty = True
            return
        self._apply_timer.start()  # start() on a running single-shot timer restarts it

    def _on_template_editing_finished(self) -> None:
        # editingFinished also fires on a plain focus-out with no edit --
        # don't apply (and flash "Saved ✓") over nothing.
        if self.filename_template_edit.text().strip() != self._last_good.get("filename_template"):
            self._schedule_apply()

    def _on_hotkey_field_settled(self, field: HotkeyField, key: str) -> None:
        # Same no-change skip as the template field. It doubles as the
        # recorder's cancel path: a cancelled recording restores the
        # pre-record text, which equals the snapshot when nothing changed.
        if field.combo().strip() != self._last_good.get(key, ""):
            self._schedule_apply()

    def _apply_now(self) -> None:
        """The debounce fire: validate, apply, then commit or roll back."""
        if self._applying:  # re-entrant fire (an event pumped mid-apply)
            self._dirty = True
            return
        # Never read a hotkey field mid-record -- its entry shows "Press
        # keys..." or a half-captured combo, not a persistable value. Defer;
        # the recorder's recording_finished reschedules if the combo actually
        # changed, and this re-armed timer covers the cancel-unchanged case.
        if any(field.is_recording() for field, _key in self._hotkey_fields()):
            self._apply_timer.start()
            return
        self._status_clear_timer.stop()
        payload = self._build_payload()
        if payload is None:
            # A guard rejected the current values (the reason is already on
            # the status label). The field keeps its text -- the user may
            # still be mid-edit; the next settled change re-schedules.
            return
        self._applying = True
        self._dirty = False
        try:
            error = self._on_apply(payload)
        finally:
            self._applying = False
        if error:
            # Roll every control back to the last-known-good payload so the
            # fields show exactly what is in effect; whatever was touched
            # during the failed apply goes with it.
            self._dirty = False
            self._set_status(error, "error")
            self._restore_controls(self._last_good)
            return
        self._last_good = payload
        self._set_status("Saved ✓", "success")
        self._status_clear_timer.start(_SAVED_MESSAGE_CLEAR_MS)
        if self._dirty:
            self._dirty = False
            self._apply_timer.start()

    def _restore_controls(self, values: dict) -> None:
        """Roll every control back to the last-known-good payload after a
        failed apply. Signals are blocked per widget so the rollback can't
        re-trigger the autosave it undoes; the value labels and dependent
        panels those signals would have refreshed are updated by hand (the
        same handlers the build path primes after connecting).
        """
        if not values:
            return

        def set_blocked(widget, setter) -> None:
            widget.blockSignals(True)
            try:
                setter()
            finally:
                widget.blockSignals(False)

        set_blocked(self.buffer_slider, lambda: self.buffer_slider.setValue(values["buffer_seconds"]))
        self.buffer_value_label.setText(f"{values['buffer_seconds']}s")
        set_blocked(self.clips_dir_edit, lambda: self.clips_dir_edit.setText(values["clips_dir"]))
        for field, key in self._hotkey_fields():
            set_blocked(field.entry, lambda f=field, k=key: f.entry.setText(values[k]))
        set_blocked(
            self.quick_save_seconds_1_spin,
            lambda: self.quick_save_seconds_1_spin.setValue(values["quick_save_seconds_1"]),
        )
        set_blocked(
            self.quick_save_seconds_2_spin,
            lambda: self.quick_save_seconds_2_spin.setValue(values["quick_save_seconds_2"]),
        )

        preset_label_by_value = {value: label for label, value in _QUALITY_PRESET_CHOICES}
        # SegmentedControl.setCurrent never emits, so no blocking needed there.
        self.preset_control.setCurrent(preset_label_by_value.get(values["quality_preset"], "Custom"))
        self._on_preset_change()
        mbps = bitrate_string_to_mbps(values["video_bitrate"])
        set_blocked(self.bitrate_slider, lambda: self.bitrate_slider.setValue(mbps))
        self.bitrate_value_label.setText(f"{mbps} Mbps")

        self.target_control.setCurrent(_CAPTURE_TARGET_LABELS.get(values["capture_mode"], "Desktop"))
        self._on_target_change()
        if self.monitor_combo is not None:
            label_by_index = {index: label for label, index in self._monitor_index_by_label.items()}
            monitor_label = label_by_index.get(values["monitor_index"])
            if monitor_label is not None:
                set_blocked(self.monitor_combo, lambda: self.monitor_combo.setCurrentText(monitor_label))
        if values["capture_mode"] == "window":
            title = values["window_title"] or _NO_WINDOW_SELECTED
            if self.window_combo.findText(title) >= 0:
                set_blocked(self.window_combo, lambda: self.window_combo.setCurrentText(title))
        if self.mic_combo is not None:
            mic_label = values["mic_device"] or "None"
            if self.mic_combo.findText(mic_label) >= 0:
                set_blocked(self.mic_combo, lambda: self.mic_combo.setCurrentText(mic_label))
        self._on_mic_volume_state_change()
        set_blocked(self.desktop_volume_slider, lambda: self.desktop_volume_slider.setValue(values["desktop_volume"]))
        self.desktop_volume_value_label.setText(f"{values['desktop_volume']}%")
        set_blocked(self.mic_volume_slider, lambda: self.mic_volume_slider.setValue(values["mic_volume"]))
        self.mic_volume_value_label.setText(f"{values['mic_volume']}%")

        is_auto = values["encoder_override"] is None
        # The silent variant keeps the switch's own toggled->knob animation
        # from being swallowed by the signal block.
        self.autodetect_switch.set_checked_silently(is_auto)
        self.encoder_control.setCurrent(
            self._encoder_label_by_value.get(values["encoder_override"] or self._current_encoder, "libx264")
        )
        self._on_autodetect_toggle(is_auto)

        set_blocked(
            self.filename_template_edit,
            lambda: self.filename_template_edit.setText(values["filename_template"]),
        )
        set_blocked(self.retention_slider, lambda: self.retention_slider.setValue(values["clip_retention_days"]))
        self.retention_value_label.setText(_format_retention_label(values["clip_retention_days"]))
        set_blocked(self.size_cap_slider, lambda: self.size_cap_slider.setValue(values["clips_max_gb"]))
        self.size_cap_value_label.setText(_format_size_cap_label(values["clips_max_gb"]))
        self.launch_on_startup_switch.set_checked_silently(values["launch_on_startup"])
        self.check_for_updates_switch.set_checked_silently(values["check_for_updates"])
        self.save_sound_switch.set_checked_silently(values["save_sound_enabled"])
        self.theme_control.setCurrent(_THEME_MODE_LABELS.get(values["theme_mode"], "System"))
        framerate_index = self.framerate_combo.findData(values["framerate"])
        if framerate_index >= 0:
            set_blocked(self.framerate_combo, lambda: self.framerate_combo.setCurrentIndex(framerate_index))
        scale_index = self.resolution_scale_combo.findData(values["resolution_scale"])
        if scale_index >= 0:
            set_blocked(self.resolution_scale_combo, lambda: self.resolution_scale_combo.setCurrentIndex(scale_index))

    def _build_payload(self) -> dict | None:
        """Assemble the full settings payload from the current control
        values -- the one and only place the field->payload mapping lives
        (the autosave fire and its last-known-good snapshot both read
        through here). Returns None when a guard rejects the current values;
        the guard has already put the reason on the status label, and the
        field is left as-is (the user may still be mid-edit -- never revert
        an empty field under their fingers).
        """
        hotkey_text = self.hotkey_field.combo().strip()
        if not hotkey_text:
            self._set_status("Hotkey cannot be empty.", "error")
            return None
        clips_text = self.clips_dir_edit.text().strip()
        if not clips_text:
            self._set_status("Clips folder cannot be empty.", "error")
            return None
        filename_template_text = self.filename_template_edit.text().strip()
        if not filename_template_text:
            self._set_status("Filename template cannot be empty.", "error")
            return None

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

        return {
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
            "theme_mode": _THEME_MODES.get(self.theme_control.current(), "system"),
            "framerate": self.framerate_combo.currentData(),
            "resolution_scale": self.resolution_scale_combo.currentData(),
            # Empty combos pass through as "" (= disabled); validating
            # non-empty ones is apply_settings' job, and its error string
            # lands on the status label like any other failure.
            "quick_save_hotkey_1": self.quick_save_hotkey_1_field.combo().strip(),
            "quick_save_seconds_1": self.quick_save_seconds_1_spin.value(),
            "quick_save_hotkey_2": self.quick_save_hotkey_2_field.combo().strip(),
            "quick_save_seconds_2": self.quick_save_seconds_2_spin.value(),
            "screenshot_hotkey": self.screenshot_hotkey_field.combo().strip(),
            "clips_max_gb": self.size_cap_slider.value(),
            "save_sound_enabled": self.save_sound_switch.isChecked(),
        }


def build_settings_frame(
    parent: QWidget | None,
    config: Config,
    ipc_port: int,
    save_events,
    current_encoder: str,
    on_apply: Callable[[dict], str | None],
    ffmpeg_path: str,
    on_update_found: Callable[[str, str], None] | None = None,
    log_path: Path | None = None,
    diagnostics_facts_provider: Callable[[], dict[str, str]] | None = None,
) -> SettingsFrame:
    """Thin factory function kept for API parity with the CustomTkinter
    original's `build_settings_frame(...)` shape. `log_path` /
    `diagnostics_facts_provider` feed the Logs sub-tab (None -> the default
    log path / a diagnostics zip without live system facts).
    """
    return SettingsFrame(
        config, ipc_port, save_events, current_encoder, on_apply, ffmpeg_path, parent,
        on_update_found=on_update_found,
        log_path=log_path,
        diagnostics_facts_provider=diagnostics_facts_provider,
    )
