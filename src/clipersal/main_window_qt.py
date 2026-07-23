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
import urllib.parse
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QPixmap, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from clipersal import (
    __version__,
    brand,
    config_store,
    diagnostics,
    ipc_client,
    player_qt,
    theme,
    thumbnails,
    update_check,
)
from clipersal.config import Config
from clipersal.gallery_window_qt import EMPTY_CLIPS_MESSAGE, ThumbnailWorker, build_gallery_frame, clips_newest_first
from clipersal.qt_widgets import ElidedLabel, ToggleSwitch
from clipersal.settings_window_qt import build_settings_frame
from clipersal.status_dot import StatusDot
from clipersal.theme import qfont as _qfont
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_STATUS_POLL_MS = 1500
_PULSE_STEP_MS = 280
_PULSE_STEPS = 5
_LOG_TAIL_POLL_MS = 2000
# Higher than the old 200 now that the search/level filters cut the visible
# volume down -- a filtered view should still reach reasonably far back.
_LOG_TAIL_LINES = 500
_RECENT_CLIPS_COUNT = 4
_RECENT_THUMB_SIZE = (120, 68)

# Low-disk banner thresholds for the clips folder, fed by STATS'
# clips_free_bytes (no separate monitor thread). Hysteresis: the banner
# appears below 1 GiB but only clears above 1.5 GiB, so a value hovering at
# the boundary doesn't flap the banner every poll.
_LOW_DISK_WARN_BYTES = 1 << 30
_LOW_DISK_CLEAR_BYTES = _LOW_DISK_WARN_BYTES * 3 // 2

_LOG_LEVEL_CHOICES = ("All", "INFO", "WARNING", "ERROR")

_NAV_ITEMS = (("home", "Home"), ("clips", "Clips"), ("settings", "Settings"), ("logs", "Logs"))

# Crash-report prompt (#11): on an edge INTO the CRASHED state the user is
# asked, once per crash episode, whether to send a crash report. Nothing is
# posted anywhere by the app itself -- "Send report" opens a prefilled GitHub
# issue in the browser for the user to review and submit, the same
# user-initiated-open pattern as the update banner's Download button (so the
# app's only outbound connection stays the update check; see update_check.py).
CRASH_REPORT_ISSUES_URL = f"https://github.com/{update_check.GITHUB_REPO}/issues/new"
_CRASH_REPORT_BODY_LIMIT = 6000  # practical ceiling for an issues/new?body= URL
_CRASH_REPORT_LOG_TAIL_LINES = 150
_CRASH_REPORT_FFMPEG_TAIL_LINES = 80
# The tail reader pulls at most this many bytes from a log's END -- plenty for
# 150+80 typical log lines, small enough that building a report can never
# slurp a multi-MB log file whole into memory.
_TAIL_READ_MAX_BYTES = 256 * 1024
_TRUNCATED_NOTICE = "\n\n... (older log lines dropped to fit the report size limit) ...\n\n"


