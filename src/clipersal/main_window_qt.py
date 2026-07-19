"""Clipersal's main app window.

OBS-style shape: a sidebar (Home / Clips / Settings / Logs) plus one content
area, built ONCE at startup, with `QStackedWidget.setCurrentWidget()` used
for tab switching -- nothing is destroyed or rebuilt on tab switches.

Cross-thread updates (save events, show/tab-switch requests) are real Qt
signals (see signals.py) connected directly in cli.py -- MainWindow itself
doesn't poll a queue for those; `_on_save_completed` is a plain slot. The
one poll kept as a real QTimer is STATUS (`_poll_status`), since it's a
genuine "did some other trigger change capture state" check with no natural
push notification, not a Tk-thread-safety workaround -- see signals.py's
docstring.

The Home tab's status-card layout (dot + expanding text + fixed-width
buttons) needed a specific pack() ordering workaround under CustomTkinter
(actions had to be packed before the expanding text column). Under Qt's
QHBoxLayout, addWidget()/addLayout() calls are already positional
left-to-right with an explicit stretch factor -- the same layout below
needs no such workaround, direct proof of the ordering-bug class the Qt
migration was expected to eliminate structurally.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from clipersal import __version__, ipc_client, thumbnails, update_check
from clipersal.brand import BrandMark, SprigAccent
from clipersal.config import Config
from clipersal.gallery_window_qt import ThumbnailWorker, build_gallery_frame
from clipersal.settings_window_qt import build_settings_frame
from clipersal.status_dot import StatusDot
from clipersal.theme import GOOD, LIVE, NEUTRAL
from clipersal.theme import qfont as _qfont
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_STATUS_POLL_MS = 1500
_PULSE_STEP_MS = 280
_PULSE_STEPS = 5
_LOG_TAIL_POLL_MS = 2000
_LOG_TAIL_LINES = 200
_RECENT_CLIPS_COUNT = 4
_RECENT_THUMB_SIZE = (120, 68)

_NAV_ITEMS = (("home", "Home"), ("clips", "Clips"), ("settings", "Settings"), ("logs", "Logs"))


class _ClickableCard(QFrame):
    """A QFrame that runs a callback on left-click -- QFrame has no native
    clicked signal (only buttons do), and the whole recent-clips card,
    thumbnail included, should be clickable, mirroring the old CTk version's
    `widget.bind("<Button-1>", ...)` on both.
    """

    def __init__(self, on_click: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_click = on_click

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mousePressEvent(event)


class MainWindow(QWidget):
    def __init__(
        self,
        config: Config,
        ipc_port: int,
        save_events,
        current_encoder: str,
        on_apply: Callable[[dict], str | None],
        ffmpeg_path: str,
        clips_dir: Path,
        log_path: Path,
        tray_enabled: bool,
        on_quit: Callable[[], None],
        app_signals=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._ipc_port = ipc_port
        self._ffmpeg_path = ffmpeg_path
        self._clips_dir = clips_dir
        self._log_path = log_path
        self._tray_enabled = tray_enabled
        self._on_quit = on_quit
        self._app_signals = app_signals
        self._pulsing = False
        self._active_tab = "home"
        self._recent_thumb_labels: dict[Path, QLabel] = {}
        self._recent_stop_worker = threading.Event()
        self._recent_worker: ThumbnailWorker | None = None
        self._update_version: str | None = None
        self._update_url: str | None = None

        self.setWindowTitle("Clipersal")
        self.resize(1140, 640)
        self.setMinimumSize(1000, 560)

        self._tabs: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}

        self._build_shell()
        self._tabs["home"] = self._build_home_tab()
        self._tabs["clips"] = build_gallery_frame(None, ffmpeg_path, clips_dir)
        self._tabs["settings"] = build_settings_frame(
            None, config, ipc_port, save_events, current_encoder, on_apply, ffmpeg_path
        )
        self._tabs["logs"] = self._build_logs_tab()
        for tab in self._tabs.values():
            self._content_stack.addWidget(tab)
        self.select_tab("home")

        self._refresh_recent_clips()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start(_STATUS_POLL_MS)

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._refresh_log_tail)
        self._log_timer.start(_LOG_TAIL_POLL_MS)

    # ---- window lifecycle -------------------------------------------------

    def show(self) -> None:  # noqa: A003 -- intentionally shadows QWidget.show, see class docstring
        super().show()
        self.raise_()
        self.activateWindow()

    def select_tab(self, name: str) -> None:
        if name not in self._tabs:
            return
        self._active_tab = name
        self._content_stack.setCurrentWidget(self._tabs[name])
        button = self._nav_buttons.get(name)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        if name == "clips":
            self._tabs["clips"].refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._tray_enabled:
            event.ignore()
            self.hide()
        else:
            event.accept()
            self._on_quit()

    # ---- shell: sidebar + content area -------------------------------------

    def _build_shell(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        outer.addWidget(sidebar)

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 20, 16, 16)

        brand_row = QHBoxLayout()
        sidebar_layout.addLayout(brand_row)
        self._brand_mark = BrandMark(size=32, parent=sidebar)
        brand_row.addWidget(self._brand_mark)
        name_col = QVBoxLayout()
        brand_row.addLayout(name_col)
        name_label = QLabel("Clipersal", sidebar)
        name_font = name_label.font()
        name_font.setBold(True)
        name_label.setFont(name_font)
        name_col.addWidget(name_label)
        version_label = QLabel(f"v{__version__}", sidebar)
        version_label.setObjectName("hint")
        name_col.addWidget(version_label)
        brand_row.addStretch()

        sidebar_layout.addSpacing(16)

        self._nav_button_group = QButtonGroup(self)
        self._nav_button_group.setExclusive(True)
        for key, label in _NAV_ITEMS:
            button = QPushButton(label, sidebar)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setFixedHeight(36)
            button.clicked.connect(lambda _checked=False, k=key: self.select_tab(k))
            sidebar_layout.addWidget(button)
            self._nav_button_group.addButton(button)
            self._nav_buttons[key] = button

        sidebar_layout.addStretch()

        content_wrap = QWidget(self)
        outer.addWidget(content_wrap, 1)
        content_layout = QVBoxLayout(content_wrap)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_stack = QStackedWidget(content_wrap)
        content_layout.addWidget(self._content_stack)

    # ---- Home tab: status dashboard + quick actions + recent clips --------

    def _build_home_tab(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header_label = QLabel("Home", frame)
        header_font = header_label.font()
        header_font.setPointSize(header_font.pointSize() + 6)
        header_font.setBold(True)
        header_label.setFont(header_font)
        layout.addWidget(header_label)

        self._update_banner = QFrame(frame)
        self._update_banner.setObjectName("card")
        self._update_banner.setVisible(False)
        layout.addWidget(self._update_banner)
        banner_row = QHBoxLayout(self._update_banner)
        banner_row.setContentsMargins(16, 10, 16, 10)
        self._update_banner_label = QLabel("", self._update_banner)
        banner_row.addWidget(self._update_banner_label, 1)
        self._update_download_button = QPushButton("Download", self._update_banner)
        self._update_download_button.setObjectName("primary")
        self._update_download_button.clicked.connect(self._on_download_update)
        banner_row.addWidget(self._update_download_button)
        self._update_dismiss_button = QPushButton("×", self._update_banner)
        self._update_dismiss_button.setFixedWidth(28)
        self._update_dismiss_button.clicked.connect(self._on_dismiss_update)
        banner_row.addWidget(self._update_dismiss_button)

        status_card = QFrame(frame)
        status_card.setObjectName("card")
        layout.addWidget(status_card)
        status_row = QHBoxLayout(status_card)
        status_row.setContentsMargins(20, 18, 20, 18)

        self._status_dot = StatusDot(size=36, dot_diameter=14, color=GOOD, parent=status_card)
        status_row.addWidget(self._status_dot)

        status_text_col = QVBoxLayout()
        status_row.addLayout(status_text_col, 1)
        self._status_label = QLabel("Recording", status_card)
        status_label_font = self._status_label.font()
        status_label_font.setPointSize(status_label_font.pointSize() + 2)
        status_label_font.setBold(True)
        self._status_label.setFont(status_label_font)
        status_text_col.addWidget(self._status_label)
        self._status_meta_label = QLabel(self._default_status_meta(), status_card)
        self._status_meta_label.setObjectName("hint")
        self._status_meta_label.setFont(_qfont(size=11, weight="normal", mono=True))
        status_text_col.addWidget(self._status_meta_label)

        actions = QHBoxLayout()
        status_row.addLayout(actions)
        self._pause_button = QPushButton("Pause", status_card)
        self._pause_button.clicked.connect(self._on_toggle_pause)
        actions.addWidget(self._pause_button)
        self._save_30s_button = QPushButton("Save last 30s", status_card)
        self._save_30s_button.clicked.connect(lambda: self._on_save("30"))
        actions.addWidget(self._save_30s_button)
        self._save_now_button = QPushButton("Save now", status_card)
        self._save_now_button.setObjectName("primary")
        self._save_now_button.clicked.connect(lambda: self._on_save())
        actions.addWidget(self._save_now_button)

        recent_header = QHBoxLayout()
        layout.addLayout(recent_header)
        recent_title = QLabel("Recent clips", frame)
        recent_title_font = recent_title.font()
        recent_title_font.setBold(True)
        recent_title.setFont(recent_title_font)
        recent_header.addWidget(recent_title)
        recent_header.addStretch()
        view_all_button = QPushButton("View all", frame)
        view_all_button.clicked.connect(lambda: self.select_tab("clips"))
        recent_header.addWidget(view_all_button)

        self._recent_strip = QHBoxLayout()
        self._recent_strip.setSpacing(10)
        recent_container = QWidget(frame)
        recent_container.setLayout(self._recent_strip)
        layout.addWidget(recent_container)

        layout.addStretch()
        return frame

    def _refresh_recent_clips(self) -> None:
        self._recent_stop_worker.set()
        while self._recent_strip.count():
            item = self._recent_strip.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._recent_thumb_labels.clear()

        clip_paths = sorted(self._clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        clip_paths = clip_paths[:_RECENT_CLIPS_COUNT]
        if not clip_paths:
            self._recent_strip.addWidget(SprigAccent(size=40))
            no_clips_label = QLabel("No clips yet -- press your hotkey or Save now to make one.")
            no_clips_label.setObjectName("hint")
            self._recent_strip.addWidget(no_clips_label)
            self._recent_strip.addStretch()
            return

        for clip_path in clip_paths:
            card = _ClickableCard(lambda p=clip_path: open_folder(p.parent))
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            thumb = QLabel(card)
            thumb.setObjectName("thumbPlaceholder")
            thumb.setFixedSize(*_RECENT_THUMB_SIZE)
            card_layout.addWidget(thumb)
            name_label = QLabel(clip_path.stem, card)
            name_label.setObjectName("hint")
            name_label.setFixedWidth(_RECENT_THUMB_SIZE[0])
            card_layout.addWidget(name_label)
            self._recent_strip.addWidget(card)
            self._recent_thumb_labels[clip_path] = thumb

        self._recent_stop_worker = threading.Event()
        worker = ThumbnailWorker(
            self._ffmpeg_path, clip_paths, self._clips_dir / thumbnails.THUMBNAIL_DIR_NAME, self._recent_stop_worker
        )
        worker.ready.connect(self._apply_recent_thumbnail)
        self._recent_worker = worker
        threading.Thread(target=worker.run, daemon=True).start()

    def _apply_recent_thumbnail(self, clip_path: Path, thumbnail_path: Path | None) -> None:
        label = self._recent_thumb_labels.get(clip_path)
        if label is None or thumbnail_path is None:
            return
        pixmap = QPixmap(str(thumbnail_path))
        if pixmap.isNull():
            return
        scaled = pixmap.scaled(
            *_RECENT_THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)

    # ---- Logs tab -----------------------------------------------------------

    def _build_logs_tab(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QHBoxLayout()
        layout.addLayout(header)
        header_label = QLabel("Logs", frame)
        header_font = header_label.font()
        header_font.setPointSize(header_font.pointSize() + 6)
        header_font.setBold(True)
        header_label.setFont(header_font)
        header.addWidget(header_label)
        header.addStretch()
        open_log_folder_button = QPushButton("Open log folder", frame)
        open_log_folder_button.clicked.connect(lambda: open_folder(self._log_path.parent))
        header.addWidget(open_log_folder_button)

        self._log_textbox = QPlainTextEdit(frame)
        self._log_textbox.setReadOnly(True)
        self._log_textbox.setFont(_qfont(size=11, mono=True))
        self._log_textbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._log_textbox, 1)

        self._refresh_log_tail()
        return frame

    def _refresh_log_tail(self) -> None:
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                content = "".join(f.readlines()[-_LOG_TAIL_LINES:]) or "(log file is empty)"
        except OSError:
            content = f"(log file not found yet: {self._log_path})"
        self._log_textbox.setPlainText(content)
        cursor = self._log_textbox.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log_textbox.setTextCursor(cursor)

    # ---- status polling (shared by the Home tab's badge) -------------------

    def _send(self, command: str, arg: str | None = None) -> str | None:
        try:
            return ipc_client.send_command(command, arg=arg, port=self._ipc_port)
        except ipc_client.IpcClientError:
            return None

    def _on_toggle_pause(self) -> None:
        response = self._send("STATUS")
        if response and ("PAUSED" in response or "CRASHED" in response):
            # RESUME doubles as the manual recovery action when capture is
            # down after ffmpeg crashed and auto-restart gave up -- see
            # cli.py's handle_resume.
            self._send("RESUME")
        else:
            self._send("PAUSE")

    def _default_status_meta(self) -> str:
        return f"Buffer: {self._config.buffer_seconds}s   ·   {self._clips_dir}"

    def _on_save(self, trim_arg: str | None = None) -> None:
        # A SAVE's server-side remux can legitimately run tens of seconds (up
        # to concat.py's _CONCAT_TIMEOUT, 60s) -- far past ipc_client's 5s
        # default -- and sent from a button click it ran ON the GUI thread,
        # freezing the whole window for the remux and then reporting failure
        # after 5s while the save actually completed. Run it on a worker
        # thread with a timeout above the server's own instead. Success is
        # announced by the server side (save_completed + toast via cli.py's
        # handle_save), so only failure needs to come back here.
        threading.Thread(target=self._save_worker, args=(trim_arg,), daemon=True).start()

    def _save_worker(self, trim_arg: str | None = None) -> None:
        try:
            response = ipc_client.send_command(
                "SAVE", arg=trim_arg, port=self._ipc_port, timeout=ipc_client.SAVE_TIMEOUT
            )
        except ipc_client.IpcClientError as exc:
            detail = str(exc)
        else:
            if not response.startswith("ERROR"):
                return
            detail = response[len("ERROR") :].strip()
        log.warning("Save failed: %s", detail)
        if self._app_signals is not None:
            # NEVER touch widgets from this worker thread -- deliver the
            # failure to the GUI thread via the queued signal, the same rule
            # as every other cross-thread update (see signals.py's docstring).
            self._app_signals.save_failed.emit(detail)

    def _set_status_dot(self, color: str) -> None:
        self._status_dot.set_color(color)

    def _poll_status(self) -> None:
        if self._pulsing:
            return
        response = self._send("STATUS")
        if response is None:
            return
        if "CRASHED" in response:
            self._set_status_dot(LIVE)
            self._status_label.setText("Capture stopped -- see Logs")
            self._pause_button.setText("Resume")
        elif "PAUSED" in response:
            self._set_status_dot(NEUTRAL)
            self._status_label.setText("Paused")
            self._pause_button.setText("Resume")
        else:
            self._set_status_dot(GOOD)
            self._status_label.setText("Recording")
            self._pause_button.setText("Pause")

    def _run_pulse(self, step: int = 0) -> None:
        if step >= _PULSE_STEPS:
            self._pulsing = False
            self._set_status_dot(GOOD)
            self._status_label.setText("Recording")
            return
        if step == 0:
            # A single flash to LIVE plus the scatter animation, rather than
            # the old repeated color-toggle loop -- the "seed dispersal"
            # motion itself now carries the pulse's visual interest across
            # the remaining steps' wait, not a flickering color swap.
            self._set_status_dot(LIVE)
            self._status_dot.pulse(LIVE)
        QTimer.singleShot(_PULSE_STEP_MS, lambda: self._run_pulse(step + 1))

    def on_save_completed(self) -> None:
        """Connected (by cli.py) to AppSignals.save_completed -- replaces the
        old queue-drained `_poll_save_events` loop with a direct slot, since
        a real Qt signal needs no polling at all.
        """
        self._pulsing = True
        self._status_label.setText("Saving…")
        # A successful save also clears any earlier save-failure note from the
        # status card's meta line.
        self._status_meta_label.setText(self._default_status_meta())
        self._run_pulse()
        self._refresh_recent_clips()
        if self._active_tab == "clips":
            self._tabs["clips"].refresh()

    def on_save_failed(self, detail: str) -> None:
        """Connected (by cli.py) to AppSignals.save_failed. The failure goes
        on the status card's meta line rather than _status_label: the meta
        line is persistent, whereas _poll_status would overwrite _status_label
        again a second later. Cleared by the next successful save (see
        on_save_completed).
        """
        summary = detail.strip().splitlines()[0] if detail.strip() else "unknown error"
        if len(summary) > 120:
            summary = summary[:117] + "..."
        self._status_meta_label.setText(f"Save failed -- {summary}")

    def show_update_banner(self, version: str, url: str) -> None:
        """Connected (by cli.py) to AppSignals.update_available."""
        self._update_version = version
        self._update_url = url
        self._update_banner_label.setText(f"Clipersal {version} is available")
        self._update_banner.setVisible(True)

    def _on_download_update(self) -> None:
        if self._update_url is None:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        # QDesktopServices calls the OS's native "open a URL" API directly --
        # unlike webbrowser.open, it never shells out to a subprocess, so it
        # can't reintroduce the console-flash bug subprocess_utils.py exists
        # to prevent in a --windowed packaged build.
        QDesktopServices.openUrl(QUrl(self._update_url))

    def _on_dismiss_update(self) -> None:
        self._update_banner.setVisible(False)
        if self._update_version is None:
            return
        try:
            cache = update_check.load_cache()
            cache["dismissed_version"] = self._update_version
            update_check.save_cache(cache)
        except Exception:  # noqa: BLE001 -- dismissing must never crash the window over a cache-write hiccup
            log.exception("Failed to persist dismissed update version")
