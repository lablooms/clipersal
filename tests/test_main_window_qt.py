import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from clipersal import main_window_qt
from clipersal.config import Config
from clipersal.ipc import IpcServer
from clipersal.main_window_qt import MainWindow
from clipersal.signals import AppSignals


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(
    tmp_path: Path,
    tray_enabled: bool = True,
    on_quit=None,
    ipc_port: int = 1,
    app_signals=None,
    clips_dir_provider=None,
    diagnostics_facts_provider=None,
) -> MainWindow:
    config = Config(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    return MainWindow(
        config, ipc_port, None, "libx264", lambda values: None, "ffmpeg",
        clips_dir_provider or (lambda: tmp_path / "clips"),
        tmp_path / "log.txt", tray_enabled, on_quit or (lambda: None), app_signals,
        diagnostics_facts_provider=diagnostics_facts_provider,
    )


def test_all_three_tabs_exist(tmp_path: Path) -> None:
    # Logs is no longer a top-level tab -- it lives inside Settings now.
    win = _make_window(tmp_path)
    assert set(win._tabs.keys()) == {"home", "clips", "settings"}
    assert win._active_tab == "home"


def test_select_tab_logs_routes_to_the_settings_logs_subtab(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.select_tab("logs")
    assert win._active_tab == "settings"
    assert win._content_stack.currentWidget() is win._tabs["settings"]
    assert win._nav_buttons["settings"].isChecked() is True
    settings = win._tabs["settings"]
    assert settings.tabs.currentWidget() is settings.logs_tab


def test_select_settings_subtab_selects_settings_and_the_named_subtab(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.select_settings_subtab("logs")
    assert win._active_tab == "settings"
    settings = win._tabs["settings"]
    assert settings.tabs.currentWidget() is settings.logs_tab
    # A field sub-tab is equally selectable by name (case-insensitive).
    win.select_settings_subtab("About")
    assert settings.tabs.currentIndex() == [settings.tabs.tabText(i) for i in range(settings.tabs.count())].index("About")


def test_select_tab_switches_stack_and_nav_checked_state(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.select_tab("settings")
    assert win._active_tab == "settings"
    assert win._content_stack.currentWidget() is win._tabs["settings"]
    assert win._nav_buttons["settings"].isChecked() is True
    assert win._nav_buttons["home"].isChecked() is False


def test_select_tab_ignores_unknown_name(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.select_tab("nonexistent")
    assert win._active_tab == "home"


def test_select_clips_tab_triggers_refresh(tmp_path: Path, monkeypatch) -> None:
    win = _make_window(tmp_path)
    called = []
    monkeypatch.setattr(win._tabs["clips"], "refresh", lambda: called.append(True))
    win.select_tab("clips")
    assert called == [True]


def test_close_event_hides_when_tray_enabled(tmp_path: Path) -> None:
    quit_calls = []
    win = _make_window(tmp_path, tray_enabled=True, on_quit=lambda: quit_calls.append(True))
    win.show()
    event = QCloseEvent()
    win.closeEvent(event)
    assert event.isAccepted() is False
    assert quit_calls == []
    assert win.isHidden() is True


def test_close_event_quits_when_tray_disabled(tmp_path: Path) -> None:
    quit_calls = []
    win = _make_window(tmp_path, tray_enabled=False, on_quit=lambda: quit_calls.append(True))
    event = QCloseEvent()
    win.closeEvent(event)
    assert event.isAccepted() is True
    assert quit_calls == [True]


class _FakeListener:
    """Stands in for pynput.keyboard.Listener -- tests never hook real global
    keyboard input (same rule as test_hotkey_widget_qt.py).
    """

    def __init__(self, on_press=None, on_release=None):
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_close_event_cancels_a_mid_record_settings_hotkey_capture(tmp_path: Path, monkeypatch) -> None:
    # The Settings tab's hotkey recorder runs an OS-wide pynput Listener;
    # closing the window to the tray mid-record must tear it down (via the
    # HotkeyField's hideEvent defense), not leak it for the rest of the
    # process. Privacy-sensitive -- see hotkey_widget_qt.py.
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    win = _make_window(tmp_path, tray_enabled=True)
    win.select_tab("settings")
    win.show()
    settings = win._tabs["settings"]
    # The hotkey field lives on the "Saving" sub-tab now -- switch to it so
    # the field is actually visible, as it would be for a real user (only a
    # VISIBLE field gets the hideEvent that cancels the recording).
    labels = [settings.tabs.tabText(i) for i in range(settings.tabs.count())]
    settings.tabs.setCurrentIndex(labels.index("Saving"))
    hotkey_field = settings.hotkey_field
    hotkey_field.record_button.click()
    listener = hotkey_field._listener
    assert hotkey_field.is_recording() is True

    win.closeEvent(QCloseEvent())  # hides to the tray, like the real ✕

    assert win.isHidden() is True
    assert hotkey_field.is_recording() is False
    assert listener.stopped is True


# ---- status polling ---------------------------------------------------------


def _stats_payload(state: str = "RECORDING", **overrides: str) -> str:
    fields = {
        "state": state,
        "uptime": "3725.0",
        "segments": "20",
        "buffer_bytes": str(40 * (1 << 20)),
        "encoder": "libx264",
        "buffer_seconds": "60",
        "clips_free_bytes": str(10 * (1 << 30)),
        "clips_count": "3",
    }
    fields.update(overrides)
    return "|".join(f"{key}={value}" for key, value in fields.items())


def _stats_server(state: str = "RECORDING", **overrides: str) -> IpcServer:
    payload = _stats_payload(state, **overrides)
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: payload)
    server.start()
    return server


def test_poll_status_recording(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "RECORDING")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._status_label.text() == "Recording"
        assert win._pause_button.text() == "Pause capture"
        assert win.windowTitle() == "Clipersal"
    finally:
        server.stop()


def test_poll_status_paused(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "PAUSED")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._status_label.text() == "Paused"
        assert win._pause_button.text() == "Resume capture"
        assert win.windowTitle() == "Clipersal — Paused"
    finally:
        server.stop()


def test_poll_status_crashed(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "CRASHED")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._status_label.text() == "Capture stopped -- see Settings→Logs"
        assert win._pause_button.text() == "Resume capture"
        assert win.windowTitle() == "Clipersal — Capture stopped"
    finally:
        server.stop()


def test_poll_status_stats_drives_the_second_meta_line(tmp_path: Path) -> None:
    server = _stats_server()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        line = win._status_stats_label.text()
        # uptime 3725s -> 1:02:05; fill = 20 segments x 2s segment_seconds.
        assert "Up 1:02:05" in line
        assert "Buffer fill ~40s/60s (20 segments, 40 MB)" in line
        assert "libx264" in line
        assert "10.0 GB free" in line
        # Line 1 (buffer + clips dir) is untouched by the poll.
        assert win._status_meta_prefix_label.text() + win._status_meta_label.text() == win._default_status_meta()
    finally:
        server.stop()


def test_poll_status_stats_fill_is_capped_at_buffer_seconds(tmp_path: Path) -> None:
    server = _stats_server(segments="50")  # 50 x 2s = 100s > 60s buffer
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert "Buffer fill ~60s/60s (50 segments" in win._status_stats_label.text()
    finally:
        server.stop()


def test_poll_status_stats_omits_missing_fields_without_none(tmp_path: Path) -> None:
    server = _stats_server(uptime="", segments="", buffer_bytes="", encoder="", clips_free_bytes="")
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        line = win._status_stats_label.text()
        assert "None" not in line
        # Only the state was known -- every stat field degraded away.
        assert line == ""
        assert win._status_label.text() == "Recording"
    finally:
        server.stop()


def test_poll_status_pause_label_flips_with_stats_state(tmp_path: Path) -> None:
    state = {"value": "RECORDING"}
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload(state["value"]))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._pause_button.text() == "Pause capture"
        state["value"] = "PAUSED"
        win._poll_status()
        assert win._pause_button.text() == "Resume capture"
        state["value"] = "RECORDING"
        win._poll_status()
        assert win._pause_button.text() == "Pause capture"
    finally:
        server.stop()


def test_poll_status_skipped_while_pulsing(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "PAUSED")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._status_label.setText("Recording")
        win._pulsing = True
        win._poll_status()
        assert win._status_label.text() == "Recording"  # unchanged -- poll was skipped
    finally:
        server.stop()


def test_poll_status_unreachable_leaves_state_unchanged(tmp_path: Path) -> None:
    win = _make_window(tmp_path, ipc_port=1)
    win._status_label.setText("Recording")
    win._poll_status()
    assert win._status_label.text() == "Recording"


# ---- crash banner -----------------------------------------------------------


def test_crash_banner_follows_stats_state_and_title_recovers(tmp_path: Path) -> None:
    state = {"value": "RECORDING"}
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload(state["value"]))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        assert win._crash_banner.isHidden() is True

        state["value"] = "CRASHED"
        win._poll_status()
        assert win._crash_banner.isHidden() is False
        assert win.windowTitle() == "Clipersal — Capture stopped"

        state["value"] = "PAUSED"
        win._poll_status()
        assert win._crash_banner.isHidden() is True
        assert win.windowTitle() == "Clipersal — Paused"

        state["value"] = "RECORDING"
        win._poll_status()
        assert win._crash_banner.isHidden() is True
        assert win.windowTitle() == "Clipersal"
    finally:
        server.stop()


def test_crash_banner_restart_button_sends_resume(tmp_path: Path) -> None:
    received = []
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("CRASHED"))
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._crash_banner.isHidden() is False

        win._crash_restart_button.click()
        assert _wait_for(lambda: received == ["RESUME"])
    finally:
        server.stop()


def test_crash_banner_view_logs_button_selects_settings_logs_subtab(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._crash_banner.setVisible(True)
    buttons = win._crash_banner.findChildren(type(win._crash_restart_button))
    view_logs = next(b for b in buttons if b.text() == "View logs")
    view_logs.click()
    assert win._active_tab == "settings"
    settings = win._tabs["settings"]
    assert settings.tabs.currentWidget() is settings.logs_tab


# ---- crash-report prompt -----------------------------------------------------


def _prompt_button(win: MainWindow, text: str):
    return next(b for b in win._crash_prompt.buttons() if b.text() == text)


def test_crash_prompt_shows_once_per_episode_and_rearms(tmp_path: Path) -> None:
    state = {"value": "RECORDING"}
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload(state["value"]))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._crash_prompt is None

        state["value"] = "CRASHED"
        win._poll_status()
        first = win._crash_prompt
        assert first is not None
        assert first.windowTitle() == "Capture stopped"

        win._poll_status()  # still crashed: no second prompt for this episode
        assert win._crash_prompt is first

        state["value"] = "RECORDING"  # episode over -> the edge re-arms
        win._poll_status()
        assert win._crash_prompt is first  # untouched while open

        state["value"] = "CRASHED"
        win._poll_status()
        assert win._crash_prompt is not None
        assert win._crash_prompt is not first  # a new episode gets a new prompt
    finally:
        server.stop()


def test_crash_prompt_not_now_just_closes(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("CRASHED"))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._crash_prompt is not None
        _prompt_button(win, "Not now").click()
        assert win._crash_prompt is None
    finally:
        server.stop()


def test_crash_prompt_restart_sends_resume(tmp_path: Path) -> None:
    received = []
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("CRASHED"))
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        _prompt_button(win, "Restart capture").click()
        assert _wait_for(lambda: received == ["RESUME"])
    finally:
        server.stop()


