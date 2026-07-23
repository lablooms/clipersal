"""In-app clip player (0.1.4): a modal-less dialog that plays a saved clip
with play/pause, seek, volume, and speed controls, plus a small trim card
that exports the marked range through concat.trim_clip (stream copy, the
original clip is never touched).

QtMultimedia is an OPTIONAL import here: the PyInstaller spec excluded it
until now (the packaging re-inclusion is a separate change), and a Linux
install can lack the distro multimedia plugins. The guard keeps `import
clipersal.player_qt` always safe; PlayerDialog refuses to construct when the
import failed, and every caller (the gallery) must check
multimedia_available() first and fall back to tray.open_file() -- the OS
default player, which was the pre-0.1.4 behavior for every open action.

The trim export's _TrimWorker is a GUI-thread QObject whose blocking method
runs on a daemon thread and delivers its result through a queued Signal, so
the remux never freezes the dialog. The dialog is shown modal-less (show(),
WA_DeleteOnClose by the caller), so several players can be open at once.

`play_clip()` is the shared open-a-clip entry point (in-app player, or the
OS default player as the fallback): the gallery (double-click, Play button,
context menu) and the main window's recent-clips strip both go through it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from clipersal import concat
from clipersal.qt_widgets import SegmentedControl
from clipersal.tray import open_file

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    _MULTIMEDIA_OK = True
except ImportError:  # QtMultimedia excluded from the frozen build, or missing plugins
    _MULTIMEDIA_OK = False

log = logging.getLogger(__name__)

_SPEED_CHOICES = ["0.5×", "1×", "1.5×", "2×"]
_SPEED_RATES = {"0.5×": 0.5, "1×": 1.0, "1.5×": 1.5, "2×": 2.0}
_DEFAULT_SPEED = "1×"


def multimedia_available() -> bool:
    """True when PySide6.QtMultimedia imported cleanly. Callers must check
    this before constructing a PlayerDialog (its __init__ raises
    RuntimeError otherwise) and fall back to tray.open_file()."""
    return _MULTIMEDIA_OK


def play_clip(
    parent_widget: QWidget | None,
    clip_path: Path,
    ffmpeg_path: str | None = None,
    on_trim_exported=None,
) -> "PlayerDialog | None":
    """Open `clip_path` in the in-app player, or in the OS's default player
    when QtMultimedia is unavailable (the pre-0.1.4 behavior for every open
    action). Shared by the gallery and the main window's recent-clips strip
    so the availability check + fallback live in exactly one place; the
    `PlayerDialog`/`multimedia_available`/`open_file` lookups are module
    globals at call time so tests can monkeypatch them.

    The dialog is shown modal-less with WA_DeleteOnClose (several players
    may be open at once) -- the caller MUST keep the returned reference
    until `destroyed` fires. Returns None when the fallback path ran.
    `on_trim_exported`, when given, is connected to `trim_exported` (the
    gallery refreshes itself and re-emits clips_changed there).
    """
    if not multimedia_available():
        open_file(clip_path)
        return None
    dialog = PlayerDialog(clip_path, ffmpeg_path, parent_widget)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if on_trim_exported is not None:
        dialog.trim_exported.connect(on_trim_exported)
    dialog.show()
    return dialog


def _format_clock(seconds: float) -> str:
    """M:SS -- the seek bar's current/total readout and the trim result hint."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _format_clock_tenths(seconds: float) -> str:
    """M:SS.s -- the captured trim marks (the playhead moves in ms, so whole
    seconds would read as if nothing happened between two nearby captures)."""
    total_tenths = max(0, int(round(seconds * 10)))
    minutes, tenths = divmod(total_tenths, 600)
    return f"{minutes}:{tenths // 10:02d}.{tenths % 10}"


class _TrimWorker(QObject):
    """The trim export's background half -- the same shape as the gallery's
    _TrimWorker: constructed on the GUI thread, `trim()` runs on a daemon
    thread (concat.trim_clip spawns an ffmpeg subprocess), and the result
    comes back through the queued trim_finished signal. Exactly one of the
    signal's two payloads is set.
    """

    trim_finished = Signal(object, object)  # output path | None, error detail | None

    def __init__(self, ffmpeg_path: str, clip_path: Path, clips_dir: Path) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._clips_dir = clips_dir

    def trim(self, start_seconds: float, end_seconds: float, duration_seconds: float | None) -> None:
        try:
            output = concat.trim_clip(
                self._ffmpeg_path,
                self._clip_path,
                start_seconds,
                end_seconds,
                self._clips_dir,
                duration_seconds=duration_seconds,
            )
        except (concat.TrimRangeError, concat.ConcatFailedError) as exc:
            log.warning("Player trim of %s failed: %s", self._clip_path, exc)
            self.trim_finished.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001 -- every trim failure goes inline in the dialog, never a traceback
            log.exception("Player trim of %s failed unexpectedly", self._clip_path)
            self.trim_finished.emit(None, str(exc))
        else:
            self.trim_finished.emit(output, None)


