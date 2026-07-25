"""In-app clip player (0.1.4): a modal-less dialog that plays a saved clip
with play/pause, seek, volume, and speed controls, plus a trim card that
exports the marked range through concat.trim_clip (stream copy, the
original clip is never touched).

The trim card is a small editor, not just two buttons: Set start/Set end
capture the playhead, the StepperDoubleSpinBox fields next to them are
directly editable (both directions stay in sync), the seek slider (a
RangeSlider) paints the marked range highlighted on the groove, a Clear
button resets both marks, and frame previews of the two marks are grabbed
by _PreviewWorker off the GUI thread (300 ms debounce, hidden quietly
when ffmpeg is missing or a grab fails). `focus_trim=True` (the gallery's
Trim... action) retitles the window and gives the card a one-time accent
border so a trim-focused open reads as an editor, not a plain player.

QtMultimedia is an OPTIONAL import here: the PyInstaller spec excluded it
until now (the packaging re-inclusion is a separate change), and a Linux
install can lack the distro multimedia plugins. The guard keeps `import
clipersal.player_qt` always safe; PlayerDialog refuses to construct when the
import failed, and every caller (the gallery) must check
multimedia_available() first and fall back to tray.open_file() -- the OS
default player, which was the pre-0.1.4 behavior for every open action.
play_clip() additionally guards the construction itself: a present-but-
broken backend can raise from the PlayerDialog constructor, and that must
fall back to the OS player too, never silently eat the user's click.

The trim export's _TrimWorker and the previews' _PreviewWorker are
GUI-thread QObjects whose blocking methods run on daemon threads and
deliver results through queued Signals, so neither the remux nor a frame
grab ever freezes the dialog. The dialog is shown modal-less (show(),
WA_DeleteOnClose by the caller), so several players can be open at once.

`play_clip()` is the shared open-a-clip entry point (in-app player, or the
OS default player as the fallback): the gallery (double-click, Play button,
context menu) and the main window's recent-clips strip both go through it.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from clipersal import concat, theme, thumbnails
from clipersal.qt_widgets import SegmentedControl, StepperDoubleSpinBox
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
    autoplay: bool = True,
    focus_trim: bool = False,
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
    `focus_trim=True` (the gallery's Trim... action) opens the dialog as a
    trim editor: paused, retitled, trim card accent-framed.
    """
    if not multimedia_available():
        # This branch used to be silent: a packaged build that can't import
        # QtMultimedia (e.g. the QtNetwork exclusion that broke shiboken's
        # QtMultimedia import in 0.1.0-lab.1/lab.2) made every Play/Trim click
        # open the OS player with no trace in the log.
        log.warning(
            "QtMultimedia is unavailable -- opening %s with the OS default player instead",
            clip_path,
        )
        open_file(clip_path)
        return None
    try:
        dialog = PlayerDialog(clip_path, ffmpeg_path, parent_widget, autoplay=autoplay, focus_trim=focus_trim)
    except Exception:  # noqa: BLE001 -- a present-but-broken backend can raise here
        # The import guard only proves QtMultimedia IMPORTED; the backend can
        # still blow up constructing the player. Falling back to the OS
        # player keeps the click from vanishing into nothing (the "player
        # disappeared" report).
        log.warning("In-app player failed to open %s -- falling back to the OS player", clip_path, exc_info=True)
        open_file(clip_path)
        return None
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


class RangeSlider(QSlider):
    """Seek slider that paints the trim range highlighted on its groove: an
    ACCENT band between the two marks plus a 3px marker line at each mark.
    `range_start_ms`/`range_end_ms` are None while a mark is unset; the band
    is only painted once both exist. Horizontal-only (the seek bar's
    orientation) -- the value->pixel mapping assumes the groove runs left to
    right. Painting reads theme.ACCENT at call time, so a live theme switch
    recolors the band on the next repaint like every other widget.
    """

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.range_start_ms: int | None = None
        self.range_end_ms: int | None = None

    def set_range(self, start_ms: int | None, end_ms: int | None) -> None:
        self.range_start_ms = start_ms
        self.range_end_ms = end_ms
        self.update()  # repaint even when the playhead itself didn't move

    def clear_range(self) -> None:
        self.set_range(None, None)

    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        super().paintEvent(event)
        start, end = self.range_start_ms, self.range_end_ms
        if start is None or end is None or self.maximum() <= self.minimum():
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self
        )
        if groove.isNull() or groove.width() <= 0:
            return
        # sliderPositionFromValue clamps out-of-range marks into the groove's
        # span, so a mark past a shrunk slider range paints at the edge.
        x_start = groove.x() + QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), start, groove.width())
        x_end = groove.x() + QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), end, groove.width())
        if x_end < x_start:  # start > end is a range error, but the band shouldn't paint inverted
            x_start, x_end = x_end, x_start
        accent = QColor(theme.ACCENT)
        band = QColor(accent)
        band.setAlphaF(0.4)
        painter = QPainter(self)
        painter.fillRect(x_start, groove.y(), x_end - x_start + 1, groove.height(), band)
        painter.fillRect(x_start - 1, groove.y(), 3, groove.height(), accent)
        painter.fillRect(x_end - 1, groove.y(), 3, groove.height(), accent)
        painter.end()


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