def _tail_lines(path: Path, max_lines: int, max_bytes: int = _TAIL_READ_MAX_BYTES) -> list[str]:
    """The last `max_lines` lines of a text file, size-guarded: at most
    `max_bytes` is read, from the END of the file (seek, not a full read --
    see the _TAIL_READ_MAX_BYTES comment). Never raises; a missing or
    unreadable file just means no lines.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        # A seek into the middle of a file lands mid-line; drop the partial
        # first line rather than ship a garbled fragment in the report.
        lines = lines[1:]
    return lines[-max_lines:]


def _build_crash_report_body(
    facts: dict[str, str], app_log_lines: list[str], ffmpeg_log_lines: list[str]
) -> str:
    """The GitHub issue body behind a "Send report" click: the collected
    system facts, then the tails of the app log and (when present) the
    capture session's ffmpeg log. Capped at _CRASH_REPORT_BODY_LIMIT chars --
    an over-long report keeps its facts header plus the NEWEST log lines (the
    ones around the crash), dropping the oldest.
    """
    header = "\n".join(f"{key}: {value}" for key, value in facts.items())
    sections: list[str] = []
    if app_log_lines:
        sections.append("--- app log (tail) ---\n" + "\n".join(app_log_lines))
    if ffmpeg_log_lines:
        sections.append("--- ffmpeg log (tail) ---\n" + "\n".join(ffmpeg_log_lines))
    logs = "\n\n".join(sections)
    body = f"{header}\n\n{logs}" if logs else header
    if len(body) <= _CRASH_REPORT_BODY_LIMIT:
        return body
    keep = _CRASH_REPORT_BODY_LIMIT - len(header) - len(_TRUNCATED_NOTICE)
    if keep < 0:
        # Facts alone overflow (not realistic): fall back to a hard cap.
        return body[:_CRASH_REPORT_BODY_LIMIT]
    return f"{header}{_TRUNCATED_NOTICE}{logs[-keep:]}"


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
        clips_dir_provider: Callable[[], Path],
        log_path: Path,
        tray_enabled: bool,
        on_quit: Callable[[], None],
        app_signals=None,
        diagnostics_facts_provider: Callable[[], dict[str, str]] | None = None,
        encoder_provider: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Opts into the global stylesheet's top-level background rule
        # (QWidget#mainWindow) -- the window owns the app's background surface
        # now that backgrounds are scoped, not blanket-painted (see theme.py).
        self.setObjectName("mainWindow")
        self._config = config
        self._ipc_port = ipc_port
        self._ffmpeg_path = ffmpeg_path
        # A live provider, not a frozen Path: apply_settings live-mutates
        # config.clips_dir, and the recent-clips strip / status meta must
        # follow a clips-folder change without an app restart.
        self._clips_dir_provider = clips_dir_provider
        self._log_path = log_path
        self._tray_enabled = tray_enabled
        self._on_quit = on_quit
        self._app_signals = app_signals
        # Live system facts for the diagnostics zip (OS, session, ffmpeg
        # version, monitors, ...) -- a provider because the encoder can
        # change via apply_settings after the window is built.
        self._diagnostics_facts_provider = diagnostics_facts_provider
        self._pulsing = False
        self._active_tab = "home"
        # Last capture state reported by the STATUS/STATS poll -- drives the
        # pause button's label/action, the crash banner, and the title.
        self._capture_state = "RECORDING"
        # The crash-report prompt currently on screen (non-modal), if any. It
        # is shown once per crash episode by the edge detection in
        # _apply_capture_state, so the poll timer is never blocked by it.
        self._crash_prompt: QMessageBox | None = None
        # The low-disk banner's dismiss hides it until free space climbs
        # back over the high-water mark and drops again (see _update_disk_banner).
        self._disk_dismissed = False
        self._recent_thumb_labels: dict[Path, QLabel] = {}
        self._recent_stop_worker = threading.Event()
        self._recent_worker: ThumbnailWorker | None = None
        self._update_version: str | None = None
        self._update_url: str | None = None
        # Open PlayerDialogs from the recent-clips strip, kept referenced so
        # the GC can't collect them (modal-less; several may be open at once).
        self._players: list = []

        self.setWindowTitle("Clipersal")
        self.resize(1140, 640)
        self.setMinimumSize(1000, 560)

        self._tabs: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}

        self._build_shell()
        self._tabs["home"] = self._build_home_tab()
        # encoder_provider is a live read of state.setup.encoder (Settings
        # can swap it mid-session); the Clips tab's Compress dialog uses it.
        self._tabs["clips"] = build_gallery_frame(None, ffmpeg_path, clips_dir_provider, encoder_provider=encoder_provider)
        self._tabs["settings"] = build_settings_frame(
            None, config, ipc_port, save_events, current_encoder, on_apply, ffmpeg_path,
            on_update_found=self.show_update_banner,
        )
        self._tabs["logs"] = self._build_logs_tab()
        for tab in self._tabs.values():
            self._content_stack.addWidget(tab)
        # Gallery edits (delete / rename / trim / compress exports) change
        # the clip set without a save happening -- keep the Home strip in
        # step. Plain gallery refreshes/sorts don't emit this (by design).
        self._tabs["clips"].clips_changed.connect(self._refresh_recent_clips)
        self.select_tab("home")

        self._build_shortcuts()
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

    # ---- keyboard shortcuts -------------------------------------------------

    def _build_shortcuts(self) -> None:
        """Window-local shortcuts (Qt.WindowShortcut: they fire while this
        window or one of its children has focus -- they are NOT global
        hotkeys; the pynput listener in cli.py owns those). SAVE-class
        actions go through the same worker-thread IPC path as the buttons.
        """
        self._shortcuts: list[QShortcut] = []

        def bind(key: str, slot: Callable[[], None]) -> None:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

        bind("Ctrl+S", lambda: self._on_save())
        bind("Ctrl+Shift+S", lambda: self._on_save("30"))
        bind("Ctrl+P", self._on_toggle_pause)
        bind("F5", lambda: self._tabs["clips"].refresh())
        bind("Ctrl+,", lambda: self.select_tab("settings"))
        bind("Ctrl+1", lambda: self.select_tab("home"))
        bind("Ctrl+2", lambda: self.select_tab("clips"))
        bind("Ctrl+3", lambda: self.select_tab("settings"))
        bind("Ctrl+4", lambda: self.select_tab("logs"))

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
        self._brand_mark = brand.BrandMark(size=32, parent=sidebar)
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

        # Sidebar footer: the studio identity row (opens the Lablooms org
        # page) and a Support link to the project's GitHub repo -- the app is
        # one of a studio's apps, and this is where that shows.
        self._lablooms_row = _ClickableCard(self._open_lablooms_url, sidebar)
        self._lablooms_row.setCursor(Qt.CursorShape.PointingHandCursor)
        lablooms_row = QHBoxLayout(self._lablooms_row)
        lablooms_row.setContentsMargins(4, 4, 4, 4)
        lablooms_row.setSpacing(6)
        lablooms_row.addWidget(brand.BrandMark(size=16, parent=self._lablooms_row))
        lablooms_label = QLabel("Lablooms", self._lablooms_row)
        lablooms_label.setObjectName("hint")
        lablooms_row.addWidget(lablooms_label)
        lablooms_row.addStretch()
        sidebar_layout.addWidget(self._lablooms_row)

        self._support_button = QPushButton("♥ Support", sidebar)
        self._support_button.setObjectName("supportButton")
        self._support_button.clicked.connect(self._open_support_url)
        sidebar_layout.addWidget(self._support_button)

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
        header_label.setFont(_qfont(size=theme.FONT_H1))
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

        # Crash banner: shown while STATS reports CRASHED (ffmpeg died and
        # the auto-restart budget ran out). Same card-banner pattern as the
        # update banner above.
        self._crash_banner = QFrame(frame)
        self._crash_banner.setObjectName("card")
        self._crash_banner.setVisible(False)
        layout.addWidget(self._crash_banner)
        crash_row = QHBoxLayout(self._crash_banner)
        crash_row.setContentsMargins(16, 10, 16, 10)
        crash_text_col = QVBoxLayout()
        crash_row.addLayout(crash_text_col, 1)
        crash_title = QLabel("Capture stopped", self._crash_banner)
        crash_title.setObjectName("crashTitle")
        crash_text_col.addWidget(crash_title)
        crash_hint = QLabel(
            "ffmpeg crashed and automatic restarts gave up. Restart capture to resume recording.",
            self._crash_banner,
        )
        crash_hint.setObjectName("hint")
        crash_text_col.addWidget(crash_hint)
        self._crash_restart_button = QPushButton("Restart capture", self._crash_banner)
        self._crash_restart_button.setObjectName("primary")
        self._crash_restart_button.clicked.connect(self._on_restart_capture)
        crash_row.addWidget(self._crash_restart_button)
        crash_logs_button = QPushButton("View logs", self._crash_banner)
        crash_logs_button.clicked.connect(lambda: self.select_tab("logs"))
        crash_row.addWidget(crash_logs_button)

        # Low-disk banner: driven by STATS' clips_free_bytes with a 1/1.5 GiB
        # hysteresis band (see _update_disk_banner); the × dismisses it until
        # free space next rises above the high-water mark and drops again.
        self._disk_banner = QFrame(frame)
        self._disk_banner.setObjectName("card")
        self._disk_banner.setVisible(False)
        layout.addWidget(self._disk_banner)
        disk_row = QHBoxLayout(self._disk_banner)
        disk_row.setContentsMargins(16, 10, 16, 10)
        disk_text_col = QVBoxLayout()
        disk_row.addLayout(disk_text_col, 1)
        disk_title = QLabel("Low disk space", self._disk_banner)
        disk_title.setObjectName("bannerTitle")
        disk_text_col.addWidget(disk_title)
        self._disk_hint_label = QLabel("", self._disk_banner)
        self._disk_hint_label.setObjectName("hint")
        disk_text_col.addWidget(self._disk_hint_label)
        self._disk_dismiss_button = QPushButton("×", self._disk_banner)
        self._disk_dismiss_button.setFixedWidth(28)
        self._disk_dismiss_button.clicked.connect(self._on_dismiss_disk_banner)
        disk_row.addWidget(self._disk_dismiss_button)

        status_card = QFrame(frame)
        status_card.setObjectName("card")
        layout.addWidget(status_card)
        status_row = QHBoxLayout(status_card)
        status_row.setContentsMargins(20, 18, 20, 18)

        # Read the status colors through the theme module at call time, never
        # a by-value `from theme import GOOD`: apply_theme() rewrites the
        # module attributes on a theme switch, and a by-value import would
        # keep handing the dot the OLD palette's hex strings forever.
        self._status_dot = StatusDot(size=36, dot_diameter=14, color=theme.GOOD, parent=status_card)
        status_row.addWidget(self._status_dot)

        status_text_col = QVBoxLayout()
        status_row.addLayout(status_text_col, 1)
        self._status_label = QLabel("Recording", status_card)
        self._status_label.setFont(_qfont(size=theme.FONT_H2))
        status_text_col.addWidget(self._status_label)
        # The meta line is two labels, not one elided string: middle-eliding
        # the WHOLE "Buffer: 60s · <clips_dir>" line used to chop into the
        # buffer text itself ("60…ight\clips"). The fixed prefix keeps the
        # buffer part always intact; only the clips_dir part elides (middle,
        # so both the drive root and the folder name stay visible).
        meta_row = QHBoxLayout()
        meta_row.setSpacing(0)  # the prefix carries its own trailing separator
        status_text_col.addLayout(meta_row)
        self._status_meta_prefix_label = QLabel("", status_card)
        self._status_meta_prefix_label.setObjectName("hint")
        self._status_meta_prefix_label.setFont(_qfont(size=theme.FONT_MONO, weight="normal", mono=True))
        meta_row.addWidget(self._status_meta_prefix_label)
        self._status_meta_label = ElidedLabel("", status_card, Qt.TextElideMode.ElideMiddle)
        self._status_meta_label.setObjectName("hint")
        self._status_meta_label.setFont(_qfont(size=theme.FONT_MONO, weight="normal", mono=True))
        meta_row.addWidget(self._status_meta_label, 1)
        self._show_default_status_meta()
        # Second meta line, fed by the STATS poll: uptime, buffer fill,
        # encoder, free disk. Empty until the first successful poll -- and
        # left alone by on_save_failed/on_save_completed, which own line 1.
        self._status_stats_label = ElidedLabel("", status_card)
        self._status_stats_label.setObjectName("hint")
        self._status_stats_label.setFont(_qfont(size=theme.FONT_MONO, weight="normal", mono=True))
        status_text_col.addWidget(self._status_stats_label)

        actions = QHBoxLayout()
        status_row.addLayout(actions)
        self._pause_button = QPushButton("Pause capture", status_card)
        self._pause_button.clicked.connect(self._on_toggle_pause)
        actions.addWidget(self._pause_button)
        # Deliberately just Pause + Save now: the 15/30/60s quick-saves live
        # on the tray menu, the quick-save hotkeys, and `clipersal-trigger
        # save --trim N` -- six buttons made the card read as a toolbar and
        # squeezed the meta line into truncation.
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
        # Manual re-read for the cases no signal covers (a clip deleted
        # externally, a save from another trigger source missed while the
        # window was hidden). Gallery edits already self-sync via
        # clips_changed.
        self._recent_refresh_button = QPushButton("Refresh", frame)
        self._recent_refresh_button.clicked.connect(self._refresh_recent_clips)
        recent_header.addWidget(self._recent_refresh_button)

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

        # Read the provider once per refresh: the folder can't meaningfully
        # change mid-pass, and a Settings change is picked up on the next one.
        clips_dir = self._clips_dir_provider()
        # clips_newest_first, not a bare glob+stat sort: a clip deleted
        # mid-listing (retention sweep on the IPC thread, external delete)
        # must be skipped, not crash the refresh -- see gallery_window_qt.
        clip_paths = clips_newest_first(clips_dir)[:_RECENT_CLIPS_COUNT]
        if not clip_paths:
            self._recent_strip.addWidget(brand.SprigAccent(size=40))
            no_clips_label = QLabel(EMPTY_CLIPS_MESSAGE)
            no_clips_label.setObjectName("hint")
            self._recent_strip.addWidget(no_clips_label)
            self._recent_strip.addStretch()
            return

        for clip_path in clip_paths:
            card = _ClickableCard(lambda p=clip_path: self._play_clip(p))
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
            self._ffmpeg_path, clip_paths, clips_dir / thumbnails.THUMBNAIL_DIR_NAME, self._recent_stop_worker
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

    def _play_clip(self, clip_path: Path) -> None:
        """Recent-strip card click: the in-app player when QtMultimedia is
        importable, the OS default player otherwise -- player_qt.play_clip
        owns the check + fallback, shared with the gallery so there's
        exactly one open-a-clip path. A trim exported from here lands in
        clips_dir, so the strip re-reads on the player's trim_exported."""
        dialog = player_qt.play_clip(
            self,
            clip_path,
            self._ffmpeg_path,
            on_trim_exported=lambda _path: self._refresh_recent_clips(),
        )
        if dialog is not None:
            self._players.append(dialog)
            dialog.destroyed.connect(lambda: self._discard_player(dialog))

    def _discard_player(self, dialog) -> None:
        try:
            self._players.remove(dialog)
        except ValueError:
            pass

    # ---- Logs tab -----------------------------------------------------------

    def _build_logs_tab(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QHBoxLayout()
        layout.addLayout(header)
        header_label = QLabel("Logs", frame)
        header_label.setFont(_qfont(size=theme.FONT_H1))
        header.addWidget(header_label)
        header.addStretch()
        open_log_folder_button = QPushButton("Open log folder", frame)
        open_log_folder_button.clicked.connect(lambda: open_folder(self._log_path.parent))
        header.addWidget(open_log_folder_button)

        controls = QHBoxLayout()
        layout.addLayout(controls)
        self._log_search_edit = QLineEdit(frame)
        self._log_search_edit.setPlaceholderText("Search logs...")
        self._log_search_edit.textChanged.connect(self._refresh_log_tail)
        controls.addWidget(self._log_search_edit, 1)
        self._log_level_combo = QComboBox(frame)
        self._log_level_combo.addItems(_LOG_LEVEL_CHOICES)
        self._log_level_combo.currentTextChanged.connect(self._refresh_log_tail)
        controls.addWidget(self._log_level_combo)
        autoscroll_label = QLabel("Auto-scroll", frame)
        autoscroll_label.setObjectName("hint")
        controls.addWidget(autoscroll_label)
        self._log_autoscroll_switch = ToggleSwitch(frame, checked=True)
        controls.addWidget(self._log_autoscroll_switch)
        copy_button = QPushButton("Copy", frame)
        copy_button.clicked.connect(self._on_copy_logs)
        controls.addWidget(copy_button)
        export_button = QPushButton("Export diagnostics...", frame)
        export_button.clicked.connect(self._on_export_diagnostics)
        controls.addWidget(export_button)

        self._log_textbox = QPlainTextEdit(frame)
        self._log_textbox.setReadOnly(True)
        self._log_textbox.setFont(_qfont(size=theme.FONT_MONO, mono=True))
        self._log_textbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._log_textbox, 1)

        # Export-diagnostics feedback, same state pattern as the Settings
        # tab's status label (#statusLabel[state=...]).
        self._diagnostics_status_label = QLabel("", frame)
        self._diagnostics_status_label.setObjectName("statusLabel")
        self._diagnostics_status_label.setWordWrap(True)
        layout.addWidget(self._diagnostics_status_label)

        self._refresh_log_tail()
        return frame

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

    def _export_diagnostics_with_dialog(self) -> Path | None:
        """Save-dialog + zip export, shared by the Logs tab's "Export
        diagnostics..." button and the crash-report prompt's "Export zip"
        button so the flow lives in exactly one place. The outcome is
        reported on the Logs tab's status label either way. Returns the
        exported path, or None when the user cancelled or the export failed.
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
        # The Logs tab button; the flow itself is shared with the
        # crash-report prompt (see _export_diagnostics_with_dialog).
        self._export_diagnostics_with_dialog()

    # ---- status polling (shared by the Home tab's badge) -------------------

    def _send(self, command: str, arg: str | None = None) -> str | None:
        try:
            return ipc_client.send_command(command, arg=arg, port=self._ipc_port)
        except ipc_client.IpcClientError:
            return None

    def _on_toggle_pause(self) -> None:
        # The decision uses the last POLLED state rather than a fresh STATUS
        # round-trip: the 1.5s poll keeps it current, and the button's own
        # label already advertises exactly what a click will do.
        command = "RESUME" if self._capture_state in ("PAUSED", "CRASHED") else "PAUSE"
        self._start_pause_worker(command)

    def _on_restart_capture(self) -> None:
        # The crash banner's button -- same action as "Resume capture".
        self._start_pause_worker("RESUME")

    def _start_pause_worker(self, command: str) -> None:
        # Off the GUI thread, like the save worker: a click must never block
        # the window on IPC. No result needs to come back -- the next poll
        # re-syncs the label/banner either way, so a failure just logs.
        threading.Thread(target=self._pause_worker, args=(command,), daemon=True).start()

    def _pause_worker(self, command: str) -> None:
        # RESUME doubles as the manual recovery action when capture is down
        # after ffmpeg crashed and auto-restart gave up -- see cli.py's
        # handle_resume.
        response = self._send(command)
        if response is None or response.startswith("ERROR"):
            log.warning("%s from the main window failed: %s", command, response or "IPC unreachable")

    def _default_status_meta(self) -> str:
        return f"Buffer: {self._config.buffer_seconds}s   ·   {self._clips_dir_provider()}"

    def _show_default_status_meta(self) -> None:
        """The default meta line, split across its two labels (see
        _build_home_tab): the fixed prefix carries the buffer length, the
        middle-elided label carries only the clips_dir path."""
        self._status_meta_prefix_label.setText(f"Buffer: {self._config.buffer_seconds}s   ·   ")
        self._status_meta_label.setText(str(self._clips_dir_provider()))

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
        stats = self._fetch_stats()
        if stats is None:
            return
        self._apply_capture_state(stats.get("state", ""))
        self._status_stats_label.setText(self._format_stats_line(stats))
        self._update_disk_banner(stats)

    def _fetch_stats(self) -> dict[str, str] | None:
        """STATS is the primary poll (one round-trip carries the state AND
        the dashboard numbers). A server that predates STATS answers "ERROR
        unknown command" -- fall back to the plain STATUS behavior from
        before STATS existed, with every other stat unknown. None means
        unreachable: leave all UI as it is, like the old STATUS failure.
        """
        response = self._send("STATS")
        if response is None:
            return None
        if response.startswith("ERROR"):
            status = self._send("STATUS")
            if status is None:
                return None
            if "CRASHED" in status:
                state = "CRASHED"
            elif "PAUSED" in status:
                state = "PAUSED"
            else:
                state = "RECORDING"
            return {"state": state}
        return ipc_client.parse_stats_payload(response)

    def _apply_capture_state(self, state: str) -> None:
        if state not in ("RECORDING", "PAUSED", "CRASHED"):
            # A server-side degraded STATS (state field empty) must not read
            # as "Recording" -- keep the last known presentation.
            return
        # Edge detection for the crash-report prompt: an entry INTO CRASHED
        # fires once per crash episode; the state leaving CRASHED re-arms it.
        entered_crashed = state == "CRASHED" and self._capture_state != "CRASHED"
        self._capture_state = state
        if state == "CRASHED":
            self._set_status_dot(theme.LIVE)
            self._status_label.setText("Capture stopped -- see Logs")
            self._pause_button.setText("Resume capture")
            self.setWindowTitle("Clipersal — Capture stopped")
        elif state == "PAUSED":
            self._set_status_dot(theme.NEUTRAL)
            self._status_label.setText("Paused")
            self._pause_button.setText("Resume capture")
            self.setWindowTitle("Clipersal — Paused")
        else:
            self._set_status_dot(theme.GOOD)
            self._status_label.setText("Recording")
            self._pause_button.setText("Pause capture")
            self.setWindowTitle("Clipersal")
        # The crash banner auto-hides the moment capture recovers.
        self._crash_banner.setVisible(state == "CRASHED")
        if entered_crashed:
            self._show_crash_report_prompt()

    # ---- crash-report prompt ------------------------------------------------

    def _show_crash_report_prompt(self) -> None:
        """The once-per-crash-episode prompt (#11): a NON-modal QMessageBox
        (show(), never exec()) so the 1.5s status poll keeps running while it
        sits on screen. Called only on an edge into CRASHED, so it can never
        stack one dialog per poll; a still-open prompt from a previous
        episode is stale and gets closed first.
        """
        old = self._crash_prompt
        if old is not None:
            old.close()
        box = QMessageBox(self)
        box.setWindowTitle("Capture stopped")
        # NoIcon: the icon is what makes Windows play its alert sound -- the
        # crash prompt must get attention through its presence, not a "dudun".
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setText(
            "Capture crashed repeatedly and was stopped. You can restart it, or send a crash report "
            "to help fix it. Nothing is sent automatically -- the report opens in your browser for "
            "you to review first."
        )
        send_button = box.addButton("Send report", QMessageBox.ButtonRole.AcceptRole)
        export_button = box.addButton("Export zip", QMessageBox.ButtonRole.ActionRole)
        restart_button = box.addButton("Restart capture", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)

        def _clicked(button) -> None:
            if button is send_button:
                self._send_crash_report()
            elif button is export_button:
                self._export_diagnostics_with_dialog()
            elif button is restart_button:
                # Same recovery action as the crash banner's restart button.
                self._on_restart_capture()
            # "Not now" needs no handling -- the dialog simply closes.

        box.buttonClicked.connect(_clicked)
        box.finished.connect(lambda _result, b=box: self._on_crash_prompt_finished(b))
        box.setModal(False)
        # Kept referenced on self: a non-modal dialog with no owner reference
        # would be garbage-collected (and vanish) under the user's nose.
        self._crash_prompt = box
        box.show()

    def _on_crash_prompt_finished(self, box: QMessageBox) -> None:
        if self._crash_prompt is box:
            self._crash_prompt = None
        box.deleteLater()

    def _send_crash_report(self) -> None:
        """Build the crash report (system facts + size-guarded log tails) and
        open it as a prefilled GitHub issue in the user's browser. The app
        itself sends nothing -- the user reviews and submits in the browser,
        the same user-initiated-open convention as the update banner.
        """
        facts: dict[str, str] = {}
        if self._diagnostics_facts_provider is not None:
            try:
                facts = self._diagnostics_facts_provider()
            except Exception:  # noqa: BLE001 -- a facts hiccup must not block the report
                log.exception("Diagnostics facts provider failed; reporting without system facts")
        app_log_lines = _tail_lines(self._log_path, _CRASH_REPORT_LOG_TAIL_LINES)
        ffmpeg_log_lines: list[str] = []
        if self._config.buffer_dir is not None:
            ffmpeg_log_lines = _tail_lines(
                Path(self._config.buffer_dir) / "ffmpeg.log", _CRASH_REPORT_FFMPEG_TAIL_LINES
            )
        body = _build_crash_report_body(facts, app_log_lines, ffmpeg_log_lines)
        query = urllib.parse.urlencode({"title": "Crash report", "body": body})
        self._open_url(f"{CRASH_REPORT_ISSUES_URL}?{query}")

    @staticmethod
    def _parse_int(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"

    def _format_stats_line(self, stats: dict[str, str]) -> str:
        """The status card's second meta line. Every field degrades
        independently -- a missing/unparseable value is simply omitted, so
        the line never shows a bare "None" (STATS degrades failed probes to
        empty strings server-side; the STATUS fallback has no stats at all).
        """
        parts: list[str] = []
        uptime = stats.get("uptime", "")
        if uptime:
            try:
                parts.append(f"Up {self._format_uptime(float(uptime))}")
            except ValueError:
                pass
        segments = self._parse_int(stats.get("segments"))
        if segments is not None:
            buffer_seconds = self._parse_int(stats.get("buffer_seconds")) or self._config.buffer_seconds
            # The newest segment is still being written, so fill is an
            # estimate: segments x segment length, capped at the buffer size.
            fill = min(segments * self._config.segment_seconds, buffer_seconds)
            fill_text = f"Buffer fill ~{fill}s/{buffer_seconds}s ({segments} segments"
            buffer_bytes = self._parse_int(stats.get("buffer_bytes"))
            if buffer_bytes is not None:
                fill_text += f", {buffer_bytes / (1 << 20):.0f} MB"
            parts.append(fill_text + ")")
        encoder = stats.get("encoder", "")
        if encoder:
            parts.append(encoder)
        free_bytes = self._parse_int(stats.get("clips_free_bytes"))
        if free_bytes is not None:
            parts.append(f"{free_bytes / (1 << 30):.1f} GB free")
        return "   ·   ".join(parts)

    def _update_disk_banner(self, stats: dict[str, str]) -> None:
        free_bytes = self._parse_int(stats.get("clips_free_bytes"))
        if free_bytes is None:
            return  # unknown -- leave the banner (and the dismissal) as it is
        if free_bytes >= _LOW_DISK_CLEAR_BYTES:
            # Above the high-water mark: hide, and re-arm the dismissal so
            # the NEXT dip below 1 GiB shows the banner again.
            self._disk_dismissed = False
            self._disk_banner.setVisible(False)
            return
        if free_bytes < _LOW_DISK_WARN_BYTES and not self._disk_dismissed:
            self._disk_hint_label.setText(
                f"Only {free_bytes / (1 << 30):.1f} GB free in the clips folder -- "
                "free up space or pick another folder in Settings; saves may fail otherwise."
            )
            self._disk_banner.setVisible(True)
        # Between the two thresholds: keep whatever visibility the banner
        # already has -- that's the hysteresis dead band.

    def _on_dismiss_disk_banner(self) -> None:
        self._disk_dismissed = True
        self._disk_banner.setVisible(False)

    def _run_pulse(self, step: int = 0) -> None:
        if step >= _PULSE_STEPS:
            self._pulsing = False
            self._set_status_dot(theme.GOOD)
            self._status_label.setText("Recording")
            return
        if step == 0:
            # A single flash to LIVE plus the scatter animation, rather than
            # the old repeated color-toggle loop -- the "seed dispersal"
            # motion itself now carries the pulse's visual interest across
            # the remaining steps' wait, not a flickering color swap.
            self._set_status_dot(theme.LIVE)
            self._status_dot.pulse(theme.LIVE)
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
        self._show_default_status_meta()
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
        # The failure takes over the whole meta line (prefix included) --
        # "Buffer: 60s · Save failed ..." would read as nonsense.
        self._status_meta_prefix_label.setText("")
        self._status_meta_label.setText(f"Save failed -- {summary}")

    def show_update_banner(self, version: str, url: str) -> None:
        """Connected (by cli.py) to AppSignals.update_available."""
        self._update_version = version
        self._update_url = url
        self._update_banner_label.setText(f"Clipersal {version} is available")
        self._update_banner.setVisible(True)

    @staticmethod
    def _open_url(url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        # QDesktopServices calls the OS's native "open a URL" API directly --
        # unlike webbrowser.open, it never shells out to a subprocess, so it
        # can't reintroduce the console-flash bug subprocess_utils.py exists
        # to prevent in a --windowed packaged build.
        QDesktopServices.openUrl(QUrl(url))

    def _open_lablooms_url(self) -> None:
        self._open_url(brand.LABLOOMS_URL)

    def _open_support_url(self) -> None:
        self._open_url(brand.SUPPORT_URL)

    def _on_download_update(self) -> None:
        if self._update_url is None:
            return
        self._open_url(self._update_url)

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