class PlayerDialog(QDialog):
    """Modal-less clip player. The widgets a test (or the gallery) needs are
    public attributes, the same convention as the gallery's dialogs:
    play_pause_button, seek_slider, time_label, volume_slider, speed_control,
    set_start_button, set_end_button, export_button.

    `trim_exported` carries the Path of a successfully exported trimmed copy;
    the gallery connects it to refresh() so the new clip shows up.
    """

    trim_exported = Signal(object)  # Path of the exported "<stem>-trimmed.mp4"

    def __init__(self, clip_path: Path, ffmpeg_path: str | None = None, parent: QWidget | None = None) -> None:
        if not _MULTIMEDIA_OK:
            raise RuntimeError(
                "PySide6.QtMultimedia is not available -- check multimedia_available() "
                "before constructing a PlayerDialog and fall back to tray.open_file()."
            )
        super().__init__(parent)
        self._clip_path = clip_path
        self._ffmpeg_path = ffmpeg_path
        self._duration_ms = 0
        self._trim_start: float | None = None
        self._trim_end: float | None = None
        self._trimming = False

        self.setWindowTitle(clip_path.name)
        self.setMinimumSize(720, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --- media plumbing --------------------------------------------------
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._video = QVideoWidget(self)
        # Themed idle surface (see build_stylesheet's #videoSurface rule):
        # without it the video widget showed an unthemed black box before
        # playback started -- the exact "rogue dark background" report.
        self._video.setObjectName("videoSurface")
        self._player.setVideoOutput(self._video)
        layout.addWidget(self._video, 1)

        # --- controls bar -----------------------------------------------------
        controls = QHBoxLayout()
        controls.setSpacing(8)
        layout.addLayout(controls)

        self.play_pause_button = QPushButton("Play", self)
        self.play_pause_button.setFixedWidth(64)
        self.play_pause_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_pause_button)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        controls.addWidget(self.seek_slider, 1)

        self.time_label = QLabel("0:00 / 0:00", self)
        self.time_label.setObjectName("hint")
        controls.addWidget(self.time_label)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip("Volume")
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self.volume_slider)

        self.speed_control = SegmentedControl(_SPEED_CHOICES, self)
        self.speed_control.setCurrent(_DEFAULT_SPEED)
        self.speed_control.setFixedWidth(220)
        self.speed_control.currentTextChanged.connect(self._on_speed_changed)
        controls.addWidget(self.speed_control)

        # Playback problems (e.g. the file was deleted while this dialog is
        # open) surface here instead of taking the dialog down.
        self._playback_hint = QLabel("", self)
        self._playback_hint.setObjectName("statusLabel")
        self._playback_hint.setWordWrap(True)
        self._playback_hint.hide()
        layout.addWidget(self._playback_hint)

        # --- trim card --------------------------------------------------------
        trim_card = QFrame(self)
        trim_card.setObjectName("card")
        trim_layout = QVBoxLayout(trim_card)
        trim_layout.setContentsMargins(12, 12, 12, 12)
        trim_layout.setSpacing(8)
        layout.addWidget(trim_card)

        trim_title = QLabel("Trim", trim_card)
        title_font = trim_title.font()
        title_font.setBold(True)
        trim_title.setFont(title_font)
        trim_layout.addWidget(trim_title)

        marks_row = QHBoxLayout()
        marks_row.setSpacing(8)
        trim_layout.addLayout(marks_row)
        self.set_start_button = QPushButton("Set start", trim_card)
        self.set_start_button.clicked.connect(self._on_set_start)
        marks_row.addWidget(self.set_start_button)
        self.start_value_label = QLabel("--", trim_card)
        marks_row.addWidget(self.start_value_label)
        marks_row.addSpacing(12)
        self.set_end_button = QPushButton("Set end", trim_card)
        self.set_end_button.clicked.connect(self._on_set_end)
        marks_row.addWidget(self.set_end_button)
        self.end_value_label = QLabel("--", trim_card)
        marks_row.addWidget(self.end_value_label)
        marks_row.addStretch()
        self.result_label = QLabel("Result: --", trim_card)
        self.result_label.setObjectName("hint")
        marks_row.addWidget(self.result_label)

        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        trim_layout.addLayout(export_row)
        self.export_button = QPushButton("Export trim", trim_card)
        self.export_button.setObjectName("primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._start_trim_export)
        export_row.addWidget(self.export_button)
        self._trim_status_label = QLabel("", trim_card)
        self._trim_status_label.setObjectName("statusLabel")
        self._trim_status_label.setWordWrap(True)
        export_row.addWidget(self._trim_status_label, 1)

        # --- player wiring ----------------------------------------------------
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_error_occurred)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.setSource(QUrl.fromLocalFile(str(clip_path)))

        self._worker = _TrimWorker(ffmpeg_path or "", clip_path, clip_path.parent)
        self._worker.trim_finished.connect(self._on_trim_finished)

        self._refresh_trim_state()
        # "Play" is the whole point of opening the dialog -- start immediately.
        self._player.play()

    # ---- playback controls ---------------------------------------------------

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_playback_state_changed(self, state) -> None:
        self.play_pause_button.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    def _on_position_changed(self, position_ms: int) -> None:
        # While the user is dragging the seek handle, their hand wins over the
        # playhead; the actual seek happens on release.
        if not self.seek_slider.isSliderDown():
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(position_ms)
            self.seek_slider.blockSignals(False)
        self._update_time_label(position_ms)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms
        self.seek_slider.blockSignals(True)  # a range change can re-emit valueChanged
        self.seek_slider.setRange(0, duration_ms)
        self.seek_slider.blockSignals(False)
        self._update_time_label(self._player.position())

    def _update_time_label(self, position_ms: int) -> None:
        self.time_label.setText(f"{_format_clock(position_ms / 1000)} / {_format_clock(self._duration_ms / 1000)}")

    def _on_slider_released(self) -> None:
        self._player.setPosition(self.seek_slider.value())

    def _on_volume_changed(self, value: int) -> None:
        self._audio.setVolume(value / 100.0)

    def _on_speed_changed(self, text: str) -> None:
        self._player.setPlaybackRate(_SPEED_RATES.get(text, 1.0))

    def _set_playback_hint(self, text: str) -> None:
        # Same state-property dance as the gallery's _ExportDialog status
        # label: Qt doesn't re-evaluate the QSS [state=...] selector on its own.
        self._playback_hint.setText(text)
        self._playback_hint.setProperty("state", "error" if text else "")
        style = self._playback_hint.style()
        style.unpolish(self._playback_hint)
        style.polish(self._playback_hint)
        self._playback_hint.setVisible(bool(text))

    def _on_error_occurred(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self._set_playback_hint(f"Playback error: {message or 'the file may have been moved or deleted.'}")

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            # Covers the deleted-while-open case on backends that report it as
            # a status change rather than an error -- tolerated, not fatal.
            self._set_playback_hint("Cannot play this file -- it may be corrupt or was deleted.")

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        # Stop before the video widget dies with the dialog; Escape triggers
        # this too (QDialog's default Escape -> reject() -> close path).
        self._player.stop()
        super().closeEvent(event)

    # ---- trim card -------------------------------------------------------------

    def _on_set_start(self) -> None:
        self._trim_start = self._player.position() / 1000.0
        self.start_value_label.setText(_format_clock_tenths(self._trim_start))
        self._refresh_trim_state()

    def _on_set_end(self) -> None:
        self._trim_end = self._player.position() / 1000.0
        self.end_value_label.setText(_format_clock_tenths(self._trim_end))
        self._refresh_trim_state()

    def _refresh_trim_state(self) -> None:
        start, end = self._trim_start, self._trim_end
        problem = None
        if start is None or end is None:
            self.result_label.setText("Result: --")
        elif start >= end:
            self.result_label.setText("Result: --")
            problem = "Start must be before End."
        else:
            self.result_label.setText(f"Result: {_format_clock(end - start)}")
        if problem is None and self._ffmpeg_path is None and start is not None and end is not None:
            problem = "Trim export needs ffmpeg on PATH."
        # A hint about the range is different from a trim failure -- the
        # status label only carries the in-flight/exported/failed states, so
        # range problems go inline into the result readout area instead of
        # being conflated with ffmpeg errors.
        if problem is not None:
            self.result_label.setText(f"Result: --  ({problem})")
        self.export_button.setEnabled(
            not self._trimming
            and problem is None
            and start is not None
            and end is not None
        )

    def _start_trim_export(self) -> None:
        start, end = self._trim_start, self._trim_end
        if start is None or end is None or start >= end or self._trimming or self._ffmpeg_path is None:
            return  # the button is disabled in exactly these states -- belt-and-braces
        self._trimming = True
        self.export_button.setEnabled(False)
        self._set_trim_status("Exporting…", "")
        # durationChanged may never have fired (unparseable file); trim_clip
        # probes via ffprobe itself when handed None.
        duration_seconds = self._duration_ms / 1000.0 if self._duration_ms > 0 else None
        threading.Thread(target=self._worker.trim, args=(start, end, duration_seconds), daemon=True).start()

    def _set_trim_status(self, text: str, state: str) -> None:
        self._trim_status_label.setText(text)
        self._trim_status_label.setProperty("state", state)
        style = self._trim_status_label.style()
        style.unpolish(self._trim_status_label)
        style.polish(self._trim_status_label)

    def _on_trim_finished(self, output_path: Path | None, error: str | None) -> None:
        self._trimming = False
        self._refresh_trim_state()  # re-arms Export for the still-valid marks
        if error is not None:
            # Collapse ffmpeg's stderr tail to one line -- a multi-line wall
            # is unreadable in a status label (the gallery's inline-error
            # convention).
            summary = " ".join(error.split())
            if len(summary) > 300:
                summary = summary[:297] + "..."
            self._set_trim_status(summary, "error")
            return
        self._set_trim_status(f"Saved {output_path.name}", "success")
        self.trim_exported.emit(output_path)
