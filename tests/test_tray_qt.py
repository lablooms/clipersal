import os
import threading
import time
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


def _drain_until(predicate, timeout: float = 5.0) -> bool:
    """Wait for an async (worker-thread) condition: tray saves run their IPC
    send on a worker and deliver the response back through a queued signal.
    Pumps sendPostedEvents(), NOT processEvents() -- the latter also fires
    every leftover test window/tray's overdue timers (each a real socket
    connect to a dead port, which can take seconds apiece), while
    sendPostedEvents() dispatches just the queued slot calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.sendPostedEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_menu_has_expected_items() -> None:
    tray = TrayIcon(ipc_port=51525, clips_dir_provider=lambda: Path("/tmp/clips"))
    labels = [action.text() for action in tray._menu.actions() if not action.isSeparator()]
    assert labels == [
        "Open Clipersal",
        "Save now",
        "Save last 30s",
        "Take screenshot",
        "View clips",
        "Open clips folder",
        "Pause capture",
        "Settings",
        "View logs",
        "Quit",
    ]


def test_on_screenshot_sends_screenshot_command_and_shows_the_filename(monkeypatch) -> None:
    sent = []

    def fake_send(command, arg=None, host="127.0.0.1", port=51525, timeout=5.0):
        sent.append({"command": command, "arg": arg, "timeout": timeout, "thread": threading.current_thread()})
        return "OK C:/clips/screenshot-20260722-010203.png"

    monkeypatch.setattr(tray_qt.ipc_client, "send_command", fake_send)
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))
    notified = []
    monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

    tray._on_screenshot()

    assert _drain_until(lambda: len(sent) == 1 and len(notified) == 1)
    assert sent[0]["command"] == "SCREENSHOT"
    assert sent[0]["arg"] is None
    # Serialized behind in-flight saves server-side, so it gets SAVE's long leash.
    assert sent[0]["timeout"] == tray_qt.ipc_client.SAVE_TIMEOUT
    assert sent[0]["thread"] is not threading.current_thread()
    assert notified[0][0] == "Screenshot saved"
    assert "screenshot-20260722-010203.png" in notified[0][1]


def test_on_screenshot_error_response_notifies(monkeypatch) -> None:
    monkeypatch.setattr(
        tray_qt.ipc_client, "send_command", lambda *a, **k: "ERROR buffer is empty -- nothing to grab yet"
    )
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))
    notified = []
    monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

    tray._on_screenshot()

    assert _drain_until(lambda: len(notified) == 1)
    assert notified[0][0] == "Screenshot failed"
    assert "buffer is empty" in notified[0][1]


def test_pause_label_toggles_after_successful_pause_and_resume() -> None:
    server = IpcServer(port=0)
    server.register("PAUSE", lambda arg: "paused")
    server.register("RESUME", lambda arg: "resumed")
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        assert tray._pause_label() == "Pause capture"

        tray._on_toggle_pause()
        # The send runs on a worker thread now; the state flips when its
        # response lands via the queued _pause_responded signal.
        assert _drain_until(lambda: tray._paused is True)
        assert tray._pause_label() == "Resume capture"
        assert tray._pause_action.text() == "Resume capture"

        tray._on_toggle_pause()
        assert _drain_until(lambda: tray._paused is False)
        assert tray._pause_action.text() == "Pause capture"
    finally:
        server.stop()


def test_pause_toggle_sends_off_the_gui_thread(monkeypatch) -> None:
    # PAUSE's server-side session stop can take seconds -- the send must not
    # run on the tray's (the app's) GUI thread.
    sent = []

    def fake_send(command, arg=None, host="127.0.0.1", port=51525, timeout=5.0):
        sent.append({"command": command, "thread": threading.current_thread()})
        return "OK paused"

    monkeypatch.setattr(tray_qt.ipc_client, "send_command", fake_send)
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))

    tray._on_toggle_pause()

    assert _drain_until(lambda: len(sent) == 1 and tray._paused is True)
    assert sent[0]["command"] == "PAUSE"
    assert sent[0]["thread"] is not threading.current_thread()


def test_pause_click_while_in_flight_is_ignored() -> None:
    # A second click before the first response lands must not send a second
    # command -- two interleaved toggles could invert each other's state.
    gate = threading.Event()
    calls = []
    server = IpcServer(port=0)

    def handle(command):
        def _handler(arg):
            calls.append(command)
            gate.wait(5)
            return "ok"

        return _handler

    server.register("PAUSE", handle("PAUSE"))
    server.register("RESUME", handle("RESUME"))
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        tray._on_toggle_pause()  # PAUSE -- the worker blocks on the gate
        tray._on_toggle_pause()  # still in flight -> ignored
        gate.set()
        assert _drain_until(lambda: tray._paused is True)
        assert calls == ["PAUSE"]
    finally:
        gate.set()
        server.stop()


def test_pause_state_unchanged_when_ipc_unreachable() -> None:
    tray = TrayIcon(ipc_port=1, clips_dir_provider=lambda: Path("/tmp/clips"))
    tray._on_toggle_pause()
    assert _drain_until(lambda: not tray._pause_in_flight)
    assert tray._paused is False


def test_pause_state_unchanged_on_error_response() -> None:
    server = IpcServer(port=0)

    def boom(arg):
        raise RuntimeError("nope")

    server.register("PAUSE", boom)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        tray._on_toggle_pause()
        assert _drain_until(lambda: not tray._pause_in_flight)
        assert tray._paused is False
    finally:
        server.stop()


def test_on_settings_success_does_not_notify(monkeypatch) -> None:
    server = IpcServer(port=0)
    server.register("SETTINGS", lambda arg: "opening settings window")
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
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
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
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
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_save_last_30s()

        # The send happens on a worker thread now; the notification lands via
        # the queued _save_responded signal.
        assert _drain_until(lambda: len(notified) == 1)
        assert received_args == ["30"]
        assert notified[0][0] == "Last 30s saved"
    finally:
        server.stop()


def test_on_save_sends_with_raised_timeout_on_worker_thread(monkeypatch) -> None:
    sent = []

    def fake_send(command, arg=None, host="127.0.0.1", port=51525, timeout=5.0):
        sent.append(
            {"command": command, "arg": arg, "port": port, "timeout": timeout, "thread": threading.current_thread()}
        )
        return "OK C:/clips/clip-1.mp4"

    monkeypatch.setattr(tray_qt.ipc_client, "send_command", fake_send)
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))
    notified = []
    monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

    tray._on_save()

    assert _drain_until(lambda: len(sent) == 1 and len(notified) == 1)
    assert sent[0]["command"] == "SAVE"
    assert sent[0]["arg"] is None
    # The timeout must clear the server-side remux limit (concat's 60s), not
    # ipc_client's 5s default -- otherwise a slow-but-successful save gets
    # reported as a failure.
    assert sent[0]["timeout"] == tray_qt.ipc_client.SAVE_TIMEOUT
    assert sent[0]["thread"] is not threading.current_thread()  # not the GUI thread
    assert notified[0][0] == "Clip saved"


def test_on_save_error_response_notifies(monkeypatch) -> None:
    monkeypatch.setattr(
        tray_qt.ipc_client, "send_command", lambda *a, **k: "ERROR Not enough has been captured yet"
    )
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))
    notified = []
    monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

    tray._on_save()

    assert _drain_until(lambda: len(notified) == 1)
    assert notified[0][0] == "Save failed"
    assert "Not enough has been captured" in notified[0][1]


def test_on_open_clips_opens_clips_dir(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(tray_qt, "open_folder", lambda path: opened.append(path))
    clips_dir = Path("/tmp/clips")
    tray = TrayIcon(ipc_port=1, clips_dir_provider=lambda: clips_dir)

    tray._on_open_clips()

    assert opened == [clips_dir]


def test_on_open_clips_opens_the_providers_current_dir(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(tray_qt, "open_folder", lambda path: opened.append(path))
    current = {"clips_dir": Path("/tmp/clips-a")}
    tray = TrayIcon(ipc_port=1, clips_dir_provider=lambda: current["clips_dir"])

    tray._on_open_clips()
    # A Settings clips-folder change must be picked up live -- the tray holds
    # a provider, not a Path frozen at construction.
    current["clips_dir"] = Path("/tmp/clips-b")
    tray._on_open_clips()

    assert opened == [Path("/tmp/clips-a"), Path("/tmp/clips-b")]


def test_on_show_error_notifies(monkeypatch) -> None:
    server = IpcServer(port=0)

    def boom(arg):
        raise RuntimeError("no display available")

    server.register("SHOW", boom)
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        notified = []
        monkeypatch.setattr(tray, "showMessage", lambda title, message, *a, **k: notified.append((title, message)))

        tray._on_show()

        assert len(notified) == 1
        assert "no display available" in notified[0][1]
    finally:
        server.stop()


def test_activated_trigger_and_double_click_open_window(monkeypatch) -> None:
    tray = TrayIcon(ipc_port=1, clips_dir_provider=lambda: Path("/tmp/clips"))
    calls = []
    monkeypatch.setattr(tray, "_on_show", lambda: calls.append("show"))

    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.Trigger)
    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.DoubleClick)
    tray._on_activated(tray_qt.QSystemTrayIcon.ActivationReason.Context)

    assert calls == ["show", "show"]


def test_tray_icon_defaults_log_path_when_not_given() -> None:
    tray = TrayIcon(ipc_port=1, clips_dir_provider=lambda: Path("/tmp/clips"))
    assert tray._log_path == tray_qt.config_store.default_log_path()


def test_about_to_show_resyncs_pause_state_from_status() -> None:
    # Pausing (or resuming) from the main window / hotkey / trigger never
    # touched the tray's own _paused flag -- the menu must re-sync from the
    # server's STATUS each time it opens instead of showing a stale label.
    state = {"status": "PAUSED"}
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: state["status"])
    server.start()
    try:
        tray = TrayIcon(ipc_port=server.port, clips_dir_provider=lambda: Path("/tmp/clips"))
        assert tray._paused is False

        tray._menu.aboutToShow.emit()

        assert _drain_until(lambda: tray._paused is True)
        assert tray._pause_action.text() == "Resume capture"
        assert tray.toolTip() == "Clipersal - Paused"

        state["status"] = "RECORDING"
        tray._menu.aboutToShow.emit()

        assert _drain_until(lambda: tray._paused is False)
        assert tray._pause_action.text() == "Pause capture"
        assert tray.toolTip() == "Clipersal - Recording"

        # A crashed ffmpeg auto-restart budget presents as paused too:
        # capture is down, and RESUME is the recovery action for both.
        state["status"] = "CRASHED"
        tray._menu.aboutToShow.emit()

        assert _drain_until(lambda: tray._paused is True)
        assert tray._pause_action.text() == "Resume capture"
    finally:
        server.stop()


def test_about_to_show_resync_failure_keeps_last_known_state(monkeypatch) -> None:
    sent = []

    def fake_send(command, arg=None, host="127.0.0.1", port=51525, timeout=5.0):
        sent.append({"command": command, "timeout": timeout, "thread": threading.current_thread()})
        raise tray_qt.ipc_client.IpcClientError("no server")

    monkeypatch.setattr(tray_qt.ipc_client, "send_command", fake_send)
    tray = TrayIcon(ipc_port=12345, clips_dir_provider=lambda: Path("/tmp/clips"))
    tray._paused = True
    tray._refresh_status()
    responses = []
    tray._status_responded.connect(responses.append)

    tray._menu.aboutToShow.emit()

    assert _drain_until(lambda: len(responses) == 1)
    assert responses == [None]  # the failure came back as None, not an exception on the GUI thread
    assert sent[0]["command"] == "STATUS"
    # The tray's regular-command timeout pattern (the 5s _send default), not
    # SAVE's 70s leash -- and off the GUI thread, so the menu never blocks.
    assert sent[0]["timeout"] == 5.0
    assert sent[0]["thread"] is not threading.current_thread()
    # Stale state survives an unreachable server.
    assert tray._paused is True
    assert tray._pause_action.text() == "Resume capture"