def test_crash_prompt_export_zip_calls_the_shared_helper(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(MainWindow, "_export_diagnostics_with_dialog", lambda self: calls.append(True))
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("CRASHED"))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        _prompt_button(win, "Export zip").click()
        assert calls == [True]
    finally:
        server.stop()


def test_crash_prompt_send_report_opens_a_prefilled_issue(tmp_path: Path, monkeypatch) -> None:
    import urllib.parse

    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

    (tmp_path / "log.txt").write_text(
        "".join(f"app log line {i}\n" for i in range(300)), encoding="utf-8"
    )
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir(exist_ok=True)
    (buffer_dir / "ffmpeg.log").write_text(
        "".join(f"ffmpeg log line {i}\n" for i in range(200)), encoding="utf-8"
    )

    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("CRASHED"))
    server.start()
    try:
        win = _make_window(
            tmp_path,
            ipc_port=server.port,
            diagnostics_facts_provider=lambda: {"os": "test-os", "encoder": "libx264"},
        )
        win._poll_status()
        _prompt_button(win, "Send report").click()
    finally:
        server.stop()

    assert len(opened) == 1
    url = opened[0]
    assert url.startswith(main_window_qt.CRASH_REPORT_ISSUES_URL + "?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["title"] == ["Crash report"]
    body = query["body"][0]
    assert "os: test-os" in body
    assert "encoder: libx264" in body
    assert "app log line 299" in body  # the app log tail made it in
    assert "ffmpeg log line 199" in body  # and the ffmpeg log tail


def test_crash_report_body_is_capped_and_keeps_the_newest_lines() -> None:
    facts = {"os": "test-os"}
    app_lines = [f"app log line {i} " + "x" * 90 for i in range(500)]  # ~50 KB of log
    body = main_window_qt._build_crash_report_body(facts, app_lines, [])
    assert len(body) <= main_window_qt._CRASH_REPORT_BODY_LIMIT
    assert "os: test-os" in body  # the facts header always survives
    assert "app log line 499" in body  # the newest lines survive the cap
    assert "app log line 0 " not in body  # the oldest are dropped first


def test_tail_lines_reads_only_the_end_of_a_big_log(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    line_count = 20_000
    # Binary write: exactly 16 bytes per line, no platform newline translation.
    path.write_bytes(b"".join(f"log-line-{i:06d}\n".encode() for i in range(line_count)))

    lines = main_window_qt._tail_lines(path, 150)
    assert len(lines) == 150
    assert lines[-1] == f"log-line-{line_count - 1:06d}"
    assert lines[0] == f"log-line-{line_count - 150:06d}"

    # The size guard bounds the read window: with max_bytes far below the
    # file size, only a slice of the end is ever read (one partial first
    # line dropped).
    lines = main_window_qt._tail_lines(path, 150, max_bytes=1024)
    assert len(lines) == 63  # 1024/16 = 64 lines, minus the partial first
    assert lines[-1] == f"log-line-{line_count - 1:06d}"


def test_tail_lines_missing_file_returns_no_lines(tmp_path: Path) -> None:
    assert main_window_qt._tail_lines(tmp_path / "nope.log", 150) == []


# ---- low-disk banner ---------------------------------------------------------


def test_disk_banner_shows_below_warn_threshold_with_free_space_text(tmp_path: Path) -> None:
    server = _stats_server(clips_free_bytes=str((1 << 30) // 2))  # 0.5 GiB
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._disk_banner.isHidden() is False
        assert "0.5 GB free" in win._disk_hint_label.text()
    finally:
        server.stop()


def test_disk_banner_hysteresis_and_dismiss_rearm(tmp_path: Path) -> None:
    free = {"value": 10 * (1 << 30)}
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload(clips_free_bytes=str(free["value"])))
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)

        free["value"] = (1 << 30) // 2  # 0.5 GiB -> shows
        win._poll_status()
        assert win._disk_banner.isHidden() is False

        free["value"] = int(1.2 * (1 << 30))  # dead band: stays shown
        win._poll_status()
        assert win._disk_banner.isHidden() is False

        win._disk_dismiss_button.click()  # dismissed -> hides and stays hidden
        assert win._disk_banner.isHidden() is True
        win._poll_status()
        assert win._disk_banner.isHidden() is True

        free["value"] = (1 << 30) // 2  # still below warn while dismissed
        win._poll_status()
        assert win._disk_banner.isHidden() is True

        free["value"] = 2 * (1 << 30)  # over the high-water mark: re-armed
        win._poll_status()
        assert win._disk_banner.isHidden() is True

        free["value"] = int(1.2 * (1 << 30))  # dead band on the way down: stays hidden
        win._poll_status()
        assert win._disk_banner.isHidden() is True

        free["value"] = (1 << 30) // 2  # below warn again -> shows again
        win._poll_status()
        assert win._disk_banner.isHidden() is False
    finally:
        server.stop()


def test_disk_banner_untouched_when_free_space_unknown(tmp_path: Path) -> None:
    server = _stats_server(clips_free_bytes="")
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._disk_banner.setVisible(True)
        win._poll_status()
        assert win._disk_banner.isHidden() is False  # no data -> left as-is
    finally:
        server.stop()


def test_toggle_pause_sends_pause_when_recording(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("PAUSE", lambda arg: received.append("PAUSE") or "paused")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._capture_state = "RECORDING"
        win._on_toggle_pause()
        assert _wait_for(lambda: received == ["PAUSE"])
    finally:
        server.stop()


def test_toggle_pause_sends_resume_when_paused(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._capture_state = "PAUSED"
        win._on_toggle_pause()
        assert _wait_for(lambda: received == ["RESUME"])
    finally:
        server.stop()


def test_toggle_pause_sends_resume_when_crashed(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._capture_state = "CRASHED"
        win._on_toggle_pause()
        assert _wait_for(lambda: received == ["RESUME"])
    finally:
        server.stop()


def test_pause_button_click_uses_the_polled_state(tmp_path: Path) -> None:
    received = []
    server = IpcServer(port=0)
    server.register("STATS", lambda arg: _stats_payload("PAUSED"))
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._pause_button.text() == "Resume capture"
        win._pause_button.click()
        assert _wait_for(lambda: received == ["RESUME"])
    finally:
        server.stop()


# ---- save buttons (worker thread + failure display) --------------------------


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Wait for an async (worker-thread) condition: the save buttons run
    their IPC send on a worker and deliver the outcome back through a queued
    signal. Pumps sendPostedEvents(), NOT processEvents() -- the latter also
    fires every leftover test window's overdue status/log timers (each a real
    socket connect to a dead port, which can take seconds apiece), while
    sendPostedEvents() dispatches just the queued slot calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.sendPostedEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_save_now_button_sends_save_on_worker_with_raised_timeout(tmp_path: Path, monkeypatch) -> None:
    sent = []

    def fake_send(command, arg=None, host="127.0.0.1", port=51525, timeout=5.0):
        sent.append(
            {"command": command, "arg": arg, "port": port, "timeout": timeout, "thread": threading.current_thread()}
        )
        return "OK C:/clips/clip-1.mp4"

    monkeypatch.setattr(main_window_qt.ipc_client, "send_command", fake_send)

    win = _make_window(tmp_path)
    win._save_now_button.click()

    assert _wait_for(lambda: len(sent) == 1)
    assert sent[0]["command"] == "SAVE"
    assert sent[0]["arg"] is None
    # The timeout must clear the server-side remux limit (concat's 60s), not
    # ipc_client's 5s default -- otherwise a slow-but-successful save gets
    # reported as a failure.
    assert sent[0]["timeout"] == main_window_qt.ipc_client.SAVE_TIMEOUT
    assert sent[0]["thread"] is not threading.current_thread()  # not the GUI thread


def test_home_actions_row_has_only_pause_and_save_now(tmp_path: Path) -> None:
    # The 15/30/60s quick-saves were decluttered off Home (they stay on the
    # tray menu, the quick-save hotkeys, and `clipersal-trigger save --trim N`).
    from PySide6.QtWidgets import QPushButton

    win = _make_window(tmp_path)
    assert not hasattr(win, "_save_15s_button")
    assert not hasattr(win, "_save_30s_button")
    assert not hasattr(win, "_save_60s_button")
    home_button_texts = [b.text() for b in win._tabs["home"].findChildren(QPushButton)]
    assert "Save last 15s" not in home_button_texts
    assert "Save last 30s" not in home_button_texts
    assert "Save last 60s" not in home_button_texts
    assert win._pause_button.text() == "Pause capture"
    assert win._save_now_button.objectName() == "primary"


def test_failed_save_is_shown_in_status_meta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_window_qt.ipc_client,
        "send_command",
        lambda *a, **k: "ERROR Not enough has been captured yet to save a clip -- wait a few seconds and try again.",
    )
    signals = AppSignals()
    win = _make_window(tmp_path, app_signals=signals)
    # Mirrors cli.py's wiring of AppSignals.save_failed.
    signals.save_failed.connect(win.on_save_failed)
    default_meta = win._status_meta_label.text()

    win._save_now_button.click()

    assert _wait_for(lambda: win._status_meta_label.text() != default_meta)
    text = win._status_meta_label.text()
    assert "Save failed" in text
    assert "Not enough has been captured" in text
    # The failure takes over the whole meta line, prefix included.
    assert win._status_meta_prefix_label.text() == ""


def test_save_completed_restores_status_meta_after_failure(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    default_meta = win._status_meta_label.text()
    default_prefix = win._status_meta_prefix_label.text()

    win.on_save_failed("boom")
    assert "Save failed" in win._status_meta_label.text()

    win.on_save_completed()
    assert win._status_meta_label.text() == default_meta
    assert win._status_meta_prefix_label.text() == default_prefix


def test_status_meta_elides_only_the_clips_dir_middle(tmp_path: Path) -> None:
    # The old single middle-elided label chopped into the buffer text itself
    # ("60…ight\clips"). Now the "Buffer: Ns ·" prefix is a fixed label and
    # only the clips_dir label elides -- middle, keeping the path's both ends.
    from PySide6.QtWidgets import QLabel

    win = _make_window(tmp_path)
    assert win._status_meta_prefix_label.text() == f"Buffer: {win._config.buffer_seconds}s   ·   "
    full_path = str(tmp_path / "clips")
    assert win._status_meta_label.text() == full_path

    win._status_meta_label.resize(60, 20)  # far narrower than the path
    displayed = QLabel.text(win._status_meta_label)  # the elided on-screen text

    assert displayed != full_path
    assert "…" in displayed
    assert win._status_meta_prefix_label.text().startswith("Buffer:")  # never elided


# ---- save-completed slot ----------------------------------------------------


def test_on_save_completed_pulses_and_refreshes(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.on_save_completed()
    assert win._pulsing is True
    assert win._status_label.text() == "Saving…"


def test_pulse_settles_back_to_recording(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._run_pulse(step=main_window_qt._PULSE_STEPS)
    assert win._pulsing is False
    assert win._status_label.text() == "Recording"


# ---- recent clips ------------------------------------------------------------


def test_refresh_recent_clips_shows_empty_message(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    assert win._recent_thumb_labels == {}
    assert win._recent_strip.count() == 3  # sprig accent + empty-hint label + stretch
    hint = win._recent_strip.itemAt(1).widget()
    assert hint.text() == "no record of your bloom-bloom moments, yet."


def test_refresh_recent_clips_lists_clips_newest_first(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    older = clips_dir / "clip-old.mp4"
    newer = clips_dir / "clip-new.mp4"
    older.write_bytes(b"x")
    newer.write_bytes(b"x")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    win = _make_window(tmp_path)
    assert list(win._recent_thumb_labels.keys()) == [newer, older]


def test_refresh_recent_clips_skips_a_clip_that_vanishes_mid_listing(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    survivor = clips_dir / "clip-survivor.mp4"
    ghost = clips_dir / "clip-ghost.mp4"
    survivor.write_bytes(b"x")
    ghost.write_bytes(b"x")
    os.utime(survivor, (1000, 1000))
    os.utime(ghost, (2000, 2000))
    real_stat = Path.stat

    def stat_that_deletes_ghost(self, *args, **kwargs):
        # The retention sweep (on the IPC thread) or an external delete can
        # remove a clip after the glob but before the sort-key stat.
        if self == ghost:
            ghost.unlink()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_deletes_ghost)

    win = _make_window(tmp_path)  # must not raise FileNotFoundError

    assert list(win._recent_thumb_labels.keys()) == [survivor]


def test_recent_clips_and_status_meta_follow_live_clips_dir_provider(tmp_path: Path) -> None:
    dir_a = tmp_path / "clips-a"
    dir_b = tmp_path / "clips-b"
    dir_b.mkdir(parents=True)
    clip_b = dir_b / "clip-b.mp4"
    clip_b.write_bytes(b"x")
    current = {"clips_dir": dir_a}

    win = _make_window(tmp_path, clips_dir_provider=lambda: current["clips_dir"])
    assert str(dir_a) in win._default_status_meta()

    # A Settings clips-folder change must be picked up live -- the window
    # holds a provider, not a Path frozen at construction (which used to show
    # the OLD folder until an app restart).
    current["clips_dir"] = dir_b
    win._refresh_recent_clips()
    assert list(win._recent_thumb_labels.keys()) == [clip_b]
    assert str(dir_b) in win._default_status_meta()


def test_apply_recent_thumbnail_sets_pixmap(tmp_path: Path) -> None:
    from PySide6.QtGui import QPixmap

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")

    win = _make_window(tmp_path)
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")

    win._apply_recent_thumbnail(clip_path, image_path)
    assert not win._recent_thumb_labels[clip_path].pixmap().isNull()


class _FakeSignal:
    """Stand-in for a Qt signal on a fake dialog: connect/emit, nothing else
    (mirrors test_gallery_window_qt.py's fake-player pattern)."""

    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)


class _FakePlayerDialog:
    """PlayerDialog replacement: records construction, exposes the two
    signals the window connects, and never touches QtMultimedia."""

    instances = []

    def __init__(self, clip_path, ffmpeg_path=None, parent=None, autoplay=True):
        self.clip_path = clip_path
        self.ffmpeg_path = ffmpeg_path
        self.parent_widget = parent
        self.autoplay = autoplay
        self.trim_exported = _FakeSignal()
        self.destroyed = _FakeSignal()
        self.delete_on_close = False
        self.shown = False
        _FakePlayerDialog.instances.append(self)

    def setAttribute(self, attribute) -> None:
        from PySide6.QtCore import Qt

        if attribute == Qt.WidgetAttribute.WA_DeleteOnClose:
            self.delete_on_close = True

    def show(self) -> None:
        self.shown = True


@pytest.fixture()
def fake_player(monkeypatch):
    _FakePlayerDialog.instances = []
    monkeypatch.setattr(main_window_qt.player_qt, "PlayerDialog", _FakePlayerDialog)
    monkeypatch.setattr(main_window_qt.player_qt, "multimedia_available", lambda: True)
    return _FakePlayerDialog


def _recent_cards(win: MainWindow) -> list:
    return [
        win._recent_strip.itemAt(i).widget()
        for i in range(win._recent_strip.count())
        if isinstance(win._recent_strip.itemAt(i).widget(), main_window_qt._ClickableCard)
    ]


def test_recent_card_click_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")

    win = _make_window(tmp_path)
    cards = _recent_cards(win)
    assert len(cards) == 1
    cards[0]._on_click()

    assert len(fake_player.instances) == 1
    dialog = fake_player.instances[0]
    assert dialog.clip_path == clip_path
    assert dialog.delete_on_close is True
    assert dialog.shown is True
    # Kept referenced so the GC can't collect the modal-less dialog.
    assert win._players == [dialog]


def test_recent_card_click_falls_back_to_open_file_without_multimedia(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")
    opened = []
    monkeypatch.setattr(main_window_qt.player_qt, "multimedia_available", lambda: False)
    monkeypatch.setattr(main_window_qt.player_qt, "open_file", lambda path: opened.append(path))

    win = _make_window(tmp_path)
    _recent_cards(win)[0]._on_click()

    assert opened == [clip_path]
    assert win._players == []


def test_home_player_trim_export_refreshes_the_recent_strip(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")

    win = _make_window(tmp_path)
    _recent_cards(win)[0]._on_click()

    trimmed = clips_dir / "clip-trimmed.mp4"
    trimmed.write_bytes(b"x")
    fake_player.instances[0].trim_exported.emit(trimmed)

    assert trimmed in win._recent_thumb_labels


def test_recent_refresh_button_rereads_the_clips_dir(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    assert win._recent_thumb_labels == {}

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")
    win._recent_refresh_button.click()

    assert clip_path in win._recent_thumb_labels


def test_gallery_clips_changed_refreshes_the_recent_strip(tmp_path: Path) -> None:
    # Wired in MainWindow.__init__: a gallery-side edit (delete, rename, a
    # trim/compress export) must show up on Home without waiting for a save.
    win = _make_window(tmp_path)
    assert win._recent_thumb_labels == {}

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / "clip.mp4"
    clip_path.write_bytes(b"x")
    win._tabs["clips"].clips_changed.emit()

    assert clip_path in win._recent_thumb_labels


# ---- update banner -------------------------------------------------------------


def test_update_banner_hidden_by_default(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    assert win._update_banner.isHidden() is True


def test_show_update_banner_sets_text_and_shows_it(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win.show_update_banner("0.2.0", "https://example.invalid/releases/tag/v0.2.0")

    assert win._update_banner.isHidden() is False
    assert "0.2.0" in win._update_banner_label.text()
    assert win._update_version == "0.2.0"
    assert win._update_url == "https://example.invalid/releases/tag/v0.2.0"


def test_download_update_opens_url(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

    win = _make_window(tmp_path)
    win.show_update_banner("0.2.0", "https://example.invalid/releases/tag/v0.2.0")
    win._on_download_update()

    assert opened == ["https://example.invalid/releases/tag/v0.2.0"]


def test_dismiss_update_hides_banner_and_persists_dismissed_version(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "update_check_cache.json"
    real_load_cache = main_window_qt.update_check.load_cache
    real_save_cache = main_window_qt.update_check.save_cache
    monkeypatch.setattr(main_window_qt.update_check, "load_cache", lambda: real_load_cache(cache_path))
    monkeypatch.setattr(main_window_qt.update_check, "save_cache", lambda values: real_save_cache(values, cache_path))

    win = _make_window(tmp_path)
    win.show_update_banner("0.2.0", "https://example.invalid/releases/tag/v0.2.0")
    win._on_dismiss_update()

    assert win._update_banner.isHidden() is True
    assert real_load_cache(cache_path)["dismissed_version"] == "0.2.0"


def test_dismiss_update_without_a_pending_version_does_not_touch_cache(tmp_path: Path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(main_window_qt.update_check, "load_cache", lambda: called.append(True))

    win = _make_window(tmp_path)
    called.clear()  # building the window already read the cache once -- the Settings tab's "last checked" label

    win._on_dismiss_update()

    assert win._update_banner.isHidden() is True
    assert called == []


# ---- quick-save buttons ------------------------------------------------------
# The Home 15/30/60s buttons are gone; quick-saves stay available via the
# Ctrl+Shift+S shortcut (covered below), the tray menu, and the trigger CLI.


# ---- keyboard shortcuts ------------------------------------------------------


def _shortcut_by_key(win: MainWindow) -> dict:
    from PySide6.QtGui import QShortcut

    return {shortcut.key().toString(): shortcut for shortcut in win.findChildren(QShortcut)}


def test_all_shortcuts_exist_with_window_context(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    win = _make_window(tmp_path)
    shortcuts = _shortcut_by_key(win)
    assert set(shortcuts) == {"Ctrl+S", "Ctrl+Shift+S", "Ctrl+P", "F5", "Ctrl+,", "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4"}
    assert all(s.context() == Qt.ShortcutContext.WindowShortcut for s in shortcuts.values())


def test_tab_shortcuts_switch_tabs(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    shortcuts = _shortcut_by_key(win)
    shortcuts["Ctrl+2"].activated.emit()
    assert win._active_tab == "clips"
    shortcuts["Ctrl+3"].activated.emit()
    assert win._active_tab == "settings"
    # Ctrl+4 jumps straight to the logs -- now the Settings→Logs sub-tab.
    shortcuts["Ctrl+4"].activated.emit()
    assert win._active_tab == "settings"
    settings = win._tabs["settings"]
    assert settings.tabs.currentWidget() is settings.logs_tab
    shortcuts["Ctrl+1"].activated.emit()
    assert win._active_tab == "home"
    shortcuts["Ctrl+,"].activated.emit()
    assert win._active_tab == "settings"


def test_ctrl_s_shortcut_sends_save_via_the_worker_path(tmp_path: Path, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        main_window_qt.ipc_client,
        "send_command",
        lambda command, arg=None, **kwargs: sent.append((command, arg)) or "OK C:/clips/clip-1.mp4",
    )

    win = _make_window(tmp_path)
    _shortcut_by_key(win)["Ctrl+S"].activated.emit()
    assert _wait_for(lambda: sent == [("SAVE", None)])

    _shortcut_by_key(win)["Ctrl+Shift+S"].activated.emit()
    assert _wait_for(lambda: sent == [("SAVE", None), ("SAVE", "30")])


def test_ctrl_p_shortcut_toggles_pause(tmp_path: Path) -> None:
    received = []
    server = IpcServer(port=0)
    server.register("PAUSE", lambda arg: received.append("PAUSE") or "paused")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._capture_state = "RECORDING"
        _shortcut_by_key(win)["Ctrl+P"].activated.emit()
        assert _wait_for(lambda: received == ["PAUSE"])
    finally:
        server.stop()


def test_f5_shortcut_refreshes_the_gallery(tmp_path: Path, monkeypatch) -> None:
    win = _make_window(tmp_path)
    called = []
    monkeypatch.setattr(win._tabs["clips"], "refresh", lambda: called.append(True))
    _shortcut_by_key(win)["F5"].activated.emit()
    assert called == [True]


# The log-viewer tests (search/level filters, copy, auto-scroll, export
# diagnostics) moved to test_settings_window_qt.py along with the Logs page
# itself, which is now the Settings tab's last sub-tab.


# ---- sidebar footer: Lablooms identity + Support ------------------------------


def test_sidebar_lablooms_row_has_brand_mark_and_label(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QLabel

    win = _make_window(tmp_path)
    marks = win._lablooms_row.findChildren(main_window_qt.brand.BrandMark)
    assert len(marks) == 1
    assert marks[0].width() == 16
    labels = [label for label in win._lablooms_row.findChildren(QLabel) if label.text() == "Lablooms"]
    assert len(labels) == 1


def test_sidebar_lablooms_row_click_opens_the_lablooms_url(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

    win = _make_window(tmp_path)
    win._lablooms_row._on_click()

    assert opened == [main_window_qt.brand.LABLOOMS_URL]


def test_sidebar_support_button_opens_the_support_url(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

    win = _make_window(tmp_path)
    assert win._support_button.objectName() == "supportButton"
    assert "♥" in win._support_button.text()

    win._support_button.click()

    assert opened == [main_window_qt.brand.SUPPORT_URL]
