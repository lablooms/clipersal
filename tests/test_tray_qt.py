import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal import tray_qt
from clipersal.ipc import IpcServer
from clipersal.tray_qt import TrayIcon


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_menu_has_expected_items() -> None:
    tray = TrayIcon(ipc_port=51525, clips_dir=Path("/tmp/clips"))
    labels = [action.text() for action in tray._menu.actions() if not action.isSeparator()]
    assert labels == [
        "Open Clipersal",
        "Save now",
        "Save last 30s",
        "View clips",
        "Open clips folder",
        "Pause capture",
        "Settings",
        "View logs",
        "Quit",
    ]


def test_pause_label_toggles_after_successful_pause_and_resume() -> None:
    server = IpcServer(port=0)
    server.register("PAUSE", lambda arg: "paused")
    server.register("RESUME", lambda arg: "resumed")
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        assert tray._pause_label() == "Pause capture"

        tray._on_toggle_pause()
        assert tray._paused is True
        assert tray._pause_label() == "Resume capture"
        assert tray._pause_action.text() == "Resume capture"

        tray._on_toggle_pause()
        assert tray._paused is False
        assert tray._pause_action.text() == "Pause capture"
    finally:
        server.stop()


def test_pause_state_unchanged_when_ipc_unreachable() -> None:
    tray = TrayIcon(ipc_port=1, clips_dir=Path("/tmp/clips"))
    tray._on_toggle_pause()
    assert tray._paused is False


def test_pause_state_unchanged_on_error_response() -> None:
    server = IpcServer(port=0)

    def boom(arg):
        raise RuntimeError("nope")

    server.register("PAUSE", boom)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        tray._on_toggle_pause()
        assert tray._paused is False
    finally:
        server.stop()


def test_on_settings_success_does_not_notify(monkeypatch) -> None:
    server = IpcServer(port=0)
    server.register("SETTINGS", lambda arg: "opening settings window")
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_settings()

        assert notified == []
    finally:
        server.stop()


def test_on_settings_error_notifies(monkeypatch) -> None:
    server = IpcServer(port=0)

    def boom(arg):
        raise RuntimeError("no display available")

    server.register("SETTINGS", boom)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_settings()

        assert len(notified) == 1
        assert "no display available" in notified[0][1]
    finally:
        server.stop()


def test_on_save_last_30s_sends_trim_argument(monkeypatch) -> None:
    server = IpcServer(port=0)
    received_args = []

    def handle_save(arg):
        received_args.append(arg)
        return "OK C:/clips/clip-trimmed.mp4"

    server.register("SAVE", handle_save)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_save_last_30s()

        assert received_args == ["30"]
        assert len(notified) == 1
        assert notified[0][0] == "Last 30s saved"
    finally:
        server.stop()


def test_on_open_clips_opens_clips_dir(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(tray_qt, "open_folder", lambda path: opened.append(path))
    clips_dir = Path("/tmp/clips")
    tray = TrayIcon(ipc_port=1, clips_dir=clips_dir)

    tray._on_open_clips()

    assert opened == [clips_dir]


def test_on_show_error_notifies(monkeypatch) -> None:
    server = IpcServer(port=0)

    def boom(arg):
        raise RuntimeError("no display available")

    server.register("SHOW", boom)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir=Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_show()

        assert len(notified) == 1
        assert "no display available" in notified[0][1]
    finally:
        server.stop()


def test_activated_trigger_and_double_click_open_window(monkeypatch) -> None:
    tray = TrayIcon(ipc_port=1, clips_dir=Path("/tmp/clips"))
    calls = []
    monkeypatch.setattr(tray, "_on_show", lambda: calls.append("show"))

    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.Trigger)
    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.DoubleClick)
    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.Context)

    assert calls == ["show", "show"]


def test_tray_icon_defaults_log_path_when_not_given() -> None:
    tray = TrayIcon(ipc_port=1, clips_dir=Path("/tmp/clips"))
    assert tray._log_path == tray_qt.config_store.default_log_path()
