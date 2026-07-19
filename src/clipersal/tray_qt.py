"""System tray icon, built on Qt's QSystemTrayIcon, which integrates
directly with QApplication's event loop -- no separate thread needed.

Every action routes through the same local IPC boundary
(ipc_client.send_command) as the hotkey and `clipersal-trigger` -- all three
trigger paths are equivalent. "Open clips folder" is the one purely local
action.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from clipersal import config_store, ipc_client
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

# Same semantics and hex values as the main window's status badge (see
# theme.py): green = normal steady recording, warm taupe-grey =
# paused. A tray icon can't practically pulse for an active save the way
# the badge does, so that state is main-window-only.
_RECORDING_COLOR = QColor(76, 175, 80)  # growth green -- matches theme.GOOD, #4CAF50
_PAUSED_COLOR = QColor(166, 147, 116)  # warm taupe-grey -- matches theme.NEUTRAL, #A69374
_ICON_SIZE = 64
_QUICK_TRIM_SECONDS = 30


def _make_icon(color: QColor) -> QIcon:
    pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    margin = _ICON_SIZE // 8
    painter.drawEllipse(margin, margin, _ICON_SIZE - margin * 2, _ICON_SIZE - margin * 2)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    # (trim_arg, response-or-None) from the save worker thread -- tray menu
    # callbacks run on the GUI thread, so the actual IPC send happens off it
    # and the response comes back through this queued signal (the same
    # cross-thread rule as signals.py's AppSignals).
    _save_responded = Signal(object)

    def __init__(self, ipc_port: int, clips_dir: Path, log_path: Path | None = None, parent=None) -> None:
        super().__init__(_make_icon(_RECORDING_COLOR), parent)
        self._ipc_port = ipc_port
        self._clips_dir = clips_dir
        self._log_path = log_path or config_store.default_log_path()
        self._paused = False

        self.setToolTip("Clipersal - Recording")
        self.activated.connect(self._on_activated)
        self._save_responded.connect(self._on_save_responded)

        self._menu = QMenu()
        self._menu.addAction("Open Clipersal", self._on_show)
        self._menu.addSeparator()
        self._menu.addAction("Save now", self._on_save)
        self._menu.addAction(f"Save last {_QUICK_TRIM_SECONDS}s", self._on_save_last_30s)
        self._menu.addAction("View clips", self._on_view_clips)
        self._menu.addAction("Open clips folder", self._on_open_clips)
        self._pause_action = self._menu.addAction(self._pause_label(), self._on_toggle_pause)
        self._menu.addAction("Settings", self._on_settings)
        self._menu.addAction("View logs", self._on_view_logs)
        self._menu.addSeparator()
        self._menu.addAction("Quit", self._on_quit)
        self.setContextMenu(self._menu)

    def _send(self, command: str, arg: str | None = None, timeout: float = 5.0) -> str | None:
        try:
            response = ipc_client.send_command(command, arg=arg, port=self._ipc_port, timeout=timeout)
            log.info("Tray %s: %s", command, response)
            return response
        except ipc_client.IpcClientError as exc:
            log.warning("Tray %s failed: %s", command, exc)
            return None

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Right-click (Context) is handled automatically via setContextMenu;
        # a plain click or double-click opens/focuses the main window --
        # the same "Open Clipersal" default action as the menu's own item.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._on_show()

    def _on_save(self) -> None:
        self._start_save_worker(None)

    def _on_save_last_30s(self) -> None:
        self._start_save_worker(str(_QUICK_TRIM_SECONDS))

    def _start_save_worker(self, trim_arg: str | None) -> None:
        # A SAVE's server-side remux can legitimately run tens of seconds (up
        # to concat.py's _CONCAT_TIMEOUT, 60s) -- far past ipc_client's 5s
        # default -- and tray menu callbacks run on the GUI thread, so a
        # synchronous send froze the tray menu for the remux and could report
        # failure after 5s while the save actually completed. Worker thread
        # with a timeout above the server's own; the response returns to the
        # GUI thread via _save_responded.
        threading.Thread(target=self._save_worker, args=(trim_arg,), daemon=True).start()

    def _save_worker(self, trim_arg: str | None) -> None:
        response = self._send("SAVE", arg=trim_arg, timeout=ipc_client.SAVE_TIMEOUT)
        self._save_responded.emit((trim_arg, response))

    def _on_save_responded(self, payload: tuple) -> None:
        trim_arg, response = payload
        if response is None:
            return
        if response.startswith("OK"):
            title = "Clip saved" if trim_arg is None else f"Last {_QUICK_TRIM_SECONDS}s saved"
            self.showMessage(title, response[len("OK") :].strip() or "Saved")
        else:
            self.showMessage("Save failed", response)

    def _on_open_clips(self) -> None:
        open_folder(self._clips_dir)

    def _on_show(self) -> None:
        response = self._send("SHOW")
        if response is not None and not response.startswith("OK"):
            self.showMessage("Clipersal", response)

    def _on_view_logs(self) -> None:
        response = self._send("LOGS")
        if response is not None and not response.startswith("OK"):
            self.showMessage("Logs", response)

    def _on_view_clips(self) -> None:
        response = self._send("GALLERY")
        if response is not None and not response.startswith("OK"):
            self.showMessage("Clips", response)

    def _pause_label(self) -> str:
        return "Resume capture" if self._paused else "Pause capture"

    def _on_toggle_pause(self) -> None:
        command = "RESUME" if self._paused else "PAUSE"
        response = self._send(command)
        if response is not None and response.startswith("OK"):
            self._paused = not self._paused
            self._refresh_status()

    def _on_settings(self) -> None:
        response = self._send("SETTINGS")
        if response is not None and not response.startswith("OK"):
            self.showMessage("Settings", response)

    def _on_quit(self) -> None:
        self._send("QUIT")

    def _refresh_status(self) -> None:
        self.setIcon(_make_icon(_PAUSED_COLOR if self._paused else _RECORDING_COLOR))
        self.setToolTip(f"Clipersal - {'Paused' if self._paused else 'Recording'}")
        self._pause_action.setText(self._pause_label())
