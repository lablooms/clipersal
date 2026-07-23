"""PlayerDialog tests. The dialog IS constructed with a real QMediaPlayer
under the offscreen platform -- setSource on a fake file does not hang or
crash there (verified: the backend just stays in StoppedState) -- but no
test asserts actual playback: the slots are driven directly and the player's
methods are monkeypatched where a call must be observed.
"""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtMultimedia")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QDialog

from clipersal import player_qt
from clipersal.player_qt import PlayerDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def clip_path(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake mp4 data")
    return path


@pytest.fixture()
def dialog(clip_path: Path) -> PlayerDialog:
    return PlayerDialog(clip_path, "ffmpeg")


def _process_events(condition, timeout=2.0) -> None:
    # sendPostedEvents, not processEvents: queued cross-thread signal
    # deliveries are what the trim worker needs pumped (the gallery tests
    # established the pattern).
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.sendPostedEvents()


def test_multimedia_available_reflects_the_import_guard() -> None:
    assert player_qt.multimedia_available() is player_qt._MULTIMEDIA_OK


def test_ctor_raises_runtimeerror_when_multimedia_missing(clip_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(player_qt, "_MULTIMEDIA_OK", False)
    with pytest.raises(RuntimeError, match="QtMultimedia"):
        PlayerDialog(clip_path, "ffmpeg")


def test_play_pause_button_text_follows_playback_state(dialog: PlayerDialog) -> None:
    assert dialog.play_pause_button.text() == "Play"
    dialog._on_playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
    assert dialog.play_pause_button.text() == "Pause"
    dialog._on_playback_state_changed(QMediaPlayer.PlaybackState.PausedState)
    assert dialog.play_pause_button.text() == "Play"


def test_toggle_play_calls_play_when_stopped(dialog: PlayerDialog, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(dialog._player, "play", lambda: calls.append("play"))
    monkeypatch.setattr(dialog._player, "pause", lambda: calls.append("pause"))
    # A fake source never enters PlayingState, so the toggle picks play().
    dialog._toggle_play()
    assert calls == ["play"]


def test_duration_and_position_drive_slider_and_time_label(dialog: PlayerDialog) -> None:
    dialog._on_duration_changed(65000)
    assert dialog.seek_slider.maximum() == 65000
    assert dialog.time_label.text() == "0:00 / 1:05"

    dialog._on_position_changed(5000)
    assert dialog.seek_slider.value() == 5000
    assert dialog.time_label.text() == "0:05 / 1:05"


def test_slider_release_seeks_to_the_handle_position(dialog: PlayerDialog, monkeypatch) -> None:
    seeks = []
    monkeypatch.setattr(dialog._player, "setPosition", lambda ms: seeks.append(ms))
    dialog._on_duration_changed(65000)  # widen the range so setValue isn't clamped
    dialog.seek_slider.setValue(12000)

    dialog._on_slider_released()

    assert seeks == [12000]


def test_speed_control_sets_playback_rate(dialog: PlayerDialog) -> None:
    dialog.speed_control._buttons["2×"].click()
    assert dialog._player.playbackRate() == 2.0
    dialog.speed_control._buttons["0.5×"].click()
    assert dialog._player.playbackRate() == 0.5


def test_volume_slider_sets_audio_volume(dialog: PlayerDialog) -> None:
    dialog.volume_slider.setValue(40)
    assert dialog._audio.volume() == pytest.approx(0.4, abs=1e-5)


def test_playback_error_shows_an_inline_hint(dialog: PlayerDialog) -> None:
    dialog._on_error_occurred(QMediaPlayer.Error.ResourceError, "file gone")
    assert "file gone" in dialog._playback_hint.text()


def test_invalid_media_status_shows_an_inline_hint(dialog: PlayerDialog) -> None:
    # The deleted-while-open case on backends that report it as a status
    # change rather than an error.
    dialog._on_media_status_changed(QMediaPlayer.MediaStatus.InvalidMedia)
    assert "deleted" in dialog._playback_hint.text()


def test_escape_closes_the_dialog(dialog: PlayerDialog) -> None:
    dialog.show()
    assert dialog.isHidden() is False
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    dialog.keyPressEvent(event)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.isHidden() is True


def test_close_event_stops_playback(dialog: PlayerDialog, monkeypatch) -> None:
    stops = []
    monkeypatch.setattr(dialog._player, "stop", lambda: stops.append(True))
    dialog.closeEvent(QCloseEvent())
    assert stops == [True]


# ---- trim card -----------------------------------------------------------------


def _set_marks(dialog: PlayerDialog, monkeypatch, start_s: float, end_s: float) -> None:
    monkeypatch.setattr(dialog._player, "position", lambda: int(start_s * 1000))
    dialog._on_set_start()
    monkeypatch.setattr(dialog._player, "position", lambda: int(end_s * 1000))
    dialog._on_set_end()


def test_export_disabled_until_both_marks_set(dialog: PlayerDialog, monkeypatch) -> None:
    assert dialog.export_button.isEnabled() is False

    monkeypatch.setattr(dialog._player, "position", lambda: 5000)
    dialog._on_set_start()
    assert dialog.start_value_label.text() == "0:05.0"
    assert dialog.export_button.isEnabled() is False
    assert dialog.result_label.text() == "Result: --"

    monkeypatch.setattr(dialog._player, "position", lambda: 9000)
    dialog._on_set_end()
    assert dialog.end_value_label.text() == "0:09.0"
    assert dialog.export_button.isEnabled() is True
    assert dialog.result_label.text() == "Result: 0:04"


def test_export_disabled_when_end_not_after_start(dialog: PlayerDialog, monkeypatch) -> None:
    _set_marks(dialog, monkeypatch, 5.0, 3.0)
    assert dialog.export_button.isEnabled() is False
    assert "before" in dialog.result_label.text()


def test_export_disabled_without_ffmpeg(clip_path: Path, monkeypatch) -> None:
    dialog = PlayerDialog(clip_path, None)
    _set_marks(dialog, monkeypatch, 1.0, 4.0)
    assert dialog.export_button.isEnabled() is False
    assert "ffmpeg" in dialog.result_label.text()


def test_trim_export_success_shows_success_and_emits_path(
    dialog: PlayerDialog, clip_path: Path, tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "clip-trimmed.mp4"
    output.write_bytes(b"trimmed data")
    trim_calls = []

    def fake_trim(ffmpeg_path, clip, start, end, clips_dir, duration_seconds=None):
        trim_calls.append((ffmpeg_path, clip, start, end, clips_dir, duration_seconds))
        return output

    monkeypatch.setattr(player_qt.concat, "trim_clip", fake_trim)
    emitted = []
    dialog.trim_exported.connect(lambda path: emitted.append(path))
    _set_marks(dialog, monkeypatch, 1.0, 4.0)

    dialog.export_button.click()

    _process_events(lambda: bool(emitted))
    # No real media -> durationChanged never fired -> None lets trim_clip probe.
    assert trim_calls == [("ffmpeg", clip_path, 1.0, 4.0, tmp_path, None)]
    assert emitted == [output]
    assert "clip-trimmed.mp4" in dialog._trim_status_label.text()
    assert dialog.export_button.isEnabled() is True  # re-armed for the still-valid marks


def test_trim_export_failure_shows_the_error_inline(dialog: PlayerDialog, monkeypatch) -> None:
    def failing_trim(*args, **kwargs):
        raise player_qt.concat.ConcatFailedError("ffmpeg trim failed:\nsome ffmpeg stderr")

    monkeypatch.setattr(player_qt.concat, "trim_clip", failing_trim)
    emitted = []
    dialog.trim_exported.connect(lambda path: emitted.append(path))
    _set_marks(dialog, monkeypatch, 1.0, 4.0)

    dialog.export_button.click()

    # The label says "Exporting…" synchronously -- wait for the FAILURE text.
    _process_events(lambda: "ffmpeg trim failed" in dialog._trim_status_label.text())
    assert "ffmpeg trim failed" in dialog._trim_status_label.text()
    assert emitted == []
    assert dialog.export_button.isEnabled() is True  # usable again after a failure
