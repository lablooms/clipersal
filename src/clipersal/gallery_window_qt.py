"""Clips tab. Lists saved clips in clips_dir in two switchable views -- the
list (rows of thumbnail + name/meta + Play/heart/"⋯") and a thumbnail grid --
with one shared view state (search text, window filter, sort mode,
favorites-first, selection mode + batch delete) applied identically to both
by _reapply_view. Each face carries a favorite heart (persisted via
clip_metadata's sidecar) and a "⋯" overflow button that pops the same
right-click context menu holding every other action (open /
reveal-in-folder / rename / trim-via-player / export / copy path / copy
filename / delete); the list row adds a Play button, the grid card plays on
double-click like a file manager. The window filter groups clips by the
{window} part of the default filename template -- see
window_name_from_clip_name.

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

0.1.4 adds the creator features on top: double-click (or the row's Play
button, or the context menu's "Play"/"Trim...") opens the clip in
player_qt's in-app PlayerDialog via the shared player_qt.play_clip helper
when QtMultimedia is importable and in the OS default player otherwise (the
player's own trim card replaced the old modal TrimDialog); "Details..." shows
file facts, a worker-probed duration/resolution, a favorite switch, and the
editable note that surfaces as the face's tooltip; "Export as GIF..." and
"Compress..." run export.py on worker threads; and dragging a row or card by
its thumbnail produces file URLs (QMimeData) for drops into Explorer/chat
apps.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QMimeData, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDrag, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from clipersal import clip_metadata, export, player_qt, theme, thumbnails
from clipersal.brand import SprigAccent
from clipersal.qt_widgets import ElidedLabel, SegmentedControl, ToggleSwitch, quiet_message
from clipersal.theme import qfont as _qfont
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_THUMBNAIL_WIDTH = 160
_THUMBNAIL_HEIGHT = 90
_PREVIEW_WIDTH = 240
_PREVIEW_HEIGHT = 135
# Grid view: fixed-width cards; the thumbnail fills the card modulo its 10px
# margins (196 - 2*10 = 176, a 16:9 box).
_GRID_CARD_WIDTH = 196
_GRID_THUMBNAIL_WIDTH = 176
_GRID_THUMBNAIL_HEIGHT = 99
_GRID_SPACING = 12

SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
SORT_NAME = "name"
SORT_LARGEST = "largest"
SORT_WINDOW = "window"

# The exact empty-state copy, shared by this tab and the main window's
# recent-clips strip so both read identically (feedback #16).
EMPTY_CLIPS_MESSAGE = "no record of your bloom-bloom moments, yet."
# (label, key) pairs in combo order; GalleryFrame stores the key, and tests
# select entries via findData(key) rather than a fragile index.
_SORT_CHOICES = (
    ("Newest first", SORT_NEWEST),
    ("Oldest first", SORT_OLDEST),
    ("Name A–Z", SORT_NAME),
    ("Largest first", SORT_LARGEST),
    ("Window A–Z", SORT_WINDOW),
)

# A clip filename from the default {window}-{date}-{time} template, with the
# collision suffix (-1, -2, ...) and the -trimmed/-compressed export suffixes
# (plus their own collision suffixes) all optional. Custom filename templates
# can produce names this doesn't match -- those parse as None and group under
# "Other" in the window filter.
_CLIP_WINDOW_RE = re.compile(
    r"^(?P<window>.+?)-\d{8}-\d{6}(-\d+)?(-trimmed(-\d+)?|-compressed(-\d+)?)?$"
)

# The window filter's "show everything" value. A parsed window name is always
# a non-empty str (the regex requires at least one character) and the
# catch-all "Other" entry carries None, so "" can never collide with a real
# filter value.
WINDOW_FILTER_ALL = ""


def window_name_from_clip_name(name: str) -> str | None:
    """The {window} part of a clip filename produced by the default template
    ("Valorant-20260722-011351.mp4" -> "Valorant"), or None for a name the
    template couldn't have produced. Old "clip-..." default names parse to
    "clip" -- they group together, which is exactly right. The window part is
    whatever precedes the FIRST -YYYYMMDD-HHMMSS run: window titles may
    themselves contain dashes and numbers, so only the 8+6-digit timestamp is
    a safe anchor. Collision (-1) and export (-trimmed / -compressed, with
    their own collision suffixes) tails are stripped too.
    """
    stem = name[:-4] if name.lower().endswith(".mp4") else name
    match = _CLIP_WINDOW_RE.match(stem)
    return match.group("window") if match is not None else None


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


def _format_duration(seconds: float) -> str:
    """M:SS for the row meta line -- whole seconds are plenty next to the
    saved-at date (the player's tenth-precision _format_clock_tenths is a
    different, editing-oriented language)."""
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}:{secs:02d}"


def clips_newest_first(clips_dir: Path) -> list[Path]:
    """clips_dir's .mp4 files, newest mtime first. A clip can vanish between
    the glob and the sort-key stat (the retention sweep runs on the IPC
    thread, or the user deletes a file externally) -- a bare
    `sorted(..., key=lambda p: p.stat().st_mtime)` let that one racy
    deletion abort the whole refresh with FileNotFoundError, so each stat
    gets the same skip-on-OSError rule as GalleryFrame._add_clip. Shared by
    the gallery and the main window's recent-clips strip so there's exactly
    one implementation.
    """
    with_mtimes: list[tuple[float, Path]] = []
    for path in clips_dir.glob("*.mp4"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # vanished between glob and stat -- leave it out
        with_mtimes.append((mtime, path))
    with_mtimes.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in with_mtimes]


def _elide_note(note: str, limit: int = 80) -> str:
    """A clip note's first line, hard-elided, for a row's tooltip -- ""
    (the common case) clears the tooltip entirely so stale text never
    survives a note being deleted in the details dialog."""
    stripped = note.strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0]
    if len(first_line) > limit:
        return first_line[: limit - 1] + "…"
    return first_line


class ThumbnailWorker(QObject):
    """Constructed on the GUI thread; `.run()` executes on a background
    thread and emits one `ready` signal per clip as its thumbnail finishes,
    so rows update progressively rather than all at once at the end.

    When constructed with an `ffprobe_path` it also probes each clip's
    duration in the same per-clip loop and emits `duration_ready` (the
    gallery's meta line uses it). Callers that don't need durations -- the
    main window's recent-clips strip -- omit the path and the probe is
    skipped entirely, keeping their construction unchanged.
    """

    ready = Signal(Path, object)  # clip_path, thumbnail_path | None
    duration_ready = Signal(Path, object)  # clip_path, duration seconds | None

    def __init__(
        self,
        ffmpeg_path: str,
        clip_paths: list[Path],
        cache_dir: Path,
        stop_event: threading.Event,
        ffprobe_path: str | None = None,
    ) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_paths = clip_paths
        self._cache_dir = cache_dir
        self._stop_event = stop_event
        self._ffprobe_path = ffprobe_path

    def run(self) -> None:
        for clip_path in self._clip_paths:
            if self._stop_event.is_set():
                return
            thumb = thumbnails.ensure_thumbnail(self._ffmpeg_path, clip_path, self._cache_dir)
            self.ready.emit(clip_path, thumb)
            if self._ffprobe_path is None:
                continue
            if self._stop_event.is_set():
                return
            # get_duration_seconds is best-effort (None on any failure), so
            # an unprobeable clip just emits None and the meta line keeps
            # its no-duration shape -- never a crash, never a stall.
            duration = thumbnails.get_duration_seconds(self._ffprobe_path, clip_path)
            self.duration_ready.emit(clip_path, duration)


class _ClipFace(QFrame):
    """The shared half of a clip's two gallery faces (the list's ClipRow and
    the grid's ClipCard): the cached stat data, the selection checkbox, the
    thumbnail label, the heart + "⋯" buttons, and the two mouse behaviors
    that are inherently per-face -- double-click (emitted as
    `double_clicked`; GalleryFrame opens the clip) and
    drag-out-of-the-gallery from the thumbnail area (file URLs for
    Explorer/chat drops). The thumbnail is a child QLabel whose unhandled
    mouse events propagate up to the face, so both gestures fire over it too.

    The buttons, checkbox, and meta label are public attributes --
    GalleryFrame wires their signals up; a face only knows how to lay itself
    out and how to re-render its own small pieces of state. Subclasses call
    super().__init__ (which builds the common widgets) and then lay them out.
    """

    double_clicked = Signal()

    def __init__(self, clip_path: Path, stat_result, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip_path = clip_path
        # Position of the last left-button press, for the drag gesture's
        # start-distance check; None while no potential drag is armed.
        self._drag_press_pos = None
        # Stat data is cached at construction (the stat call itself is
        # vanish-tolerant, in GalleryFrame._add_clip) so the gallery's
        # size/age sorts never re-stat a file that may have vanished since.
        self.size_bytes = stat_result.st_size
        self.mtime = stat_result.st_mtime
        self.setObjectName("card")
        # The menu itself is built by GalleryFrame, which owns the handlers.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self._saved_at_text = datetime.fromtimestamp(stat_result.st_mtime).strftime("%b %d, %Y  %I:%M %p")
        self._size_text = _format_size(stat_result.st_size)

        self.select_checkbox = QCheckBox(self)
        self.select_checkbox.setVisible(False)  # selection mode only -- GalleryFrame toggles it

        self.thumb_label = QLabel(self)
        self.thumb_label.setObjectName("thumbPlaceholder")
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ♡/♥ with the accent color when checked, via the global QSS
        # (#heartButton) -- theme tokens live in the stylesheet, so a live
        # theme switch recolors the heart like every other widget.
        self.favorite_button = QPushButton("♡", self)
        self.favorite_button.setObjectName("heartButton")
        self.favorite_button.setCheckable(True)
        self.favorite_button.setToolTip("Favorite")
        # Overflow: pops the same full context menu as a right-click on the
        # face (Open / Reveal / Rename / Trim / Delete and friends live there).
        self.menu_button = QPushButton("⋯", self)
        self.menu_button.setObjectName("menuButton")
        self.menu_button.setToolTip("More actions")
        for button in (self.favorite_button, self.menu_button):
            button.setFixedHeight(30)
        self.favorite_button.setFixedWidth(34)
        self.menu_button.setFixedWidth(34)
        self.favorite_button.toggled.connect(self._on_heart_toggled)

    def _on_heart_toggled(self, checked: bool) -> None:
        self.favorite_button.setText("♥" if checked else "♡")

    def set_favorite(self, favorite: bool) -> None:
        """Sync the heart to the persisted state. Signals are blocked so this
        never re-fires `toggled` -- GalleryFrame's handler on that signal is
        the only path allowed to write the sidecar, and it calls this."""
        self.favorite_button.blockSignals(True)
        self.favorite_button.setChecked(favorite)
        self.favorite_button.setText("♥" if favorite else "♡")
        self.favorite_button.blockSignals(False)

    def set_selection_visible(self, visible: bool) -> None:
        self.select_checkbox.setVisible(visible)

    def set_duration(self, seconds: float) -> None:
        """Append the probed duration to the meta line (per-face format).
        Called only for a successful probe -- a None result leaves the
        no-duration line alone."""
        raise NotImplementedError

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumb_label.setPixmap(pixmap)

    # ---- mouse: double-click to play, drag from the thumbnail to export ----

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_press_pos = event.position().toPoint()
        # super() AFTER storing: the default handler keeps normal clicks
        # (buttons, checkbox, context menu) working exactly as before.
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        if (
            self._drag_press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and self.thumb_label.geometry().contains(self._drag_press_pos)
            and (event.position().toPoint() - self._drag_press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._drag_press_pos = None  # one drag per press
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def _build_drag_mime_data(self) -> QMimeData:
        """The drag payload: the clip as a file URL, which is what Explorer,
        Finder, browsers, and chat apps all accept for a dropped file."""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.clip_path))])
        return mime

    def _start_drag(self) -> None:
        drag = QDrag(self)
        drag.setMimeData(self._build_drag_mime_data())
        pixmap = self.thumb_label.pixmap()
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
        drag.exec(Qt.DropAction.CopyAction)


class ClipRow(_ClipFace):
    """The list-mode face: thumbnail + name/meta + a slim action row (Play,
    the favorite heart, and the "⋯" overflow), with the selection checkbox
    leading in selection mode."""

    def __init__(self, clip_path: Path, stat_result, parent: QWidget | None = None) -> None:
        super().__init__(clip_path, stat_result, parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        layout.addWidget(self.select_checkbox, 0, Qt.AlignmentFlag.AlignVCenter)

        self.thumb_label.setFixedSize(_THUMBNAIL_WIDTH, _THUMBNAIL_HEIGHT)
        layout.addWidget(self.thumb_label)

        right_column = QVBoxLayout()
        right_column.setSpacing(4)
        layout.addLayout(right_column, 1)

        name_label = ElidedLabel(clip_path.name, self, Qt.TextElideMode.ElideMiddle)
        # Elided (middle, keeping the extension): a {window}-template clip
        # name can be arbitrarily long, and an unbounded name label would
        # push the action buttons out of the row at the window minimum.
        bold_font = name_label.font()
        bold_font.setBold(True)
        name_label.setFont(bold_font)
        right_column.addWidget(name_label)

        self.meta_label = ElidedLabel(f"{self._saved_at_text}  ·  {self._size_text}", self)
        # Also elided (right): a non-eliding label's MINIMUM width is its
        # full text width, which pushed the row past the viewport at narrow
        # widths and clipped the action buttons behind a horizontal scroll.
        self.meta_label.setObjectName("hint")
        right_column.addWidget(self.meta_label)
        right_column.addStretch()

        # Actions sit on the RIGHT edge, vertically centered against the
        # thumbnail -- the media-list shape (OBS/Spotify) that keeps the row
        # balanced. Bottom-left buttons left the whole right side of the row
        # dead, which read as broken at wide window sizes.
        self.play_button = QPushButton("Play", self)
        self.play_button.setMinimumWidth(64)
        self.play_button.setFixedHeight(30)
        layout.addWidget(self.play_button, 0, Qt.AlignmentFlag.AlignVCenter)
        for button in (self.favorite_button, self.menu_button):
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_duration(self, seconds: float) -> None:
        self.meta_label.setText(f"{self._saved_at_text}  ·  {self._size_text}  ·  {_format_duration(seconds)}")


class ClipCard(_ClipFace):
    """The grid-mode face: a fixed-width card with the thumbnail on top, the
    (middle-elided) name and a compact size/duration meta line beneath it,
    and the selection checkbox, heart, and "⋯" on a bottom row. No Play
    button -- double-click plays, like a file manager; every other action is
    the shared _ClipFace behavior (same context menu, same drag-out), so a
    clip acts identically in both views.
    """

    def __init__(self, clip_path: Path, stat_result, parent: QWidget | None = None) -> None:
        super().__init__(clip_path, stat_result, parent)
        self.setFixedWidth(_GRID_CARD_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.thumb_label.setFixedSize(_GRID_THUMBNAIL_WIDTH, _GRID_THUMBNAIL_HEIGHT)
        layout.addWidget(self.thumb_label, 0, Qt.AlignmentFlag.AlignHCenter)

        name_label = ElidedLabel(clip_path.name, self, Qt.TextElideMode.ElideMiddle)
        bold_font = name_label.font()
        bold_font.setBold(True)
        name_label.setFont(bold_font)
        layout.addWidget(name_label)

        self.meta_label = ElidedLabel(self._size_text, self)
        # Same elision rule as the row's meta line (the card is only 176px
        # wide inside) -- text() keeps the full string for tests/tooltips.
        self.meta_label.setObjectName("hint")
        layout.addWidget(self.meta_label)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)
        layout.addLayout(bottom_row)
        bottom_row.addWidget(self.select_checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        bottom_row.addStretch()
        bottom_row.addWidget(self.favorite_button)
        bottom_row.addWidget(self.menu_button)

    def set_duration(self, seconds: float) -> None:
        # "size · duration" -- the card is too narrow for the saved-at date,
        # and the name's own timestamp already carries it.
        self.meta_label.setText(f"{self._size_text}  ·  {_format_duration(seconds)}")


class _VideoInfoWorker(QObject):
    """ClipDetailsDialog's background half (same shape as ThumbnailWorker):
    `run()` executes on a daemon thread -- the thumbnail ensure (an ffmpeg
    frame-grab on a cold cache) and the get_video_info ffprobe call must
    never block the GUI thread -- and both results come back queued.
    """

    thumbnail_ready = Signal(object)  # thumbnail path | None
    info_ready = Signal(object)  # (duration, width, height) | None

    def __init__(self, ffmpeg_path: str, ffprobe_path: str | None, clip_path: Path, cache_dir: Path) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._clip_path = clip_path
        self._cache_dir = cache_dir

    def run(self) -> None:
        thumbnail = thumbnails.ensure_thumbnail(self._ffmpeg_path, self._clip_path, self._cache_dir)
        self.thumbnail_ready.emit(thumbnail)
        # get_video_info is best-effort (None on any failure) -- an
        # unprobeable clip shows "unknown", never a crash. No ffprobe means
        # no probe at all (the ThumbnailWorker duration rule).
        info = thumbnails.get_video_info(self._ffprobe_path, self._clip_path) if self._ffprobe_path else None
        self.info_ready.emit(info)


class ClipDetailsDialog(QDialog):
    """Modal details view for one clip: thumbnail, filename, size, saved-at,
    worker-probed duration + resolution ("unknown" when unprobeable), a
    favorite switch, and an editable note. The note persists to the
    .clipmeta.json sidecar on accept only; the favorite switch is wired by
    GalleryFrame to _set_favorite (live, accept-or-cancel alike) so it can
    never disagree with the row's heart. Fields are public attributes, the
    same convention as ClipRow's buttons.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        clip_path: Path,
        clips_dir: Path,
        favorite: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._clip_path = clip_path
        self._clips_dir = clips_dir

        self.setWindowTitle("Clip details")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        layout.addLayout(top_row)
        self.thumb_label = QLabel(self)
        self.thumb_label.setObjectName("thumbPlaceholder")
        self.thumb_label.setFixedSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self.thumb_label)

        facts_column = QVBoxLayout()
        top_row.addLayout(facts_column, 1)
        self.name_label = QLabel(clip_path.name, self)
        name_font = self.name_label.font()
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        facts_column.addWidget(self.name_label)
        # Stat is vanish-tolerant here too: the clip can be deleted between
        # the gallery's refresh and this dialog opening.
        try:
            stat_result = clip_path.stat()
            saved_at = datetime.fromtimestamp(stat_result.st_mtime).strftime("%b %d, %Y  %I:%M %p")
            size_text = _format_size(stat_result.st_size)
        except OSError:
            saved_at = "unknown"
            size_text = "unknown"
        self.meta_label = QLabel(f"Saved: {saved_at}  ·  Size: {size_text}", self)
        self.meta_label.setObjectName("hint")
        self.meta_label.setWordWrap(True)
        facts_column.addWidget(self.meta_label)
        self.info_label = QLabel("Duration: probing…  ·  Resolution: probing…", self)
        self.info_label.setObjectName("hint")
        facts_column.addWidget(self.info_label)

        favorite_row = QHBoxLayout()
        favorite_row.setSpacing(8)
        facts_column.addLayout(favorite_row)
        favorite_caption = QLabel("Favorite", self)
        favorite_row.addWidget(favorite_caption)
        favorite_row.addStretch()
        self.favorite_switch = ToggleSwitch(self, checked=favorite)
        favorite_row.addWidget(self.favorite_switch)
        facts_column.addStretch()

        note_caption = QLabel("Note", self)
        layout.addWidget(note_caption)
        self.note_edit = QPlainTextEdit(self)
        self.note_edit.setPlaceholderText("Add a note -- it shows as the clip's tooltip in the gallery.")
        self.note_edit.setPlainText(clip_metadata.note_for(clips_dir, clip_path.name))
        self.note_edit.setFixedHeight(90)
        layout.addWidget(self.note_edit)

        buttons_row = QHBoxLayout()
        layout.addLayout(buttons_row)
        buttons_row.addStretch()
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons_row.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save", self)
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.accept)
        buttons_row.addWidget(self.save_button)

        self._worker = _VideoInfoWorker(
            ffmpeg_path,
            thumbnails.find_ffprobe(ffmpeg_path),
            clip_path,
            clips_dir / thumbnails.THUMBNAIL_DIR_NAME,
        )
        self._worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._worker.info_ready.connect(self._on_info_ready)
        threading.Thread(target=self._worker.run, daemon=True).start()

    def _on_thumbnail_ready(self, thumbnail_path: Path | None) -> None:
        pixmap = QPixmap(str(thumbnail_path)) if thumbnail_path is not None else QPixmap()
        if pixmap.isNull():
            return  # keep the placeholder -- a missing thumbnail is not an error
        self.thumb_label.setPixmap(
            pixmap.scaled(
                _PREVIEW_WIDTH,
                _PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _on_info_ready(self, info) -> None:
        if info is None:
            self.info_label.setText("Duration: unknown  ·  Resolution: unknown")
            return
        duration, width, height = info
        duration_text = _format_duration(duration) if duration is not None else "unknown"
        resolution_text = f"{width}×{height}" if width and height else "unknown"
        self.info_label.setText(f"Duration: {duration_text}  ·  Resolution: {resolution_text}")

    def accept(self) -> None:
        # strip(): a whitespace-only "note" is no note -- set_note clears
        # the key on empty text, which is how a note gets deleted.
        clip_metadata.set_note(self._clips_dir, self._clip_path.name, self.note_edit.toPlainText().strip())
        super().accept()


class _ExportWorker(QObject):
    """The GIF/compress dialogs' background half -- same daemon-thread +
    queued-signal shape as ThumbnailWorker. export.py's functions spawn
    ffmpeg subprocesses that can run for minutes (compress), so they must
    never run on the GUI thread. Exactly one of the finished payloads is set.
    """

    finished = Signal(object, object)  # output path | None, error detail | None

    def __init__(self, ffmpeg_path: str, clip_path: Path, out_dir: Path, encoder: str | None = None) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._out_dir = out_dir
        self._encoder = encoder

    def export_gif(self, start: float, duration: float, fps: int, width: int) -> None:
        try:
            output = export.export_gif(
                self._ffmpeg_path, self._clip_path, self._out_dir,
                start=start, duration=duration, fps=fps, width=width,
            )
        except Exception as exc:  # noqa: BLE001 -- ExportError or ValueError; every failure goes inline
            log.warning("GIF export of %s failed: %s", self._clip_path, exc)
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(output, None)

    def compress(self, bitrate: str, scale_height: int | None) -> None:
        try:
            output = export.compress_clip(
                self._ffmpeg_path, self._encoder or "libx264", self._clip_path, self._out_dir,
                bitrate=bitrate, scale_height=scale_height,
            )
        except Exception as exc:  # noqa: BLE001 -- same inline-failure rule as export_gif
            log.warning("Compress of %s failed: %s", self._clip_path, exc)
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(output, None)


class _ExportDialog(QDialog):
    """Shared shell for the two export dialogs: a status label with the
    #statusLabel state polish, Cancel + action buttons that lock while the
    worker runs, and `export_succeeded` carrying the output Path on success
    (the dialog then closes; the gallery shows the result message). On
    failure the error shows inline and the dialog stays open.
    """

    export_succeeded = Signal(Path)

    def _init_export_state(self) -> None:
        self._exporting = False

    def _set_status(self, text: str, state: str = "") -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def _begin_export(self, working_text: str) -> bool:
        if self._exporting:
            return False
        self._exporting = True
        self.export_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._set_status(working_text)
        return True

    def _on_export_finished(self, output_path: Path | None, error: str | None) -> None:
        self._exporting = False
        self.cancel_button.setEnabled(True)
        self.export_button.setEnabled(True)
        if error is not None:
            summary = " ".join(error.split())
            if len(summary) > 300:
                summary = summary[:297] + "..."
            self._set_status(summary, "error")
            return
        # Success stays INLINE (and the dialog open for another tweak+export):
        # a QMessageBox.information here played the Windows "Asterisk" sound on
        # every export, and forced the dialog closed just to say "it worked".
        self._set_status(f"Saved as {output_path.name}", "success")
        self.cancel_button.setText("Close")
        self.export_succeeded.emit(output_path)


class GifExportDialog(_ExportDialog):
    """Modal "Export as GIF" editor for one clip: start/duration/fps/width
    fields (validated by the spin ranges to export_gif's accepted domain),
    an inline status label, and a worker-thread export."""

    def __init__(self, ffmpeg_path: str, clip_path: Path, out_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_export_state()
        self.setWindowTitle("Export as GIF")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title_label = QLabel(clip_path.name, self)
        title_font = title_label.font()
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        hint_label = QLabel("GIFs save next to the clip but don't appear in the gallery (not .mp4).", self)
        hint_label.setObjectName("hint")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        fields_row = QHBoxLayout()
        fields_row.setSpacing(10)
        layout.addLayout(fields_row)
        self.start_spin = QDoubleSpinBox(self)
        self.start_spin.setRange(0.0, 9999.0)
        self.start_spin.setDecimals(1)
        self.start_spin.setSuffix(" s")
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setRange(0.5, 30.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setValue(3.0)
        self.duration_spin.setSuffix(" s")
        self.fps_spin = QSpinBox(self)
        self.fps_spin.setRange(4, 30)
        self.fps_spin.setValue(12)
        self.width_spin = QSpinBox(self)
        self.width_spin.setRange(200, 1920)
        self.width_spin.setSingleStep(20)
        self.width_spin.setValue(480)
        self.width_spin.setSuffix(" px")
        for caption, field in (
            ("Start", self.start_spin),
            ("Duration", self.duration_spin),
            ("FPS", self.fps_spin),
            ("Width", self.width_spin),
        ):
            column = QVBoxLayout()
            fields_row.addLayout(column)
            field_label = QLabel(caption, self)
            field_label.setObjectName("hint")
            column.addWidget(field_label)
            column.addWidget(field)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons_row = QHBoxLayout()
        layout.addLayout(buttons_row)
        buttons_row.addStretch()
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons_row.addWidget(self.cancel_button)
        self.export_button = QPushButton("Export", self)
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self._start_export)
        buttons_row.addWidget(self.export_button)

        self._worker = _ExportWorker(ffmpeg_path, clip_path, out_dir)
        self._worker.finished.connect(self._on_export_finished)

    def _start_export(self) -> None:
        if not self._begin_export("Exporting GIF…"):
            return
        threading.Thread(
            target=self._worker.export_gif,
            args=(self.start_spin.value(), self.duration_spin.value(), self.fps_spin.value(), self.width_spin.value()),
            daemon=True,
        ).start()


class CompressDialog(_ExportDialog):
    """Modal "Compress" editor for one clip: a bitrate choice and an
    optional downscale, re-encoded with the current capture encoder (handed
    in by the gallery, which resolves it live from state.setup.encoder).
    The original clip is always kept; the output is a new -compressed.mp4."""

    _SCALE_CHOICES = (("Original size", None), ("720p", 720), ("480p", 480))

    def __init__(
        self,
        ffmpeg_path: str,
        encoder: str,
        clip_path: Path,
        out_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._init_export_state()
        self._encoder = encoder
        self.setWindowTitle("Compress clip")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title_label = QLabel(clip_path.name, self)
        title_font = title_label.font()
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        hint_label = QLabel(f"Re-encodes with {encoder}; the original clip is kept.", self)
        hint_label.setObjectName("hint")
        layout.addWidget(hint_label)

        fields_row = QHBoxLayout()
        fields_row.setSpacing(10)
        layout.addLayout(fields_row)
        self.bitrate_combo = QComboBox(self)
        for bitrate in ("2.5M", "4M", "6M", "8M"):
            self.bitrate_combo.addItem(bitrate)
        self.bitrate_combo.setCurrentText("4M")
        self.scale_combo = QComboBox(self)
        for label, height in self._SCALE_CHOICES:
            self.scale_combo.addItem(label, userData=height)
        for caption, field in (("Bitrate", self.bitrate_combo), ("Scale", self.scale_combo)):
            column = QVBoxLayout()
            fields_row.addLayout(column)
            field_label = QLabel(caption, self)
            field_label.setObjectName("hint")
            column.addWidget(field_label)
            column.addWidget(field)
        fields_row.addStretch()

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons_row = QHBoxLayout()
        layout.addLayout(buttons_row)
        buttons_row.addStretch()
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons_row.addWidget(self.cancel_button)
        self.export_button = QPushButton("Compress", self)
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self._start_export)
        buttons_row.addWidget(self.export_button)

        self._worker = _ExportWorker(ffmpeg_path, clip_path, out_dir, encoder=encoder)
        self._worker.finished.connect(self._on_export_finished)

    def _start_export(self) -> None:
        if not self._begin_export("Compressing…"):
            return
        threading.Thread(
            target=self._worker.compress,
            args=(self.bitrate_combo.currentText(), self.scale_combo.currentData()),
            daemon=True,
        ).start()


class GalleryFrame(QWidget):
    """The Clips tab. Owns the view state (search text, window filter, sort
    mode, favorites-first, selection mode + the selected paths, list/grid
    view) and re-applies it to the faces built by refresh() -- every clip
    gets a ClipRow AND a ClipCard up front, so the view switch only decides
    which container is showing and never rebuilds or loses thumbnails,
    durations, or checkbox states. The widgets a test (or the next wave)
    needs to drive are public attributes.

    `clips_changed` fires only when the set of clips on disk changes through
    the gallery (delete, batch delete, rename, a player's trim export, a
    compress export) -- NOT on a plain refresh/filter/sort/view-switch, which
    only re-reads or re-arranges what's already there. The main window
    connects it to its recent-clips strip so the strip follows gallery edits
    without waiting for the next save.
    """

    clips_changed = Signal()

    def __init__(
        self,
        ffmpeg_path: str,
        clips_dir_provider: Callable[[], Path],
        parent: QWidget | None = None,
        encoder_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._ffmpeg_path = ffmpeg_path
        # A live provider, not a frozen Path: apply_settings live-mutates
        # config.clips_dir, and this tab must list/open the folder saves go
        # to NOW, not the one captured when the window was built.
        self._clips_dir_provider = clips_dir_provider
        # Same live-provider reasoning for the compress dialog's encoder:
        # apply_settings can switch state.setup.encoder after this tab is
        # built, so a frozen value would go stale. None -> libx264.
        self._encoder_provider = encoder_provider
        self._rows: dict[Path, ClipRow] = {}
        self._cards: dict[Path, ClipCard] = {}
        # The grid's currently-visible cards, in display order; _reflow_grid
        # wraps them into however many columns the viewport fits.
        self._grid_order: list[ClipCard] = []
        self._grid_stretch_row: int | None = None
        self._grid_stretch_column: int | None = None
        self._stop_worker = threading.Event()
        self._worker: ThumbnailWorker | None = None
        # Open PlayerDialogs, kept referenced so the GC can't collect them
        # (they're modal-less; several may be open at once). Pruned on destroy.
        self._players: list = []

        # View state, re-applied to the existing faces on every change.
        self._search_text = ""
        self._sort_mode = SORT_NEWEST
        self._favorites_first = False
        # WINDOW_FILTER_ALL, a parsed window name (str), or None for the
        # catch-all "Other" entry -- see _rebuild_window_filter.
        self._window_filter: str | None = WINDOW_FILTER_ALL
        self._view_mode = "list"  # "list" | "grid"
        # Selection is keyed by Path and vanish-tolerant: every use either
        # unlinks behind try/except OSError or intersects with what refresh
        # actually enumerated.
        self._selection_mode = False
        self._selected: set[Path] = set()
        self._favorites: set[str] = set()  # filenames, loaded once per refresh
        self._notes: dict[str, str] = {}  # filename -> note, same per-refresh load

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        # Row 1 -- the header: title on the left; view switch + folder +
        # refresh on the right.
        header = QHBoxLayout()
        header.setSpacing(12)
        outer.addLayout(header)
        title = QLabel("Clips", self)
        title.setFont(_qfont(size=theme.FONT_H1))
        header.addWidget(title)
        header.addStretch()

        # SegmentedControl's own size policy is Expanding (it fills a
        # Settings card); here it must hug its two buttons on the header's
        # right edge.
        self.view_switch = SegmentedControl(["List", "Grid"], self)
        self.view_switch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.view_switch.setToolTip("Switch between the list and the thumbnail grid")
        self.view_switch.currentTextChanged.connect(self._on_view_changed)
        header.addWidget(self.view_switch)

        open_folder_button = QPushButton("Open folder", self)
        open_folder_button.clicked.connect(lambda: open_folder(self._clips_dir_provider()))
        header.addWidget(open_folder_button)

        refresh_button = QPushButton("Refresh", self)
        refresh_button.setObjectName("primary")
        refresh_button.clicked.connect(self.refresh)
        header.addWidget(refresh_button)

        # Row 2 -- the view controls. Every one of them re-applies in place
        # via _reapply_view and acts on both views identically.
        controls = QHBoxLayout()
        controls.setSpacing(12)
        outer.addLayout(controls)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search clips…")
        self.search_edit.setClearButtonEnabled(True)
        # A usable floor at the 1000px window minimum: the controls to its
        # right can't shrink, and without this the search collapsed to an
        # unusable sliver there first.
        self.search_edit.setMinimumWidth(120)
        self.search_edit.textChanged.connect(self._on_search_changed)
        controls.addWidget(self.search_edit, 1)

        # Window filter: "All windows" plus one entry per distinct {window}
        # name parsed from the current clips (with per-window clip counts),
        # rebuilt on every refresh by _rebuild_window_filter. Capped so a
        # long window title can't squeeze the other controls at the 1000px
        # window minimum.
        self.window_filter_combo = QComboBox(self)
        self.window_filter_combo.setMaximumWidth(180)
        self.window_filter_combo.setToolTip("Filter by the window a clip was saved from")
        self.window_filter_combo.currentIndexChanged.connect(self._on_window_filter_changed)
        controls.addWidget(self.window_filter_combo)

        self.sort_combo = QComboBox(self)
        for label, key in _SORT_CHOICES:
            self.sort_combo.addItem(label, userData=key)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        controls.addWidget(self.sort_combo)

        favorites_first_label = QLabel("Favorites first", self)
        favorites_first_label.setObjectName("hint")
        controls.addWidget(favorites_first_label)
        self.favorites_first_switch = ToggleSwitch(self)
        self.favorites_first_switch.toggled.connect(self._on_favorites_first_changed)
        controls.addWidget(self.favorites_first_switch)

        self.select_button = QPushButton("Select", self)
        self.select_button.setCheckable(True)
        self.select_button.toggled.connect(self._set_selection_mode)
        controls.addWidget(self.select_button)

        # Selection-mode bar, hidden until the Select toggle is on.
        self._selection_bar = QWidget(self)
        selection_layout = QHBoxLayout(self._selection_bar)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.setSpacing(8)
        self.select_all_button = QPushButton("All", self._selection_bar)
        self.select_all_button.clicked.connect(self._select_all_visible)
        self.select_none_button = QPushButton("None", self._selection_bar)
        self.select_none_button.clicked.connect(self._select_none)
        for button in (self.select_all_button, self.select_none_button):
            button.setFixedHeight(26)
            selection_layout.addWidget(button)
        selection_layout.addStretch()
        self.delete_selected_button = QPushButton("Delete selected (0)", self._selection_bar)
        self.delete_selected_button.setObjectName("danger")
        self.delete_selected_button.setEnabled(False)
        self.delete_selected_button.clicked.connect(self._do_delete_selected)
        selection_layout.addWidget(self.delete_selected_button)
        outer.addWidget(self._selection_bar)
        self._selection_bar.hide()

        self._empty_container = QWidget(self)
        empty_layout = QVBoxLayout(self._empty_container)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(SprigAccent(size=56), alignment=Qt.AlignmentFlag.AlignHCenter)
        self._empty_label = QLabel(EMPTY_CLIPS_MESSAGE, self._empty_container)
        self._empty_label.setObjectName("hint")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_label)
        outer.addWidget(self._empty_container)
        self._empty_container.hide()

        # The two views, stacked: page 0 is the list, page 1 the grid. Both
        # are driven by the same _reapply_view pass, so the view state reads
        # identically whichever page is showing.
        self._view_stack = QStackedWidget(self)
        outer.addWidget(self._view_stack, 1)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._view_stack.addWidget(self._scroll_area)

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()  # keeps rows top-aligned as they're added/removed
        self._scroll_area.setWidget(self._list_container)
        # QScrollArea.setWidget flips the container's autoFillBackground ON,
        # filling it with the UNTHEMED palette Window grey (in BOTH modes --
        # the "rogue dark background" report). Turn it back off: the rows
        # (ClipRow cards) paint their own surfaces.
        self._list_container.setAutoFillBackground(False)

        self._grid_scroll_area = QScrollArea()
        self._grid_scroll_area.setWidgetResizable(True)
        self._view_stack.addWidget(self._grid_scroll_area)

        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(_GRID_SPACING)
        self._grid_scroll_area.setWidget(self._grid_container)
        # Same autoFillBackground rule as the list container above.
        self._grid_container.setAutoFillBackground(False)
        # A viewport resize changes how many card columns fit -- re-wrap the
        # visible cards (same order) whenever it changes.
        self._grid_scroll_area.viewport().installEventFilter(self)

        self.footer_label = QLabel(self)
        self.footer_label.setObjectName("hint")
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        outer.addWidget(self.footer_label)

        self.refresh()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 -- Qt's naming
        if watched is self._grid_scroll_area.viewport() and event.type() == QEvent.Type.Resize:
            self._reflow_grid()
        return super().eventFilter(watched, event)

    def refresh(self) -> None:
        self._stop_worker.set()  # stop any in-flight worker from a previous refresh
        for face in list(self._rows.values()) + list(self._cards.values()):
            face.setParent(None)
            face.deleteLater()
        self._rows.clear()
        self._cards.clear()
        self._grid_order = []
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)
        self._empty_container.hide()

        # Read the provider once per refresh: the folder can't meaningfully
        # change mid-pass, and a Settings change is picked up on the next one.
        clips_dir = self._clips_dir_provider()
        clip_paths = clips_newest_first(clips_dir)
        # One sidecar read per refresh, never per row: favorites feed the
        # star states and the favorites-first ordering, and prune drops
        # entries orphaned by renames/deletes (sidecar keys are full
        # filenames, so those never reattach to a different clip).
        self._favorites = clip_metadata.favorites(clips_dir)
        # Notes ride along on the same once-per-refresh sidecar read (never
        # per row): they feed the face tooltips. prune below drops entries
        # orphaned by renames/deletes, so stale notes can't reattach.
        self._notes = {
            name: entry.get("note", "")
            for name, entry in clip_metadata.load_metadata(clips_dir).items()
            if entry.get("note")
        }
        clip_metadata.prune(clips_dir, {p.name for p in clip_paths})
        # The selection survives a refresh only for clips that still exist.
        self._selected.intersection_update(clip_paths)
        self._rebuild_window_filter(clip_paths)
        if not clip_paths:
            self._update_empty_state()
            self._update_footer()
            self._update_selection_ui()
            return

        for clip_path in clip_paths:
            self._add_clip(clip_path)

        thumbnails.cleanup_orphaned_thumbnails(
            clips_dir / thumbnails.THUMBNAIL_DIR_NAME, {p.stem for p in clip_paths}
        )

        self._reapply_view()
        self._update_footer()
        self._update_selection_ui()

        self._stop_worker = threading.Event()
        worker = ThumbnailWorker(
            self._ffmpeg_path,
            clip_paths,
            clips_dir / thumbnails.THUMBNAIL_DIR_NAME,
            self._stop_worker,
            # None when ffprobe is genuinely unavailable -- the worker then
            # skips durations entirely and the meta lines keep their
            # two-part shape.
            ffprobe_path=thumbnails.find_ffprobe(self._ffmpeg_path),
        )
        worker.ready.connect(self._apply_thumbnail)
        worker.duration_ready.connect(self._apply_duration)
        self._worker = worker  # kept alive for the duration of the background thread
        threading.Thread(target=worker.run, daemon=True).start()

    def _add_clip(self, clip_path: Path) -> None:
        try:
            stat_result = clip_path.stat()
        except OSError:
            return
        # Both faces are built up front; the view switch only decides which
        # container is showing, so toggling views never rebuilds anything or
        # loses thumbnail/duration/selection state.
        row = ClipRow(clip_path, stat_result, self._list_container)
        card = ClipCard(clip_path, stat_result, self._grid_container)
        for face in (row, card):
            face.favorite_button.toggled.connect(
                lambda checked, p=clip_path: self._set_favorite(p, checked)
            )
            face.select_checkbox.toggled.connect(
                lambda checked, p=clip_path: self._on_face_selection_toggled(p, checked)
            )
            face.customContextMenuRequested.connect(
                lambda pos, p=clip_path, f=face: self._show_context_menu(p, f.mapToGlobal(pos))
            )
            # The "⋯" button pops the same menu a right-click would, anchored
            # to the button's bottom-left so it reads as the button's own popup.
            face.menu_button.clicked.connect(
                lambda _checked=False, p=clip_path, f=face: self._show_context_menu(
                    p, f.menu_button.mapToGlobal(f.menu_button.rect().bottomLeft())
                )
            )
            face.double_clicked.connect(lambda p=clip_path: self._play_clip(p))
            face.set_favorite(clip_path.name in self._favorites)
            face.set_selection_visible(self._selection_mode)
            face.select_checkbox.setChecked(clip_path in self._selected)
        row.play_button.clicked.connect(lambda: self._play_clip(clip_path))
        row.setToolTip(_elide_note(self._notes.get(clip_path.name, "")))
        card.setToolTip(self._card_tooltip(clip_path))
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)  # before the trailing stretch
        self._rows[clip_path] = row
        self._cards[clip_path] = card
        # Cards enter the grid layout in _reapply_view's reflow (order +
        # filters decide placement); until then a fresh card stays out of it.
        card.hide()

    def _card_tooltip(self, clip_path: Path) -> str:
        """The card's name label is middle-elided at 176px, so its tooltip
        always carries the full filename; a saved note rides along under it
        (the row's tooltip shows the note alone -- its name has room)."""
        note = _elide_note(self._notes.get(clip_path.name, ""))
        if note:
            return f"{clip_path.name}\n{note}"
        return clip_path.name

    # ---- header controls: search / sort / favorites-first ------------------

    # ---- view controls: view switch / search / window filter / sort / favorites-first

    def _on_view_changed(self, text: str) -> None:
        self._view_mode = "grid" if text == "Grid" else "list"
        self._view_stack.setCurrentIndex(1 if self._view_mode == "grid" else 0)
        if self._view_mode == "grid":
            # The column count depends on the viewport width, which is only
            # meaningful once the grid page is actually showing (its resize
            # event reflows too -- this covers the already-right-size case).
            self._reflow_grid()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip().lower()
        self._reapply_view()

    def _on_window_filter_changed(self, _index: int) -> None:
        self._window_filter = self.window_filter_combo.currentData()
        self._reapply_view()

    def _on_sort_changed(self, _index: int) -> None:
        self._sort_mode = self.sort_combo.currentData()
        self._reapply_view()

    def _on_favorites_first_changed(self, checked: bool) -> None:
        self._favorites_first = checked
        self._reapply_view()

    def _rebuild_window_filter(self, clip_paths: list[Path]) -> None:
        """Repopulate the window filter from the clips refresh() just
        enumerated: "All windows", then each distinct parsed window name
        (case-insensitive sort) with its clip count, then "Other" collecting
        the clips whose names the template parser can't read. The previous
        selection is restored when it still exists, otherwise the filter
        falls back to All. Signals are blocked so the rebuild never fires a
        spurious re-filter -- _window_filter is set directly here, and the
        refresh's own _reapply_view pass applies it.
        """
        counts: dict[str | None, int] = {}
        for clip_path in clip_paths:
            window = window_name_from_clip_name(clip_path.name)
            counts[window] = counts.get(window, 0) + 1
        entries: list[tuple[str, str | None]] = [("All windows", WINDOW_FILTER_ALL)]
        for name in sorted((w for w in counts if w is not None), key=str.lower):
            entries.append((f"{name} ({counts[name]})", name))
        if None in counts:
            entries.append((f"Other ({counts[None]})", None))

        combo = self.window_filter_combo
        previous = self._window_filter
        combo.blockSignals(True)
        combo.clear()
        values: list[str | None] = []
        for label, value in entries:
            combo.addItem(label, userData=value)
            values.append(value)
        try:
            index = values.index(previous)
        except ValueError:
            index = 0
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._window_filter = values[index]

    def _window_matches(self, clip_path: Path) -> bool:
        """The window filter's half of a face's visibility: everything on
        "All windows"; otherwise the clip's parsed window name must equal the
        filter value (None -- the "Other" entry -- matches exactly the clips
        whose names the parser can't read)."""
        if self._window_filter == WINDOW_FILTER_ALL:
            return True
        return window_name_from_clip_name(clip_path.name) == self._window_filter

    def _reapply_view(self) -> None:
        """Re-apply the current search/window-filter/sort/favorites-first
        state to the faces refresh() already built -- hiding and re-ordering
        in place rather than rebuilding, so thumbnails, durations, and
        checkbox states survive every keystroke in the search box, and no
        stat call ever happens outside _add_clip's vanish-tolerant one. Both
        views are driven by the same ordered pass: the list re-inserts its
        rows, the grid re-places only the visible cards in the same order.
        """
        for clip_path, row in self._rows.items():
            visible = self._search_text in clip_path.name.lower() and self._window_matches(clip_path)
            row.setVisible(visible)
            card = self._cards.get(clip_path)
            if card is not None:
                card.setVisible(visible)

        def sort_key(item: tuple[Path, ClipRow]):
            clip_path, row = item
            # Favorites-first ranks ABOVE the chosen sort, not instead of
            # it: within each rank the active sort still applies.
            favorite_rank = 0 if (self._favorites_first and clip_path.name in self._favorites) else 1
            if self._sort_mode == SORT_OLDEST:
                primary = row.mtime
            elif self._sort_mode == SORT_NAME:
                primary = clip_path.name.lower()
            elif self._sort_mode == SORT_LARGEST:
                primary = -row.size_bytes
            elif self._sort_mode == SORT_WINDOW:
                # By parsed window name (case-insensitive), newest first
                # within a window; names the parser can't read ("Other")
                # sink to the end.
                window = window_name_from_clip_name(clip_path.name)
                primary = (window is None, (window or "").lower(), -row.mtime)
            else:  # SORT_NEWEST -- the same order clips_newest_first enumerated
                primary = -row.mtime
            return (favorite_rank, primary)

        ordered_rows = [row for _, row in sorted(self._rows.items(), key=sort_key)]
        for row in ordered_rows:
            self._list_layout.removeWidget(row)
        for row in ordered_rows:
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)  # before the trailing stretch

        self._grid_order = [self._cards[row.clip_path] for row in ordered_rows if not row.isHidden()]
        self._reflow_grid()
        self._update_empty_state()

    def _reflow_grid(self) -> None:
        """Place the visible cards (_grid_order) into the grid, wrapping at
        as many card-width columns as the viewport currently fits. Runs on
        every view-state change and on viewport resizes. Cards not in
        _grid_order stay OUT of the layout entirely: a hidden widget would
        keep its grid cell and leave a hole.
        """
        layout = self._grid_layout
        while layout.count():
            layout.takeAt(0)
        # Reset the previous pass's stretch markers (tracked explicitly --
        # QGridLayout doesn't reliably shrink rowCount/columnCount after a
        # clear, so re-reading them here could miss a stale stretch row).
        if self._grid_stretch_row is not None:
            layout.setRowStretch(self._grid_stretch_row, 0)
            self._grid_stretch_row = None
        if self._grid_stretch_column is not None:
            layout.setColumnStretch(self._grid_stretch_column, 0)
            self._grid_stretch_column = None
        if not self._grid_order:
            return
        width = self._grid_scroll_area.viewport().width()
        columns = max(1, int((width + _GRID_SPACING) // (_GRID_CARD_WIDTH + _GRID_SPACING)))
        for index, card in enumerate(self._grid_order):
            layout.addWidget(card, index // columns, index % columns)
        # Leftover space collects in an empty trailing row/column instead of
        # spreading between the cards (the all-zero-stretch default would
        # distribute it BETWEEN the rows and break the grid's even rhythm).
        self._grid_stretch_row = (len(self._grid_order) - 1) // columns + 1
        layout.setRowStretch(self._grid_stretch_row, 1)
        self._grid_stretch_column = columns
        layout.setColumnStretch(self._grid_stretch_column, 1)

    def _update_empty_state(self) -> None:
        if not self._rows:
            self._empty_label.setText(EMPTY_CLIPS_MESSAGE)
            self._empty_container.show()
        elif all(row.isHidden() for row in self._rows.values()):
            # Clips exist but the search/filter hides every one -- a
            # different message from the genuinely-empty folder, or the user
            # thinks their clips are gone.
            self._empty_label.setText("No clips match your search/filter.")
            self._empty_container.show()
        else:
            self._empty_container.hide()

    def _update_footer(self) -> None:
        count = len(self._rows)
        total_bytes = sum(row.size_bytes for row in self._rows.values())
        favorites_count = sum(1 for clip_path in self._rows if clip_path.name in self._favorites)
        noun = "clip" if count == 1 else "clips"
        favorite_noun = "favorite" if favorites_count == 1 else "favorites"
        self.footer_label.setText(
            f"{count} {noun}  ·  {_format_size(total_bytes)}  ·  {favorites_count} {favorite_noun}"
        )

    def _do_rename(self, clip_path: Path) -> None:
        new_stem, ok = QInputDialog.getText(self, "Rename clip", f"New name for {clip_path.name}:", text=clip_path.stem)
        if not ok or not new_stem:
            return
        # A name containing a path separator makes with_name() raise an
        # uncaught ValueError ("Invalid name") -- and a clip can never
        # legitimately live outside clips_dir, so reject it as a bad name.
        if "/" in new_stem or "\\" in new_stem:
            quiet_message(self, "Rename clip", f"A clip name cannot contain path separators: {new_stem!r}")
            return
        new_path = clip_path.with_name(f"{new_stem}{clip_path.suffix}")
        if new_path == clip_path:
            return  # unchanged name -- nothing to do, and not an overwrite
        # Path.rename() silently REPLACES an existing destination on POSIX;
        # only Windows raises FileExistsError. Refuse up front on every
        # platform rather than destroy another clip without any prompt.
        if new_path.exists():
            quiet_message(self, "Rename clip", f"A clip named {new_path.name} already exists.")
            return
        try:
            clip_path.rename(new_path)
        except OSError as exc:
            log.warning("Could not rename %s to %s: %s", clip_path, new_path, exc)
            return
        self.refresh()
        self.clips_changed.emit()

    # ---- play in-app (0.1.4) ---------------------------------------------------

    def _play_clip(self, clip_path: Path) -> None:
        """Row Play button / double-click / context-menu "Play" and "Trim...":
        the in-app player when QtMultimedia is importable, the OS default
        player otherwise. Construction + the fallback live in
        player_qt.play_clip, shared with the main window's recent-clips
        strip; this method only keeps the modal-less dialog referenced."""
        dialog = player_qt.play_clip(
            self,
            clip_path,
            self._ffmpeg_path,
            on_trim_exported=self._on_player_trim_exported,
        )
        if dialog is not None:
            self._players.append(dialog)
            dialog.destroyed.connect(lambda: self._discard_player(dialog))

    def _on_player_trim_exported(self, _path) -> None:
        # The player's trim card wrote a new -trimmed.mp4: show it, and tell
        # the main window's recent strip the clip set changed.
        self.refresh()
        self.clips_changed.emit()

    def _discard_player(self, dialog) -> None:
        try:
            self._players.remove(dialog)
        except ValueError:
            pass

    # ---- details / notes --------------------------------------------------------

    def _do_details(self, clip_path: Path) -> None:
        clips_dir = self._clips_dir_provider()
        dialog = ClipDetailsDialog(
            self._ffmpeg_path,
            clip_path,
            clips_dir,
            favorite=clip_path.name in self._favorites,
            parent=self,
        )
        # Live sync through the single write path (same as the row's star) --
        # a favorite change applies immediately, not on Save.
        dialog.favorite_switch.toggled.connect(lambda checked: self._set_favorite(clip_path, checked))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # The dialog already persisted the note on accept; mirror it into the
        # per-refresh cache and the faces' tooltips without a full refresh.
        note = clip_metadata.note_for(clips_dir, clip_path.name)
        if note:
            self._notes[clip_path.name] = note
        else:
            self._notes.pop(clip_path.name, None)
        row = self._rows.get(clip_path)
        if row is not None:
            row.setToolTip(_elide_note(note))
        card = self._cards.get(clip_path)
        if card is not None:
            card.setToolTip(self._card_tooltip(clip_path))

    # ---- exports (GIF / compress) -------------------------------------------------

    def _resolve_encoder(self) -> str:
        """The current capture encoder for a compress export -- never-raising:
        an absent or broken provider degrades to libx264, which ffmpeg always
        has (the provider reads state.setup.encoder, which apply_settings can
        swap mid-session)."""
        if self._encoder_provider is None:
            return "libx264"
        try:
            return self._encoder_provider() or "libx264"
        except Exception:  # noqa: BLE001 -- best-effort provider, degrade quietly
            return "libx264"

    def _do_export_gif(self, clip_path: Path) -> None:
        dialog = GifExportDialog(self._ffmpeg_path, clip_path, self._clips_dir_provider(), self)
        # No export_succeeded handling needed: the dialog reports success
        # inline and GIFs never appear in the gallery (not .mp4), so there's
        # nothing to refresh.
        dialog.exec()

    def _do_compress(self, clip_path: Path) -> None:
        dialog = CompressDialog(
            self._ffmpeg_path, self._resolve_encoder(), clip_path, self._clips_dir_provider(), self
        )
        dialog.export_succeeded.connect(self._on_compress_succeeded)
        dialog.exec()

    def _on_compress_succeeded(self, output_path: Path) -> None:
        self.refresh()  # the new -compressed.mp4 should appear immediately
        self.clips_changed.emit()

    def _do_delete(self, clip_path: Path) -> None:
        reply = quiet_message(
            self,
            "Delete clip",
            f"Delete {clip_path.name}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            clip_path.unlink()
        except OSError as exc:
            log.warning("Could not delete %s: %s", clip_path, exc)
            return
        self._selected.discard(clip_path)
        self._remove_clip(clip_path)
        self._update_selection_ui()
        self._update_empty_state()
        self._update_footer()
        self.clips_changed.emit()

    def _remove_clip(self, clip_path: Path) -> None:
        for face in (self._rows.pop(clip_path, None), self._cards.pop(clip_path, None)):
            if face is not None:
                face.setParent(None)
                face.deleteLater()
        # The deleted clip's card would otherwise sit in the grid order (and
        # get re-placed by the next reflow) until something triggers a full
        # _reapply_view -- drop it and re-lay the grid right away.
        self._grid_order = [card for card in self._grid_order if card.clip_path != clip_path]
        self._reflow_grid()

    # ---- selection mode + batch delete --------------------------------------

    def _set_selection_mode(self, enabled: bool) -> None:
        self._selection_mode = enabled
        self.select_button.setText("Done" if enabled else "Select")
        self._selection_bar.setVisible(enabled)
        if not enabled:
            # Leaving selection mode drops the selection with it -- no
            # half-armed Delete button lurking behind the Done toggle.
            self._selected.clear()
        for face in list(self._rows.values()) + list(self._cards.values()):
            face.set_selection_visible(enabled)
            if not enabled and face.select_checkbox.isChecked():
                # Emits toggled(False) -> the handler discards from the
                # already-cleared set; harmless, and keeps box state honest.
                face.select_checkbox.setChecked(False)
        self._update_selection_ui()

    def _on_face_selection_toggled(self, clip_path: Path, checked: bool) -> None:
        if checked:
            self._selected.add(clip_path)
        else:
            self._selected.discard(clip_path)
        # Mirror the new state onto the clip's OTHER face (silently -- it
        # must not re-enter this handler), so the selection reads the same
        # after a view switch no matter which face was toggled.
        for face in (self._rows.get(clip_path), self._cards.get(clip_path)):
            if face is not None and face.select_checkbox.isChecked() != checked:
                face.select_checkbox.blockSignals(True)
                face.select_checkbox.setChecked(checked)
                face.select_checkbox.blockSignals(False)
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        count = len(self._selected)
        self.delete_selected_button.setText(f"Delete selected ({count})")
        self.delete_selected_button.setEnabled(count > 0)

    def _select_all_visible(self) -> None:
        # Only rows the user can currently see: a search-filtered clip must
        # never be swept into a batch delete invisibly.
        for row in self._rows.values():
            if not row.isHidden():
                row.select_checkbox.setChecked(True)

    def _select_none(self) -> None:
        for row in self._rows.values():
            row.select_checkbox.setChecked(False)
        # Paths whose row vanished mid-session have no checkbox to emit for
        # them -- clear the set itself too.
        self._selected.clear()
        self._update_selection_ui()

    def _do_delete_selected(self) -> None:
        paths = [clip_path for clip_path in self._rows if clip_path in self._selected]
        if not paths:
            return
        noun = "clip" if len(paths) == 1 else "clips"
        reply = quiet_message(
            self,
            "Delete clips",
            f"Delete {len(paths)} selected {noun}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for clip_path in paths:
            try:
                clip_path.unlink()
            except OSError as exc:
                # Same rule as _do_delete: a vanished/locked file is logged
                # and skipped (its row stays for the next refresh), it never
                # aborts the rest of the batch.
                log.warning("Could not delete %s: %s", clip_path, exc)
                continue
            self._selected.discard(clip_path)
            self._remove_clip(clip_path)
        self._update_selection_ui()
        self._update_empty_state()
        self._update_footer()
        self.clips_changed.emit()

    # ---- favorites -----------------------------------------------------------

    def _set_favorite(self, clip_path: Path, favorite: bool) -> None:
        """The single write path behind both faces' hearts and the context
        menu's Favorite action: persist to the sidecar, sync the in-memory
        set, sync both faces' hearts, then float/sink the clip if
        favorites-first is active."""
        clip_metadata.set_favorite(self._clips_dir_provider(), clip_path.name, favorite)
        if favorite:
            self._favorites.add(clip_path.name)
        else:
            self._favorites.discard(clip_path.name)
        for face in (self._rows.get(clip_path), self._cards.get(clip_path)):
            if face is not None:
                face.set_favorite(favorite)  # no signal loop -- set_favorite blocks toggled
        self._update_footer()  # the footer carries the favorites count
        if self._favorites_first:
            self._reapply_view()

    # ---- context menu ---------------------------------------------------------

    def _build_context_menu(self, clip_path: Path) -> QMenu:
        """The face's right-click menu -- also what the "⋯" button pops. It
        holds every per-clip action now that the faces themselves only show
        Play (list row) + the heart + "⋯". Themed by the global stylesheet's
        QMenu rule like every other menu in the app. "Play" is first: it's
        the double-click behavior, so it's the menu's default action too.
        "Trim..." opens the in-app player -- its trim card does the exporting.
        """
        menu = QMenu(self)
        play_action = menu.addAction("Play")
        play_action.triggered.connect(lambda: self._play_clip(clip_path))
        open_action = menu.addAction("Open")
        open_action.triggered.connect(lambda: open_folder(clip_path))
        reveal_action = menu.addAction("Reveal in folder")
        reveal_action.triggered.connect(lambda: reveal_in_file_manager(clip_path))
        menu.addSeparator()
        favorite_action = menu.addAction("Favorite")
        favorite_action.setCheckable(True)
        favorite_action.setChecked(clip_path.name in self._favorites)
        favorite_action.triggered.connect(lambda checked: self._set_favorite(clip_path, checked))
        details_action = menu.addAction("Details…")
        details_action.triggered.connect(lambda: self._do_details(clip_path))
        menu.addSeparator()
        rename_action = menu.addAction("Rename…")
        rename_action.triggered.connect(lambda: self._do_rename(clip_path))
        trim_action = menu.addAction("Trim…")
        trim_action.triggered.connect(lambda: self._play_clip(clip_path))
        gif_action = menu.addAction("Export as GIF…")
        gif_action.triggered.connect(lambda: self._do_export_gif(clip_path))
        compress_action = menu.addAction("Compress…")
        compress_action.triggered.connect(lambda: self._do_compress(clip_path))
        copy_action = menu.addAction("Copy path")
        copy_action.triggered.connect(lambda: QGuiApplication.clipboard().setText(str(clip_path)))
        # Stem only (no extension): the shareable name, e.g. for pasting next
        # to the clip in a chat.
        copy_name_action = menu.addAction("Copy filename")
        copy_name_action.triggered.connect(lambda: QGuiApplication.clipboard().setText(clip_path.stem))
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self._do_delete(clip_path))
        return menu

    def _show_context_menu(self, clip_path: Path, global_pos) -> None:
        menu = self._build_context_menu(clip_path)
        menu.exec(global_pos)
        menu.deleteLater()

    # ---- async deliveries ------------------------------------------------------

    def _apply_duration(self, clip_path: Path, seconds: float | None) -> None:
        if seconds is None:
            return  # unprobeable clip -- the meta line keeps its two-part shape
        for face in (self._rows.get(clip_path), self._cards.get(clip_path)):
            if face is not None:
                face.set_duration(seconds)

    def _apply_thumbnail(self, clip_path: Path, thumbnail_path: Path | None) -> None:
        if thumbnail_path is None:
            return
        pixmap = QPixmap(str(thumbnail_path))
        if pixmap.isNull():
            return
        row = self._rows.get(clip_path)
        if row is not None:
            row.set_thumbnail(
                pixmap.scaled(
                    _THUMBNAIL_WIDTH,
                    _THUMBNAIL_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        card = self._cards.get(clip_path)
        if card is not None:
            card.set_thumbnail(
                pixmap.scaled(
                    _GRID_THUMBNAIL_WIDTH,
                    _GRID_THUMBNAIL_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


def build_gallery_frame(
    parent: QWidget | None,
    ffmpeg_path: str,
    clips_dir_provider: Callable[[], Path],
    encoder_provider: Callable[[], str] | None = None,
) -> GalleryFrame:
    """Thin factory function kept for API parity with the CustomTkinter
    original's `build_gallery_frame(parent, ffmpeg_path, clips_dir)` shape --
    the third argument is now a live clips-dir provider, see GalleryFrame.
    `encoder_provider` is a live read of state.setup.encoder for the
    Compress dialog (None -> libx264).
    """
    return GalleryFrame(ffmpeg_path, clips_dir_provider, parent, encoder_provider=encoder_provider)
