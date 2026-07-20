"""Clips tab. Lists saved clips in clips_dir with a thumbnail, saved-at date,
and size; supports open / reveal-in-folder / rename / trim / delete / refresh.

`GalleryFrame` is built once (by main_window_qt.py) and never destroyed --
switching to this tab just raises the already-built
widget, so its thumbnail worker only ever runs when refresh() is explicitly
called (on build, or after a save), not continuously.

Thumbnails are generated in a background thread (an ffmpeg frame-grab per
clip takes a few hundred ms -- doing it synchronously would make opening the
gallery feel slow before thumbnails.py's cache is warm). Delivery back to
the GUI thread is a real Qt signal (ThumbnailWorker.ready), the same
signal-bridge pattern toast_qt.py established, replacing the old
queue.Queue + after()-poll.

TrimDialog follows the same worker pattern for its own blocking operations
(the ffprobe duration probe, the two preview frame-grabs, and the trim
remux itself): _TrimWorker runs them on daemon threads and delivers results
back through queued signals, so the dialog never blocks the GUI thread.
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from clipersal import concat, thumbnails
from clipersal.brand import SprigAccent
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_THUMBNAIL_WIDTH = 160
_THUMBNAIL_HEIGHT = 90
_PREVIEW_WIDTH = 240
_PREVIEW_HEIGHT = 135
_PREVIEW_DEBOUNCE_MS = 500


def reveal_in_file_manager(path: Path) -> None:
    """Open the containing folder with the file highlighted, where the
    platform supports it -- falls back to just opening the folder.
    """
    if sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(path)], check=False)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        # Most Linux file managers have no consistent "select this file"
        # convention across DEs -- just open the folder.
        open_folder(path.parent)


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def parse_timestamp(text: str) -> float | None:
    """Parse a trim-dialog timestamp: either plain seconds ("90", "90.5")
    or mm:ss.s ("1:30", "1:30.5"). Returns seconds as a float, or None for
    anything that doesn't fit one of those two shapes -- the dialog treats
    None as "not a usable value" and keeps Trim disabled rather than
    guessing at creative formats (hh:mm:ss is deliberately NOT accepted:
    clips are buffer-length short, and two shapes keep the rules obvious).
    """
    text = text.strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            return None
        minutes_text, seconds_text = parts
        if not minutes_text.isdigit() or not seconds_text:
            return None
        try:
            seconds = float(seconds_text)
        except ValueError:
            return None
        if not 0 <= seconds < 60:
            return None
        return int(minutes_text) * 60 + seconds
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _format_seconds(seconds: float) -> str:
    """mm:ss.s -- the same shape parse_timestamp accepts, so the duration
    readout, the End field's default, and the result label all speak one
    language. Rounds to tenths (that's all the dialog displays)."""
    total_tenths = int(round(seconds * 10))
    minutes, tenths = divmod(total_tenths, 600)
    return f"{minutes}:{tenths // 10:02d}.{tenths % 10}"


class ThumbnailWorker(QObject):
    """Constructed on the GUI thread; `.run()` executes on a background
    thread and emits one `ready` signal per clip as its thumbnail finishes,
    so rows update progressively rather than all at once at the end.
    """

    ready = Signal(Path, object)  # clip_path, thumbnail_path | None

    def __init__(self, ffmpeg_path: str, clip_paths: list[Path], cache_dir: Path, stop_event: threading.Event) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_paths = clip_paths
        self._cache_dir = cache_dir
        self._stop_event = stop_event

    def run(self) -> None:
        for clip_path in self._clip_paths:
            if self._stop_event.is_set():
                return
            thumb = thumbnails.ensure_thumbnail(self._ffmpeg_path, clip_path, self._cache_dir)
            self.ready.emit(clip_path, thumb)


class ClipRow(QFrame):
    """One saved clip: thumbnail + name/meta + Open/Reveal/Rename/Trim/Delete.
    The five buttons are public attributes -- GalleryFrame wires their
    `clicked` signals up, this class only knows how to lay itself out.
    """

    def __init__(self, clip_path: Path, stat_result, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip_path = clip_path
        self.setObjectName("card")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.thumb_label = QLabel(self)
        self.thumb_label.setObjectName("thumbPlaceholder")
        self.thumb_label.setFixedSize(_THUMBNAIL_WIDTH, _THUMBNAIL_HEIGHT)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.thumb_label)

        right_column = QVBoxLayout()
        layout.addLayout(right_column, 1)

        name_label = QLabel(clip_path.name, self)
        bold_font = name_label.font()
        bold_font.setBold(True)
        name_label.setFont(bold_font)
        right_column.addWidget(name_label)

        saved_at = datetime.fromtimestamp(stat_result.st_mtime).strftime("%b %d, %Y  %I:%M %p")
        meta_label = QLabel(f"{saved_at}  ·  {_format_size(stat_result.st_size)}", self)
        meta_label.setObjectName("hint")
        right_column.addWidget(meta_label)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        right_column.addLayout(actions)

        self.open_button = QPushButton("Open", self)
        self.reveal_button = QPushButton("Reveal", self)
        self.rename_button = QPushButton("Rename", self)
        self.trim_button = QPushButton("Trim", self)
        self.delete_button = QPushButton("Delete", self)
        for button in (self.open_button, self.reveal_button, self.rename_button, self.trim_button, self.delete_button):
            button.setFixedHeight(26)
            actions.addWidget(button)
        actions.addStretch()
        right_column.addStretch()

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumb_label.setPixmap(pixmap)


class _TrimWorker(QObject):
    """TrimDialog's background-thread half, the same shape as
    ThumbnailWorker: constructed on the GUI thread, its methods run on
    daemon threads (the ffprobe duration probe, the preview frame-grabs,
    and the trim remux itself -- none of which may block the GUI thread),
    and every result comes back through a queued signal.
    """

    duration_ready = Signal(object)  # float | None -- the probed clip duration
    preview_ready = Signal(str, float, object)  # "start" | "end", requested offset, frame path | None
    trim_finished = Signal(object, object)  # output path | None, error detail | None -- exactly one is set

    def __init__(self, ffmpeg_path: str, clip_path: Path, clips_dir: Path) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._clips_dir = clips_dir

    def probe_duration(self) -> None:
        ffprobe_path = thumbnails.find_ffprobe(self._ffmpeg_path)
        duration = thumbnails.get_duration_seconds(ffprobe_path, self._clip_path) if ffprobe_path else None
        self.duration_ready.emit(duration)

    def grab_preview(self, which: str, offset_seconds: float) -> None:
        # Preview frames go in the thumbnail cache dir with a leading-dot
        # name that matches no clip stem, so the gallery's orphaned-
        # thumbnail sweep removes them on the next refresh -- no separate
        # cleanup is needed even when the dialog is cancelled.
        target = (
            self._clips_dir
            / thumbnails.THUMBNAIL_DIR_NAME
            / f".trim-{self._clip_path.stem}-{which}-{offset_seconds:.3f}.jpg"
        )
        frame = thumbnails.grab_frame_at(self._ffmpeg_path, self._clip_path, offset_seconds, target)
        self.preview_ready.emit(which, offset_seconds, frame)

    def trim(self, start_seconds: float, end_seconds: float, duration_seconds: float) -> None:
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
            log.warning("Trim of %s failed: %s", self._clip_path, exc)
            self.trim_finished.emit(None, str(exc))
        except Exception as exc:  # noqa: BLE001 -- every trim failure goes inline in the dialog, never a traceback
            log.exception("Trim of %s failed unexpectedly", self._clip_path)
            self.trim_finished.emit(None, str(exc))
        else:
            self.trim_finished.emit(output, None)


class TrimDialog(QDialog):
    """Modal trim editor for one clip: Start/End fields (mm:ss.s or plain
    seconds), a frame preview at each cut point, and a live result-duration
    readout. The trim itself runs on _TrimWorker -- on success
    `trim_succeeded` carries the new clip's path and the dialog closes; on
    failure the error is shown inline and the dialog stays open.

    The fields and buttons are public attributes for the same reason
    ClipRow's buttons are: GalleryFrame (and tests) drive the dialog
    through them.
    """

    trim_succeeded = Signal(Path)

    def __init__(self, ffmpeg_path: str, clip_path: Path, clips_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip_path = clip_path
        self._duration: float | None = None
        self._duration_probe_done = False
        self._trim_error: str | None = None
        self._trimming = False

        self.setWindowTitle("Trim clip")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title_label = QLabel(clip_path.name, self)
        title_font = title_label.font()
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        self._duration_label = QLabel("Probing duration…", self)
        self._duration_label.setObjectName("hint")
        layout.addWidget(self._duration_label)

        previews_row = QHBoxLayout()
        previews_row.setSpacing(10)
        layout.addLayout(previews_row)
        self._preview_labels: dict[str, QLabel] = {}
        for which, caption in (("start", "Start"), ("end", "End")):
            column = QVBoxLayout()
            previews_row.addLayout(column)
            preview = QLabel(self)
            preview.setObjectName("thumbPlaceholder")
            preview.setFixedSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setText("No frame yet")
            column.addWidget(preview)
            caption_label = QLabel(caption, self)
            caption_label.setObjectName("hint")
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(caption_label)
            self._preview_labels[which] = preview

        fields_row = QHBoxLayout()
        fields_row.setSpacing(10)
        layout.addLayout(fields_row)
        self.start_field = QLineEdit("0:00.0", self)
        self.end_field = QLineEdit(self)
        self.end_field.setPlaceholderText("mm:ss.s or seconds")
        for caption, field in (("Start", self.start_field), ("End", self.end_field)):
            column = QVBoxLayout()
            fields_row.addLayout(column)
            field_label = QLabel(caption, self)
            field_label.setObjectName("hint")
            column.addWidget(field_label)
            column.addWidget(field)

        note_label = QLabel("Cuts snap to the nearest ~2 s keyframe.", self)
        note_label.setObjectName("hint")
        layout.addWidget(note_label)

        self._result_label = QLabel("Result: --", self)
        layout.addWidget(self._result_label)

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("statusLabel")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        buttons_row = QHBoxLayout()
        layout.addLayout(buttons_row)
        buttons_row.addStretch()
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons_row.addWidget(self.cancel_button)
        self.trim_button = QPushButton("Trim", self)
        self.trim_button.setObjectName("primary")
        self.trim_button.clicked.connect(self._start_trim)
        buttons_row.addWidget(self.trim_button)

        self._worker = _TrimWorker(ffmpeg_path, clip_path, clips_dir)
        self._worker.duration_ready.connect(self._on_duration_ready)
        self._worker.preview_ready.connect(self._on_preview_ready)
        self._worker.trim_finished.connect(self._on_trim_finished)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._load_previews)

        self.start_field.textChanged.connect(self._on_fields_changed)
        self.end_field.textChanged.connect(self._on_fields_changed)

        self._refresh_validation()
        threading.Thread(target=self._worker.probe_duration, daemon=True).start()

    def _on_fields_changed(self) -> None:
        # Editing either field supersedes a previous trim failure -- the
        # range that error referred to no longer applies.
        self._trim_error = None
        self._refresh_validation()
        # Debounce the frame-grabs: each preview is an ffmpeg subprocess,
        # so grabbing per keystroke would spawn a process per character.
        self._preview_timer.start(_PREVIEW_DEBOUNCE_MS)

    def _refresh_validation(self) -> None:
        start = parse_timestamp(self.start_field.text())
        end = parse_timestamp(self.end_field.text())
        problem = None
        if start is None or end is None:
            self._result_label.setText("Result: --")
            # Only nag about an unparseable field once something's actually
            # been typed into it; empty fields just keep Trim disabled.
            if (start is None and self.start_field.text().strip()) or (end is None and self.end_field.text().strip()):
                problem = "Enter times as mm:ss.s or plain seconds."
        elif start >= end:
            self._result_label.setText("Result: --")
            problem = "Start must be before End."
        elif self._duration is not None and end > self._duration:
            self._result_label.setText("Result: --")
            problem = f"End is past the clip's duration ({_format_seconds(self._duration)})."
        else:
            self._result_label.setText(f"Result: {_format_seconds(end - start)}")
        if problem is None and self._duration is None and self._duration_probe_done:
            problem = "Could not determine this clip's duration -- trim is unavailable."
        self._set_error(self._trim_error or problem or "")
        self.trim_button.setEnabled(
            not self._trimming
            and problem is None
            and start is not None
            and end is not None
            and self._duration is not None
        )

    def _set_error(self, text: str) -> None:
        # Same unpolish/polish dance as first_run_qt: the `state` property
        # drives the QSS color, which Qt doesn't re-evaluate on its own.
        self._error_label.setText(text)
        self._error_label.setProperty("state", "error" if text else "")
        style = self._error_label.style()
        style.unpolish(self._error_label)
        style.polish(self._error_label)

    def _on_duration_ready(self, duration: float | None) -> None:
        self._duration_probe_done = True
        self._duration = duration
        if duration is None:
            self._duration_label.setText("Duration unknown")
        else:
            self._duration_label.setText(f"Total: {_format_seconds(duration)}")
            if not self.end_field.text().strip():
                # Pre-fill End with the full duration -- only when the user
                # hasn't already typed something while the probe ran.
                self.end_field.setText(_format_seconds(duration))
        self._refresh_validation()
        self._load_previews()

    def _load_previews(self) -> None:
        for which, field in (("start", self.start_field), ("end", self.end_field)):
            offset = parse_timestamp(field.text())
            if offset is None:
                self._clear_preview(which)
                continue
            threading.Thread(target=self._worker.grab_preview, args=(which, offset), daemon=True).start()

    def _clear_preview(self, which: str, text: str = "No frame yet") -> None:
        label = self._preview_labels[which]
        label.clear()
        label.setText(text)

    def _on_preview_ready(self, which: str, offset: float, frame_path: Path | None) -> None:
        field = self.start_field if which == "start" else self.end_field
        current = parse_timestamp(field.text())
        if current is None or abs(current - offset) > 1e-9:
            return  # stale grab -- the field moved on while ffmpeg was running
        pixmap = QPixmap(str(frame_path)) if frame_path is not None else QPixmap()
        if pixmap.isNull():
            self._clear_preview(which, text="No frame at this time")
            return
        self._preview_labels[which].setPixmap(
            pixmap.scaled(
                _PREVIEW_WIDTH,
                _PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _start_trim(self) -> None:
        start = parse_timestamp(self.start_field.text())
        end = parse_timestamp(self.end_field.text())
        # The Trim button is disabled unless all three values are known and
        # valid -- this re-check is belt-and-braces for a direct call.
        if start is None or end is None or self._duration is None or self._trimming:
            return
        self._trimming = True
        self._trim_error = None
        self._result_label.setText("Trimming…")
        self.trim_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.start_field.setEnabled(False)
        self.end_field.setEnabled(False)
        threading.Thread(target=self._worker.trim, args=(start, end, self._duration), daemon=True).start()

    def _on_trim_finished(self, output_path: Path | None, error: str | None) -> None:
        self._trimming = False
        self.cancel_button.setEnabled(True)
        self.start_field.setEnabled(True)
        self.end_field.setEnabled(True)
        if error is not None:
            # Collapse to one line: ConcatFailedError can carry a chunk of
            # ffmpeg stderr, unreadable as a multi-line wall in a small dialog.
            summary = " ".join(error.split())
            if len(summary) > 300:
                summary = summary[:297] + "..."
            self._trim_error = summary
            self._refresh_validation()
            return
        self.trim_succeeded.emit(output_path)
        self.accept()


class GalleryFrame(QWidget):
    def __init__(self, ffmpeg_path: str, clips_dir_provider: Callable[[], Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ffmpeg_path = ffmpeg_path
        # A live provider, not a frozen Path: apply_settings live-mutates
        # config.clips_dir, and this tab must list/open the folder saves go
        # to NOW, not the one captured when the window was built.
        self._clips_dir_provider = clips_dir_provider
        self._rows: dict[Path, ClipRow] = {}
        self._stop_worker = threading.Event()
        self._worker: ThumbnailWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)

        header = QHBoxLayout()
        outer.addLayout(header)
        title = QLabel("Clips", self)
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        title.setFont(title_font)
        header.addWidget(title)
        header.addStretch()

        open_folder_button = QPushButton("Open folder", self)
        open_folder_button.clicked.connect(lambda: open_folder(self._clips_dir_provider()))
        header.addWidget(open_folder_button)

        refresh_button = QPushButton("Refresh", self)
        refresh_button.setObjectName("primary")
        refresh_button.clicked.connect(self.refresh)
        header.addWidget(refresh_button)

        self._empty_container = QWidget(self)
        empty_layout = QVBoxLayout(self._empty_container)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(SprigAccent(size=56), alignment=Qt.AlignmentFlag.AlignHCenter)
        self._empty_label = QLabel(
            'No clips saved yet -- press your hotkey or use "Save now" to make one.', self._empty_container
        )
        self._empty_label.setObjectName("hint")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_label)
        outer.addWidget(self._empty_container)
        self._empty_container.hide()

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        outer.addWidget(self._scroll_area, 1)

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()  # keeps rows top-aligned as they're added/removed
        self._scroll_area.setWidget(self._list_container)

        self.refresh()

    def refresh(self) -> None:
        self._stop_worker.set()  # stop any in-flight worker from a previous refresh
        for row in list(self._rows.values()):
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._empty_container.hide()

        # Read the provider once per refresh: the folder can't meaningfully
        # change mid-pass, and a Settings change is picked up on the next one.
        clips_dir = self._clips_dir_provider()
        clip_paths = sorted(clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not clip_paths:
            self._empty_container.show()
            return

        for clip_path in clip_paths:
            self._add_row(clip_path)

        thumbnails.cleanup_orphaned_thumbnails(
            clips_dir / thumbnails.THUMBNAIL_DIR_NAME, {p.stem for p in clip_paths}
        )

        self._stop_worker = threading.Event()
        worker = ThumbnailWorker(
            self._ffmpeg_path, clip_paths, clips_dir / thumbnails.THUMBNAIL_DIR_NAME, self._stop_worker
        )
        worker.ready.connect(self._apply_thumbnail)
        self._worker = worker  # kept alive for the duration of the background thread
        threading.Thread(target=worker.run, daemon=True).start()

    def _add_row(self, clip_path: Path) -> None:
        try:
            stat_result = clip_path.stat()
        except OSError:
            return
        row = ClipRow(clip_path, stat_result, self._list_container)
        row.open_button.clicked.connect(lambda: open_folder(clip_path))
        row.reveal_button.clicked.connect(lambda: reveal_in_file_manager(clip_path))
        row.rename_button.clicked.connect(lambda: self._do_rename(clip_path))
        row.trim_button.clicked.connect(lambda: self._do_trim(clip_path))
        row.delete_button.clicked.connect(lambda: self._do_delete(clip_path))
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)  # before the trailing stretch
        self._rows[clip_path] = row

    def _do_rename(self, clip_path: Path) -> None:
        new_stem, ok = QInputDialog.getText(self, "Rename clip", f"New name for {clip_path.name}:", text=clip_path.stem)
        if not ok or not new_stem:
            return
        # A name containing a path separator makes with_name() raise an
        # uncaught ValueError ("Invalid name") -- and a clip can never
        # legitimately live outside clips_dir, so reject it as a bad name.
        if "/" in new_stem or "\\" in new_stem:
            QMessageBox.warning(self, "Rename clip", f"A clip name cannot contain path separators: {new_stem!r}")
            return
        new_path = clip_path.with_name(f"{new_stem}{clip_path.suffix}")
        if new_path == clip_path:
            return  # unchanged name -- nothing to do, and not an overwrite
        # Path.rename() silently REPLACES an existing destination on POSIX;
        # only Windows raises FileExistsError. Refuse up front on every
        # platform rather than destroy another clip without any prompt.
        if new_path.exists():
            QMessageBox.warning(self, "Rename clip", f"A clip named {new_path.name} already exists.")
            return
        try:
            clip_path.rename(new_path)
        except OSError as exc:
            log.warning("Could not rename %s to %s: %s", clip_path, new_path, exc)
            return
        self.refresh()

    def _do_trim(self, clip_path: Path) -> None:
        # Read the provider once as the dialog opens -- the same
        # once-per-action read as refresh(), so a Settings folder change
        # mid-dialog is picked up by the next action instead.
        dialog = TrimDialog(self._ffmpeg_path, clip_path, self._clips_dir_provider(), self)
        dialog.trim_succeeded.connect(self._on_trim_succeeded)
        dialog.exec()

    def _on_trim_succeeded(self, output_path: Path) -> None:
        self.refresh()
        QMessageBox.information(self, "Trim clip", f"Saved trimmed copy as {output_path.name}.")

    def _do_delete(self, clip_path: Path) -> None:
        reply = QMessageBox.question(
            self,
            "Delete clip",
            f"Delete {clip_path.name}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            clip_path.unlink()
        except OSError as exc:
            log.warning("Could not delete %s: %s", clip_path, exc)
            return
        row = self._rows.pop(clip_path, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        if not self._rows:
            self._empty_container.show()

    def _apply_thumbnail(self, clip_path: Path, thumbnail_path: Path | None) -> None:
        row = self._rows.get(clip_path)
        if row is None or thumbnail_path is None:
            return
        pixmap = QPixmap(str(thumbnail_path))
        if pixmap.isNull():
            return
        scaled = pixmap.scaled(
            _THUMBNAIL_WIDTH,
            _THUMBNAIL_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        row.set_thumbnail(scaled)


def build_gallery_frame(parent: QWidget | None, ffmpeg_path: str, clips_dir_provider: Callable[[], Path]) -> GalleryFrame:
    """Thin factory function kept for API parity with the CustomTkinter
    original's `build_gallery_frame(parent, ffmpeg_path, clips_dir)` shape --
    the third argument is now a live clips-dir provider, see GalleryFrame.
    """
    return GalleryFrame(ffmpeg_path, clips_dir_provider, parent)
