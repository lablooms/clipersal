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
from PySide6.QtGui import QCloseEvent, QKeyEvent, QPixmap
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
    assert dialog.start_field.value() == 5.0
    assert dialog.export_button.isEnabled() is False
    assert dialog.result_label.text() == "Result: --"

    monkeypatch.setattr(dialog._player, "position", lambda: 9000)
    dialog._on_set_end()
    assert dialog.end_field.value() == 9.0
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


def test_autoplay_false_opens_paused(clip_path: Path) -> None:
    from PySide6.QtMultimedia import QMediaPlayer

    dialog = PlayerDialog(clip_path, "ffmpeg", autoplay=False)

    assert dialog._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    assert dialog.play_pause_button.text() == "Play"


# ---- range slider ---------------------------------------------------------------


def test_range_slider_set_and_clear_range() -> None:
    slider = player_qt.RangeSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 10000)
    assert slider.range_start_ms is None
    assert slider.range_end_ms is None

    slider.set_range(1000, 5000)
    assert slider.range_start_ms == 1000
    assert slider.range_end_ms == 5000

    slider.clear_range()
    assert slider.range_start_ms is None
    assert slider.range_end_ms is None


def test_range_slider_paints_without_crashing_offscreen() -> None:
    # No pixel assertions -- just that the highlighted paint path (and the
    # cleared one) survive a real repaint under the offscreen platform.
    slider = player_qt.RangeSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 10000)
    slider.resize(240, 30)
    slider.show()

    slider.set_range(1000, 5000)
    assert not slider.grab().isNull()
    # Inverted marks (a range error) must not paint an inverted band either.
    slider.set_range(5000, 1000)
    assert not slider.grab().isNull()
    slider.clear_range()
    assert not slider.grab().isNull()


# ---- trim card: fields / slider / clear ------------------------------------------


def test_set_buttons_sync_fields_and_slider_range(dialog: PlayerDialog, monkeypatch) -> None:
    _set_marks(dialog, monkeypatch, 5.0, 9.0)

    assert dialog.start_field.value() == 5.0
    assert dialog.end_field.value() == 9.0
    assert dialog.seek_slider.range_start_ms == 5000
    assert dialog.seek_slider.range_end_ms == 9000


def test_editing_a_field_moves_the_mark_and_slider(dialog: PlayerDialog) -> None:
    dialog.start_field.setValue(2.5)
    assert dialog._trim_start == 2.5
    assert dialog.seek_slider.range_start_ms == 2500
    assert dialog.export_button.isEnabled() is False  # end still unset

    dialog.end_field.setValue(7.5)
    assert dialog._trim_end == 7.5
    assert dialog.seek_slider.range_end_ms == 7500
    assert dialog.export_button.isEnabled() is True
    assert dialog.result_label.text() == "Result: 0:05"


def test_field_edits_and_set_buttons_stay_in_sync(dialog: PlayerDialog, monkeypatch) -> None:
    # Set start captures the playhead into the field; editing the field then
    # moves the same mark -- one source of truth in both directions.
    monkeypatch.setattr(dialog._player, "position", lambda: 4000)
    dialog.set_start_button.click()
    assert dialog.start_field.value() == 4.0

    dialog.start_field.setValue(6.0)
    assert dialog._trim_start == 6.0
    monkeypatch.setattr(dialog._player, "position", lambda: 8000)
    dialog.set_end_button.click()
    assert dialog.end_field.value() == 8.0
    assert dialog._trim_end == 8.0
    assert dialog.export_button.isEnabled() is True


def test_export_disabled_when_fields_make_start_pass_end(dialog: PlayerDialog) -> None:
    dialog.start_field.setValue(9.0)
    dialog.end_field.setValue(3.0)

    assert dialog.export_button.isEnabled() is False
    assert "before" in dialog.result_label.text()


def test_clear_button_resets_marks_fields_and_slider(dialog: PlayerDialog, monkeypatch) -> None:
    _set_marks(dialog, monkeypatch, 1.0, 4.0)
    assert dialog.export_button.isEnabled() is True

    dialog.clear_button.click()

    assert dialog._trim_start is None
    assert dialog._trim_end is None
    assert dialog.start_field.value() == 0.0
    assert dialog.end_field.value() == 0.0
    assert dialog.seek_slider.range_start_ms is None
    assert dialog.seek_slider.range_end_ms is None
    assert dialog.export_button.isEnabled() is False
    assert dialog.result_label.text() == "Result: --"


def test_duration_tightens_the_field_ranges(dialog: PlayerDialog) -> None:
    dialog._on_duration_changed(65000)

    assert dialog.start_field.maximum() == 65.0
    assert dialog.end_field.maximum() == 65.0


