"""First-run setup wizard. Shown once, before normal startup, when no config
file exists yet (per `config_store.default_config_path()`) -- i.e. a
genuinely first launch.

A QDialog built against the single shared QApplication cli.py constructs
before anything else -- just a QDialog run via `.exec()` against that app,
no separate event loop needed since there's only ever one QApplication.

Both "Get Started" and closing the dialog (✕, same as "Skip for now")
persist the current config values -- the only difference is whether the
user edited the fields first. Persisting either way means the wizard
doesn't nag on every subsequent launch; only a config file that's never
been written at all counts as "first run".
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from clipersal import config_store, theme
from clipersal.brand import BrandMark, SprigAccent
from clipersal.config import Config
from clipersal.hotkey import is_valid_combo
from clipersal.hotkey_widget_qt import HotkeyField

log = logging.getLogger(__name__)


def _persist(config: Config) -> str | None:
    """Persist the (possibly edited) config, returning an error string when
    the write fails (a read-only config dir, a full disk) instead of raising
    out of a button slot: "Get Started" surfaces it on the error label,
    "Skip" just closes (no config file means the wizard reappears next
    launch, which is honest enough)."""
    try:
        config_store.save_overrides(
            {
                "buffer_seconds": config.buffer_seconds,
                "clips_dir": str(config.clips_dir),
                "hotkey_combo": config.hotkey_combo,
                "video_bitrate": config.video_bitrate,
                "encoder_override": config.encoder_override,
                "filename_template": config.filename_template,
                "clip_retention_days": config.clip_retention_days,
                "launch_on_startup": config.launch_on_startup,
            }
        )
    except OSError as exc:
        log.warning("Could not save settings from the first-run wizard: %s", exc)
        return str(exc)
    return None


class _FirstRunDialog(QDialog):
    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Welcome to Clipersal")
        # Snug fit: the old 420px height left a large dead gap between the
        # card and the footer (the stretch below absorbs the difference).
        self.setFixedSize(440, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 24, 22, 22)

        header = QHBoxLayout()
        layout.addLayout(header)
        brand_mark = BrandMark(size=34, parent=self)
        header.addWidget(brand_mark)
        title_col = QVBoxLayout()
        header.addLayout(title_col, 1)
        # The wizard's one-line page title -- H1 like the main window's page
        # headers (theme.py's typography rules).
        title_label = QLabel("Welcome to Clipersal!", self)
        title_label.setFont(theme.qfont(size=theme.FONT_H1))
        title_col.addWidget(title_label)
        subtitle = QLabel("Let's get you set up -- takes a few seconds.", self)
        subtitle.setObjectName("hint")
        # Word-wrap: the dialog is a fixed 440px, and at the HINT size an
        # unwrapped one-liner can overflow past the right edge.
        subtitle.setWordWrap(True)
        title_col.addWidget(subtitle)
        header.addWidget(SprigAccent(size=34, parent=self))

        card = QFrame(self)
        card.setObjectName("card")
        layout.addWidget(card)
        card_layout = QVBoxLayout(card)

        card_layout.addWidget(self._field_label("Clips folder", card))
        clips_row = QHBoxLayout()
        card_layout.addLayout(clips_row)
        self._clips_dir_edit = QLineEdit(str(config.clips_dir), card)
        clips_row.addWidget(self._clips_dir_edit, 1)
        browse_button = QPushButton("Browse...", card)
        browse_button.clicked.connect(self._browse_clips_dir)
        clips_row.addWidget(browse_button)

        card_layout.addWidget(self._field_label("Save hotkey", card))
        self._hotkey_field = HotkeyField(config.hotkey_combo, card)
        card_layout.addWidget(self._hotkey_field)
        hint_label = QLabel(
            "Click Record and press a combo, or type it directly -- press this to save your last clip.", card
        )
        hint_label.setObjectName("hint")
        hint_label.setWordWrap(True)
        card_layout.addWidget(hint_label)

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("statusLabel")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        layout.addStretch()

        footer = QHBoxLayout()
        layout.addLayout(footer)
        skip_button = QPushButton("Skip for now", self)
        skip_button.clicked.connect(self._skip)
        footer.addWidget(skip_button)
        footer.addStretch()
        get_started_button = QPushButton("Get Started", self)
        get_started_button.setObjectName("primary")
        get_started_button.clicked.connect(self._get_started)
        footer.addWidget(get_started_button)

    def _field_label(self, text: str, parent: QWidget) -> QLabel:
        """A field name label -- plain BODY text (theme.py's typography
        rules: bold is reserved for titles and clip names)."""
        return QLabel(text, parent)

    def _browse_clips_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose clips folder", self._clips_dir_edit.text() or str(Path.home())
        )
        if chosen:
            self._clips_dir_edit.setText(chosen)

    def _set_error(self, text: str) -> None:
        self._error_label.setText(text)
        self._error_label.setProperty("state", "error")
        style = self._error_label.style()
        style.unpolish(self._error_label)
        style.polish(self._error_label)

    def _skip(self) -> None:
        # Same mid-record hazard as _get_started, but for the listener itself:
        # closing the wizard while the recorder is listening must tear down
        # its OS-wide pynput Listener, not leak it for the rest of the
        # process. (The config values persisted below were never mutated by
        # the recorder, so there's no placeholder-persist hazard here.)
        if self._hotkey_field.is_recording():
            self._hotkey_field.cancel_recording()
        _persist(self._config)
        self.accept()

    def _get_started(self) -> None:
        # Same mid-record hazard as Settings' Save: don't persist the
        # recorder's "Press keys..." placeholder as the combo.
        if self._hotkey_field.is_recording():
            self._hotkey_field.cancel_recording()
        hotkey_text = self._hotkey_field.combo().strip()
        if not hotkey_text:
            self._set_error("Hotkey cannot be empty.")
            return
        # Unlike Settings, this path persists the combo directly (no
        # apply_settings in between), so the parse check has to happen here
        # -- a bad combo saved now means no hotkey on every future launch.
        if not is_valid_combo(hotkey_text):
            self._set_error(f"Invalid hotkey combo: {hotkey_text!r} -- use pynput format, e.g. <ctrl>+<alt>+r")
            return
        clips_text = self._clips_dir_edit.text().strip()
        if not clips_text:
            self._set_error("Clips folder cannot be empty.")
            return
        try:
            new_clips_dir = Path(clips_text).expanduser()
            new_clips_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_error(f"Could not use clips folder: {exc}")
            return

        self._config.clips_dir = new_clips_dir
        self._config.hotkey_combo = hotkey_text
        error = _persist(self._config)
        if error is not None:
            self._set_error(f"Could not save settings: {error}")
            return
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 -- the ✕ button behaves like "Skip for now"
        self._skip()
        super().closeEvent(event)


def show_first_run_wizard(config: Config) -> None:
    """Blocking (runs its own QDialog.exec() against the shared
    QApplication) -- returns once the user has clicked through, having
    mutated `config` in place and persisted it either way.
    """
    dialog = _FirstRunDialog(config)
    dialog.exec()
