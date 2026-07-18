import os
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


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(tmp_path: Path, tray_enabled: bool = True, on_quit=None, ipc_port: int = 1) -> MainWindow:
    config = Config(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    return MainWindow(
        config, ipc_port, None, "libx264", lambda values: None, "ffmpeg", tmp_path / "clips",
        tmp_path / "log.txt", tray_enabled, on_quit or (lambda: None),
    )


def test_all_four_tabs_exist(tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    assert set(win._tabs.keys()) == {"home", "clips", "settings", "logs"}
    assert win._active_tab == "home"


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


# ---- status polling ---------------------------------------------------------


def test_poll_status_recording(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "RECORDING")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._status_label.text() == "Recording"
        assert win._pause_button.text() == "Pause"
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
        assert win._pause_button.text() == "Resume"
    finally:
        server.stop()


def test_poll_status_crashed(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    server.register("STATUS", lambda arg: "CRASHED")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._poll_status()
        assert win._status_label.text() == "Capture stopped -- see Logs"
        assert win._pause_button.text() == "Resume"
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


def test_toggle_pause_sends_pause_when_recording(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("STATUS", lambda arg: "RECORDING")
    server.register("PAUSE", lambda arg: received.append("PAUSE") or "paused")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._on_toggle_pause()
        assert received == ["PAUSE"]
    finally:
        server.stop()


def test_toggle_pause_sends_resume_when_paused(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("STATUS", lambda arg: "PAUSED")
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._on_toggle_pause()
        assert received == ["RESUME"]
    finally:
        server.stop()


def test_toggle_pause_sends_resume_when_crashed(tmp_path: Path) -> None:
    server = IpcServer(port=0)
    received = []
    server.register("STATUS", lambda arg: "CRASHED")
    server.register("RESUME", lambda arg: received.append("RESUME") or "resumed")
    server.start()
    try:
        win = _make_window(tmp_path, ipc_port=server.port)
        win._on_toggle_pause()
        assert received == ["RESUME"]
    finally:
        server.stop()


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
    assert win._recent_strip.count() == 3  # sprig accent + "no clips yet" label + stretch


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
    win._on_dismiss_update()

    assert win._update_banner.isHidden() is True
    assert called == []