# ---- trim card: frame previews ----------------------------------------------------


def _write_frame(path: Path) -> Path:
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.red)
    assert pixmap.save(str(path), "JPG")
    return path


def test_previews_show_the_grabbed_frames(dialog: PlayerDialog, monkeypatch) -> None:
    grabs = []

    def fake_grab(ffmpeg_path, clip, offset, target, size=320):
        grabs.append(offset)
        return _write_frame(target)

    monkeypatch.setattr(player_qt.thumbnails, "grab_frame_at", fake_grab)
    _set_marks(dialog, monkeypatch, 1.0, 4.0)
    # Fire directly -- the 300 ms debounce timer needs a running event loop,
    # which these tests deliberately never start.
    dialog._refresh_previews()

    _process_events(lambda: not dialog.start_preview.isHidden() and not dialog.end_preview.isHidden())
    assert sorted(grabs) == [1.0, 4.0]
    assert not dialog.start_preview.pixmap().isNull()
    assert not dialog.end_preview.pixmap().isNull()


def test_previews_stay_hidden_when_the_grab_fails(dialog: PlayerDialog, monkeypatch) -> None:
    monkeypatch.setattr(player_qt.thumbnails, "grab_frame_at", lambda *args, **kwargs: None)
    _set_marks(dialog, monkeypatch, 1.0, 4.0)
    delivered = []
    dialog._ensure_preview_worker().preview_ready.connect(lambda *args: delivered.append(args))

    dialog._refresh_previews()

    _process_events(lambda: len(delivered) >= 2)
    assert dialog.start_preview.isHidden()
    assert dialog.end_preview.isHidden()


def test_stale_preview_results_are_dropped(dialog: PlayerDialog, clip_path: Path) -> None:
    dialog._preview_generation = 5

    dialog._on_preview_ready(4, "start", clip_path)  # an older generation's grab

    assert dialog.start_preview.isHidden()
    assert dialog.start_preview.pixmap().isNull()


def test_clearing_a_mark_hides_its_preview(dialog: PlayerDialog, monkeypatch) -> None:
    monkeypatch.setattr(
        player_qt.thumbnails, "grab_frame_at", lambda ffmpeg, clip, offset, target, size=320: _write_frame(target)
    )
    _set_marks(dialog, monkeypatch, 1.0, 4.0)
    dialog._refresh_previews()
    _process_events(lambda: not dialog.start_preview.isHidden() and not dialog.end_preview.isHidden())

    dialog.clear_button.click()
    dialog._refresh_previews()  # both marks are None now

    assert dialog.start_preview.isHidden()
    assert dialog.end_preview.isHidden()


# ---- focus_trim --------------------------------------------------------------------


def test_focus_trim_retitles_and_accent_marks_the_card(clip_path: Path) -> None:
    dialog = PlayerDialog(clip_path, "ffmpeg", autoplay=False, focus_trim=True)

    assert dialog.windowTitle() == f"Trim — {clip_path.name}"
    assert dialog.trim_card.property("trimFocus") is True


def test_plain_open_keeps_the_clip_title_and_unmarked_card(dialog: PlayerDialog, clip_path: Path) -> None:
    assert dialog.windowTitle() == clip_path.name
    assert not dialog.trim_card.property("trimFocus")


# ---- play_clip hardening -------------------------------------------------------------


def test_play_clip_falls_back_to_open_file_when_the_ctor_raises(clip_path: Path, monkeypatch) -> None:
    def exploding_ctor(*args, **kwargs):
        raise RuntimeError("backend exploded")

    opened = []
    monkeypatch.setattr(player_qt, "PlayerDialog", exploding_ctor)
    monkeypatch.setattr(player_qt, "open_file", lambda path: opened.append(path))

    assert player_qt.play_clip(None, clip_path, "ffmpeg") is None
    assert opened == [clip_path]


def test_play_clip_passes_autoplay_and_focus_trim_through(clip_path: Path, monkeypatch) -> None:
    captured = {}

    class _FakeDialog:
        def __init__(self, clip, ffmpeg_path, parent, autoplay=True, focus_trim=False):
            captured["autoplay"] = autoplay
            captured["focus_trim"] = focus_trim

        def setAttribute(self, attribute) -> None:
            pass

        def show(self) -> None:
            pass

    monkeypatch.setattr(player_qt, "PlayerDialog", _FakeDialog)

    dialog = player_qt.play_clip(None, clip_path, "ffmpeg", autoplay=False, focus_trim=True)

    assert isinstance(dialog, _FakeDialog)
    assert captured == {"autoplay": False, "focus_trim": True}