_PREVIEW_WIDTH = 120
_PREVIEW_HEIGHT = 68
# Grabbed wider than the label so the downscale to 120px stays sharp on
# high-DPI displays; the label re-scales to its own size keeping aspect.
_PREVIEW_GRAB_WIDTH = 240
_PREVIEW_DEBOUNCE_MS = 300
# The trim fields' maximum before durationChanged reports the real duration
# (an unparseable file may never fire it): a generous 24 h ceiling so the
# fields stay editable, tightened to the clip's length once it's known.
_UNKNOWN_DURATION_FIELD_MAX = 86400.0


class _PreviewWorker(QObject):
    """The frame previews' background half -- the same shape as _TrimWorker:
    constructed on the GUI thread, `grab()` runs on a daemon thread
    (thumbnails.grab_frame_at spawns an ffmpeg subprocess), and the result
    comes back through the queued preview_ready signal. The generation
    counter round-trips with every emission so the dialog can drop grabs
    that a newer mark change already superseded (the "cancel/replace" of a
    moving playhead -- the clip path itself is fixed for the dialog's
    lifetime). Exactly one grab runs per mark per generation.
    """

    preview_ready = Signal(int, str, object)  # generation, "start" | "end", target Path | None

    def __init__(self, ffmpeg_path: str, clip_path: Path, work_dir: Path) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._work_dir = work_dir

    def grab(self, generation: int, which: str, offset_seconds: float) -> None:
        target = self._work_dir / f"trim-preview-{which}.jpg"
        path = thumbnails.grab_frame_at(
            self._ffmpeg_path, self._clip_path, offset_seconds, target, size=_PREVIEW_GRAB_WIDTH
        )
        self.preview_ready.emit(generation, which, path)


