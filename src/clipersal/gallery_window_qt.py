"""Clips tab. Lists saved clips in clips_dir with a thumbnail, saved-at date,
and size; supports open / reveal-in-folder / rename / delete / refresh.

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
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from clipersal import thumbnails
from clipersal.brand import SprigAccent
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_THUMBNAIL_WIDTH = 160
_THUMBNAIL_HEIGHT = 90


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
    """One saved clip: thumbnail + name/meta + Open/Reveal/Rename/Delete.
    The four buttons are public attributes -- GalleryFrame wires their
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
        self.delete_button = QPushButton("Delete", self)
        for button in (self.open_button, self.reveal_button, self.rename_button, self.delete_button):
            button.setFixedHeight(26)
            actions.addWidget(button)
        actions.addStretch()
        right_column.addStretch()

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumb_label.setPixmap(pixmap)


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