class PlayerDialog(QDialog):
    """Modal-less clip player. The widgets a test (or the gallery) needs are
    public attributes, the same convention as the gallery's dialogs:
    play_pause_button, seek_slider (a RangeSlider), time_label,
    volume_slider, speed_control, set_start_button, start_field,
    set_end_button, end_field, clear_button, start_preview, end_preview,
    result_label, export_button, trim_card.

    `trim_exported` carries the Path of a successfully exported trimmed copy;
    the gallery connects it to refresh() so the new clip shows up.
    """

    trim_exported = Signal(object)  # Path of the exported "<stem>-trimmed.mp4"

    def __init__(
        self,
        clip_path: Path,
        ffmpeg_path: str | None = None,
        parent: QWidget | None = None,
        autoplay: bool = True,
        focus_trim: bool = False,
    ) -> None:
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
        self._preview_worker: _PreviewWorker | None = None
        self._preview_dir: Path | None = None
        self._preview_generation = 0

        # A trim-focused open (the gallery's Trim... action) retitles the
        # window so it reads as an editor before anything else registers.
        self.setWindowTitle(f"Trim — {clip_path.name}" if focus_trim else clip_path.name)
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

        self.seek_slider = RangeSlider(Qt.Orientation.Horizontal, self)
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
        self.trim_card = QFrame(self)
        self.trim_card.setObjectName("card")
        if focus_trim:
            # One-time accent border (the QFrame#card[trimFocus="true"] QSS
            # rule) so a Trim... open reads as an editor, not a plain player.
            # Set before show(), so the global stylesheet picks it up on the
            # first polish -- no unpolish/polish dance needed.
            self.trim_card.setProperty("trimFocus", True)
        trim_layout = QVBoxLayout(self.trim_card)
        trim_layout.setContentsMargins(12, 12, 12, 12)
        trim_layout.setSpacing(8)
        layout.addWidget(self.trim_card)

        trim_title = QLabel("TRIM", self.trim_card)
        # A card section title -- the #cardTitle style (literal caps; Qt QSS
        # has no text-transform), same convention as the settings cards.
        trim_title.setObjectName("cardTitle")
        trim_layout.addWidget(trim_title)

        marks_row = QHBoxLayout()
        marks_row.setSpacing(8)
        trim_layout.addLayout(marks_row)
        self.set_start_button = QPushButton("Set start", self.trim_card)
        self.set_start_button.clicked.connect(self._on_set_start)
        marks_row.addWidget(self.set_start_button)
        # The fields are directly editable AND follow the Set buttons -- both
        # directions converge on _apply_mark_edit. The initial maximum is
        # generous because durationChanged may not have fired yet (the
        # playhead a Set button captures always fits); it tightens to the
        # real duration when that signal arrives. 0.1s precision matches
        # what the old M:SS.s mark readout showed.
        self.start_field = StepperDoubleSpinBox(self.trim_card)
        self.start_field.setDecimals(1)
        self.start_field.setSingleStep(0.1)
        self.start_field.setSuffix(" s")
        self.start_field.setRange(0.0, _UNKNOWN_DURATION_FIELD_MAX)
        self.start_field.setFixedWidth(90)
        self.start_field.valueChanged.connect(self._on_start_field_edited)
        marks_row.addWidget(self.start_field)
        marks_row.addSpacing(12)
        self.set_end_button = QPushButton("Set end", self.trim_card)
        self.set_end_button.clicked.connect(self._on_set_end)
        marks_row.addWidget(self.set_end_button)
        self.end_field = StepperDoubleSpinBox(self.trim_card)
        self.end_field.setDecimals(1)
        self.end_field.setSingleStep(0.1)
        self.end_field.setSuffix(" s")
        self.end_field.setRange(0.0, _UNKNOWN_DURATION_FIELD_MAX)
        self.end_field.setFixedWidth(90)
        self.end_field.valueChanged.connect(self._on_end_field_edited)
        marks_row.addWidget(self.end_field)
        marks_row.addSpacing(12)
        self.clear_button = QPushButton("Clear", self.trim_card)
        self.clear_button.setToolTip("Reset both trim marks")
        self.clear_button.clicked.connect(self._on_clear_marks)
        marks_row.addWidget(self.clear_button)
        marks_row.addStretch()
        self.result_label = QLabel("Result: --", self.trim_card)
        self.result_label.setObjectName("hint")
        marks_row.addWidget(self.result_label)

        previews_row = QHBoxLayout()
        previews_row.setSpacing(8)
        trim_layout.addLayout(previews_row)
        # Frame previews of the two marks, grabbed off the GUI thread by
        # _PreviewWorker. Hidden until a grab lands (and hidden again when a
        # mark clears or a grab fails) -- no ffmpeg means they simply never
        # appear, which is the quiet-degradation house rule.
        self.start_preview = QLabel(self.trim_card)
        self.start_preview.setObjectName("thumbPlaceholder")
        self.start_preview.setFixedSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
        self.start_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.start_preview.hide()
        previews_row.addWidget(self.start_preview)
        self.end_preview = QLabel(self.trim_card)
        self.end_preview.setObjectName("thumbPlaceholder")
        self.end_preview.setFixedSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
        self.end_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.end_preview.hide()
        previews_row.addWidget(self.end_preview)
        previews_row.addStretch()

        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        trim_layout.addLayout(export_row)
        self.export_button = QPushButton("Export trim", self.trim_card)
        self.export_button.setObjectName("primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._start_trim_export)
        export_row.addWidget(self.export_button)
        self._trim_status_label = QLabel("", self.trim_card)
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

        # Preview grabs are debounced: dragging the playhead across Set
        # start/end must not spawn an ffmpeg subprocess per captured mark.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._refresh_previews)

        self._refresh_trim_state()
        # Watching is the point of opening the dialog for play -- start
        # immediately. A trim-focused open stays paused instead: autoplay
        # made "Trim…" feel like it just played the video, with the marks
        # impossible to land on a moving playhead.
        if autoplay:
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
        if duration_ms > 0:
            # Tighten the trim fields to the real duration. A clamped field
            # emits valueChanged, which routes through the same edit handler
            # as typing -- the mark follows the field, keeping them in sync.
            duration_seconds = duration_ms / 1000.0
            self.start_field.setMaximum(duration_seconds)
            self.end_field.setMaximum(duration_seconds)
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
        # In-flight preview grabs are dropped via the generation bump; their
        # work dir goes away best-effort (a still-writing ffmpeg keeps its
        # file on Windows -- ignore_errors leaves it for the OS temp sweep).
        self._preview_timer.stop()
        self._preview_generation += 1
        if self._preview_dir is not None:
            shutil.rmtree(self._preview_dir, ignore_errors=True)
            self._preview_dir = None
        super().closeEvent(event)

    # ---- trim card -------------------------------------------------------------

    def _on_set_start(self) -> None:
        self._set_trim_start(self._player.position() / 1000.0)

    def _on_set_end(self) -> None:
        self._set_trim_end(self._player.position() / 1000.0)

    def _on_clear_marks(self) -> None:
        self._trim_start = None
        self._trim_end = None
        for field in (self.start_field, self.end_field):
            field.blockSignals(True)  # resetting the display isn't an edit
            field.setValue(0.0)
            field.blockSignals(False)
        self.seek_slider.clear_range()
        self._refresh_trim_state()
        self._schedule_preview_refresh()

    def _on_start_field_edited(self, value: float) -> None:
        self._apply_mark_edit("start", value)

    def _on_end_field_edited(self, value: float) -> None:
        self._apply_mark_edit("end", value)

    def _apply_mark_edit(self, which: str, value: float) -> None:
        # The field already holds `value` (it emitted the change), so only
        # the mark, the slider band, and the export gating need to follow.
        if which == "start":
            self._trim_start = value
        else:
            self._trim_end = value
        self._sync_range_slider()
        self._refresh_trim_state()
        self._schedule_preview_refresh()

    def _set_trim_start(self, seconds: float) -> None:
        self._set_trim_mark("start", seconds)

    def _set_trim_end(self, seconds: float) -> None:
        self._set_trim_mark("end", seconds)

    def _set_trim_mark(self, which: str, seconds: float) -> None:
        field = self.start_field if which == "start" else self.end_field
        field.blockSignals(True)  # the field syncs to the mark, not vice versa
        field.setValue(seconds)
        field.blockSignals(False)
        # The mark takes the field's (precision-rounded, clamped) value so
        # what the user reads is exactly what gets exported.
        self._apply_mark_edit(which, field.value())

    def _sync_range_slider(self) -> None:
        start_ms = int(round(self._trim_start * 1000)) if self._trim_start is not None else None
        end_ms = int(round(self._trim_end * 1000)) if self._trim_end is not None else None
        self.seek_slider.set_range(start_ms, end_ms)

    def _schedule_preview_refresh(self) -> None:
        if self._ffmpeg_path is None:
            return  # previews silently stay hidden without ffmpeg
        self._preview_timer.start()  # single-shot: restarts the 300 ms settle window

    def _ensure_preview_worker(self) -> _PreviewWorker | None:
        if self._ffmpeg_path is None:
            return None
        if self._preview_worker is None:
            # Lazily created so a plain Play open never touches the temp dir.
            self._preview_dir = Path(tempfile.mkdtemp(prefix="clipersal-trim-preview-"))
            self._preview_worker = _PreviewWorker(self._ffmpeg_path, self._clip_path, self._preview_dir)
            self._preview_worker.preview_ready.connect(self._on_preview_ready)
        return self._preview_worker

    def _refresh_previews(self) -> None:
        """Grab the frame at each set mark on daemon threads. Marks that are
        unset hide their preview immediately; the generation bump strands any
        grab an older mark change still has in flight (its result arrives
        with a stale generation and is dropped in _on_preview_ready)."""
        worker = self._ensure_preview_worker()
        if worker is None:
            return
        self._preview_generation += 1
        generation = self._preview_generation
        for which, mark in (("start", self._trim_start), ("end", self._trim_end)):
            if mark is None:
                self._preview_label(which).hide()
                continue
            threading.Thread(target=worker.grab, args=(generation, which, mark), daemon=True).start()

    def _preview_label(self, which: str) -> QLabel:
        return self.start_preview if which == "start" else self.end_preview

    def _on_preview_ready(self, generation: int, which: str, path: Path | None) -> None:
        if generation != self._preview_generation:
            return  # superseded by a newer mark change while ffmpeg ran
        label = self._preview_label(which)
        pixmap = QPixmap(str(path)) if path is not None else QPixmap()
        if pixmap.isNull():  # grab failed or produced garbage -- hide quietly
            label.hide()
            return
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        label.show()

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
