"""Save-notification toast.

The one place in the app that deliberately uses a frameless window -- the
main window needs native minimize/close/drag, which frameless breaks, but a
toast is supposed to be chromeless by nature.

The thumbnail is fetched in a background thread, same reasoning as before
(an ffmpeg frame-grab takes ~0.3-0.5s -- blocking the GUI thread for that
would freeze the whole app). Unlike the CustomTkinter version, delivery back
to the GUI thread is a real Qt signal (`_ThumbnailFetcher.ready`, connected
with the automatic cross-thread QueuedConnection) instead of a
queue.Queue + window.after() poll -- this is the first real use of the
signal-bridge pattern from signals.py's docstring, applied here at the
smallest possible scale before the bigger tabs need it too.

**Bloom entrance animation**: the
toast no longer just appears at full size -- it expands outward from a small
point at its final position's center, plus a fade-in, so it reads as a bud
opening rather than a flat popup. `QEasingCurve.OutBack` deliberately
overshoots slightly past full size before settling back, the same
"blooms a little past its final size, then relaxes" motion a flower or a
scattering seed pod actually has, not just a plain linear grow. The
animation is built in `__init__` (so the starting geometry/opacity are set
before the window is ever shown) but only *started* from
`show_save_toast()`, after `.show()` -- animating a widget's geometry before
it's shown has no visible effect to animate.

**Actions row, stacking, meta line** (0.1.3): each toast carries "Open"
(play with the OS default app) and "Show in folder" buttons -- child
QPushButtons consume their own mouse presses, so a button click never also
triggers the whole-toast click behavior. Toasts stack upward: the module
level `_live_toasts` list tracks the open ones, each new toast's final
position offsets up by (height + gap) per already-open toast, and a close
re-flows the survivors down into the gap. A meta line under the filename
shows "duration · size" probed on the same background thread as the
thumbnail (an ffprobe call + an os.stat -- never the GUI thread, and the
caller deliberately does NOT pre-probe: cli.py just hands over the path).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from clipersal import thumbnails
from clipersal.gallery_window_qt import reveal_in_file_manager
from clipersal.tray import open_file, open_folder

log = logging.getLogger(__name__)

_TOAST_WIDTH = 300
_THUMBNAIL_WIDTH = 90
_THUMBNAIL_HEIGHT = _THUMBNAIL_WIDTH * 9 // 16
_DISPLAY_MS = 4500
_MARGIN = 20
_STACK_GAP = 12
_ENTRANCE_GEOMETRY_MS = 320
_ENTRANCE_OPACITY_MS = 220

# The open toasts, oldest first (index 0 = bottom of the stack). A toast is
# appended by show_save_toast() and removed via its destroyed signal -- see
# _on_toast_destroyed / _reflow_toasts. Module-level because stacking is a
# property of the toast population, not of any one toast.
_live_toasts: list["SaveToast"] = []


def _format_duration(seconds: float) -> str:
    """M:SS for the meta line ("1:05") -- whole seconds are plenty on a toast."""
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}:{secs:02d}"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class _ThumbnailFetcher(QObject):
    """Constructed on the GUI thread, `.fetch()` run on a background thread
    -- `ready.emit(...)` from that thread is automatically delivered to
    whatever's connected on the GUI thread via Qt.QueuedConnection.

    Also probes the meta line (duration via ffprobe, size via os.stat) on
    the same thread -- both are subprocess/syscall work that must stay off
    the GUI thread, and the caller (cli.py) deliberately hands over just the
    path instead of pre-probing. `include_thumbnail=False` (a screenshot
    toast, which shows the PNG itself directly) skips the ffmpeg frame-grab
    but still probes the meta.
    """

    ready = Signal(object)  # Path | None
    meta_ready = Signal(str)  # "1:05 · 12.4 MB", or "" when nothing is known

    def __init__(self, ffmpeg_path: str, clip_path: Path, cache_dir: Path, include_thumbnail: bool = True) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._cache_dir = cache_dir
        self._include_thumbnail = include_thumbnail

    def fetch(self) -> None:
        if self._include_thumbnail:
            thumb = thumbnails.ensure_thumbnail(self._ffmpeg_path, self._clip_path, self._cache_dir)
            self.ready.emit(thumb)
        self.meta_ready.emit(self._probe_meta())

    def _probe_meta(self) -> str:
        # Each part degrades independently: no ffprobe (or a probe failure)
        # just omits the duration, a vanishing clip just omits the size --
        # never an exception out of a cosmetic label.
        parts: list[str] = []
        try:
            ffprobe = thumbnails.find_ffprobe(self._ffmpeg_path)
        except Exception as exc:  # noqa: BLE001 -- omit the duration, keep the size
            log.debug("ffprobe discovery for the toast meta line raised: %s", exc)
            ffprobe = None
        if ffprobe:
            duration = thumbnails.get_duration_seconds(ffprobe, self._clip_path)
            if duration is not None:
                parts.append(_format_duration(duration))
        try:
            parts.append(_format_size(self._clip_path.stat().st_size))
        except OSError:
            pass
        return " · ".join(parts)


class SaveToast(QWidget):
    def __init__(
        self,
        ffmpeg_path: str,
        clip_path: Path,
        cache_dir: Path,
        parent: QWidget | None = None,
        title: str = "Clip saved",
    ) -> None:
        # A real QObject parent keeps this top-level window's C++ object (and
        # its PySide6 wrapper) alive for as long as the parent lives, even
        # though the window flags below make it render independent of the
        # parent's own geometry -- without this, nothing else holds a Python
        # reference to the toast once show_save_toast() returns, and it could
        # be garbage-collected out from under its own auto-dismiss timer.
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self._clip_path = clip_path
        self._closed = False
        # The window itself stays transparent; only the inner #card QFrame
        # (below) is opaque and rounded, so the rounded corners actually show
        # against the desktop instead of leaving square artifacts.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # close() (auto-dismiss or click) only HIDES a parented widget --
        # without this, every save leaves a permanent hidden child (animation
        # group, thumbnail fetcher and all) on the persistent MainWindow.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("card")
        outer.addWidget(card)

        content = QHBoxLayout(card)
        content.setContentsMargins(12, 12, 12, 12)
        content.setSpacing(10)

        self._thumb_label = QLabel(card)
        self._thumb_label.setObjectName("thumbPlaceholder")
        self._thumb_label.setFixedSize(_THUMBNAIL_WIDTH, _THUMBNAIL_HEIGHT)
        content.addWidget(self._thumb_label)

        text_col = QVBoxLayout()
        content.addLayout(text_col, 1)

        title_label = QLabel(title, card)
        title_label.setObjectName("toastTitle")
        text_col.addWidget(title_label)

        name_label = QLabel(clip_path.name, card)
        name_label.setWordWrap(True)
        text_col.addWidget(name_label)

        # Filled in by _on_meta_ready once the fetcher thread has probed the
        # duration/size -- stays empty (invisible) until then.
        self._meta_label = QLabel("", card)
        self._meta_label.setObjectName("hint")
        text_col.addWidget(self._meta_label)

        hint_label = QLabel("Click to open folder", card)
        hint_label.setObjectName("hint")
        text_col.addWidget(hint_label)

        # Child buttons consume their own mouse presses, so clicking one
        # never also fires the whole-toast mousePressEvent below (no
        # double-action); neither button closes the toast -- the
        # auto-dismiss timer still owns its lifetime.
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        self._open_button = QPushButton("Open", card)
        self._open_button.clicked.connect(self._on_open_clicked)
        buttons_row.addWidget(self._open_button)
        self._reveal_button = QPushButton("Show in folder", card)
        self._reveal_button.clicked.connect(self._on_reveal_clicked)
        buttons_row.addWidget(self._reveal_button)
        buttons_row.addStretch()
        text_col.addLayout(buttons_row)

        self.setFixedWidth(_TOAST_WIDTH)
        self.adjustSize()
        # How many toasts are already open -- this one stacks above them.
        # Captured before _final_geometry() reads it; show_save_toast()
        # appends this toast to _live_toasts only after construction.
        self._stack_index = len(_live_toasts)
        # Captured now, while width/height still reflect the fully laid-out
        # toast -- _build_entrance_animation() below immediately shrinks the
        # widget for the entrance animation, after which self.width()/
        # height() no longer describe the final size _final_geometry()
        # computes from, so this can't be recomputed later.
        self._final_rect = self._final_geometry()
        self._entrance_animation = self._build_entrance_animation()

        if clip_path.suffix.lower() == ".png":
            # The "thumbnail" of a screenshot IS the file itself -- loading
            # it directly beats spawning ffmpeg to re-grab a frame out of a
            # still image (which would just fail into the placeholder). The
            # fetcher still runs for the meta line (its size), just without
            # the frame-grab half.
            self._on_thumbnail_ready(clip_path)
            include_thumbnail = False
        else:
            include_thumbnail = True
        self._fetcher = _ThumbnailFetcher(ffmpeg_path, clip_path, cache_dir, include_thumbnail=include_thumbnail)
        self._fetcher.ready.connect(self._on_thumbnail_ready)
        self._fetcher.meta_ready.connect(self._on_meta_ready)
        threading.Thread(target=self._fetcher.fetch, daemon=True).start()

        # The context overload ties the timer to this toast's lifetime: once
        # WA_DeleteOnClose has destroyed the toast (e.g. an early click), the
        # pending singleShot must never fire close() on a dead C++ object.
        QTimer.singleShot(_DISPLAY_MS, self, self.close)

    def _final_geometry(self) -> QRect:
        # availableGeometry() natively excludes the taskbar, unlike the old
        # Tk version's manual "_MARGIN * 3" extra-headroom guess.
        screen_rect = QGuiApplication.primaryScreen().availableGeometry()
        x = screen_rect.right() - self.width() - _MARGIN
        y = screen_rect.bottom() - self.height() - _MARGIN
        # Stacking: each already-open toast pushes this one up by one
        # toast-height plus the inter-toast gap.
        y -= (self.height() + _STACK_GAP) * self._stack_index
        return QRect(x, y, self.width(), self.height())

    def _build_entrance_animation(self) -> QParallelAnimationGroup:
        final_rect = self._final_rect
        center = final_rect.center()
        start_rect = QRect(center.x() - 2, center.y() - 2, 4, 4)

        # setFixedWidth() in __init__ pinned minimumSize == maximumSize, which
        # would silently clamp every frame of the geometry animation straight
        # back to full size (Qt enforces min/max on every setGeometry call,
        # not just the first). Lift the constraint for the animation's
        # duration and re-pin it once the toast settles into final_rect, so
        # later content changes (e.g. the thumbnail arriving) can't grow it.
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        # Set the *starting* state now, before the window is ever shown --
        # start_entrance_animation() (called after .show()) just plays the
        # transition from here to final_rect/opacity 1.0.
        self.setGeometry(start_rect)
        self.setWindowOpacity(0.0)

        geometry_anim = QPropertyAnimation(self, b"geometry", self)
        geometry_anim.setDuration(_ENTRANCE_GEOMETRY_MS)
        geometry_anim.setStartValue(start_rect)
        geometry_anim.setEndValue(final_rect)
        geometry_anim.setEasingCurve(QEasingCurve.Type.OutBack)

        opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        opacity_anim.setDuration(_ENTRANCE_OPACITY_MS)
        opacity_anim.setStartValue(0.0)
        opacity_anim.setEndValue(1.0)

        group = QParallelAnimationGroup(self)
        group.addAnimation(geometry_anim)
        group.addAnimation(opacity_anim)
        group.finished.connect(lambda: self.setFixedSize(final_rect.size()))
        return group

    def start_entrance_animation(self) -> None:
        """Called by show_save_toast() after .show() -- animating geometry
        on a widget that isn't shown yet has nothing to animate.
        """
        self._entrance_animation.start()

    def _on_thumbnail_ready(self, thumb_path: Path | None) -> None:
        if thumb_path is None or self._closed:
            return
        pixmap = QPixmap(str(thumb_path))
        if pixmap.isNull():
            return
        scaled = pixmap.scaled(
            _THUMBNAIL_WIDTH,
            _THUMBNAIL_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb_label.setPixmap(scaled)

    def _on_meta_ready(self, meta: str) -> None:
        if self._closed or not meta:
            return
        self._meta_label.setText(meta)

    def _on_open_clicked(self) -> None:
        # Play with the OS default app -- open_file is already log-and-continue.
        open_file(self._clip_path)

    def _on_reveal_clicked(self) -> None:
        try:
            reveal_in_file_manager(self._clip_path)
        except Exception:  # noqa: BLE001 -- a cosmetic button must never crash the toast
            log.exception("Failed to reveal %s in the file manager", self._clip_path)

    def mousePressEvent(self, event) -> None:  # noqa: N802 -- Qt's own naming convention
        if event.button() == Qt.MouseButton.LeftButton:
            open_folder(self._clip_path.parent)
            self.close()
        else:
            super().mousePressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        super().closeEvent(event)


def _on_toast_destroyed(toast: "SaveToast") -> None:
    """Connected to each toast's destroyed signal (WA_DeleteOnClose means
    close() really destroys it): drop it from the live list and shift the
    survivors down to fill the gap.
    """
    try:
        _live_toasts.remove(toast)
    except ValueError:
        pass
    _reflow_toasts()


def _reflow_toasts() -> None:
    """Re-seat every open toast at its stack position after a close. A plain
    move(), not an animation -- the close itself is the visual event, the
    survivors just need to not leave a hole. Toasts still mid-entrance keep
    their own animation target (it converges on the next reflow). Never
    raises -- stacking is cosmetic.
    """
    try:
        screen_rect = QGuiApplication.primaryScreen().availableGeometry()
        for index, toast in enumerate(list(_live_toasts)):
            toast._stack_index = index
            if toast._entrance_animation.state() == QAbstractAnimation.State.Running:
                continue
            target_y = screen_rect.bottom() - _MARGIN - toast.height() - (toast.height() + _STACK_GAP) * index
            if toast.y() != target_y:
                toast.move(toast.x(), target_y)
    except Exception:  # noqa: BLE001 -- purely cosmetic, never fatal
        log.exception("Failed to reflow toasts")


def show_save_toast(
    parent: QWidget | None,
    ffmpeg_path: str,
    clip_path: Path,
    cache_dir: Path,
    title: str = "Clip saved",
) -> None:
    """Show a toast for a just-saved clip (or screenshot -- `title` swaps the
    heading to e.g. "Screenshot saved"). Never raises -- a toast is
    cosmetic, and a failure here must never take down the save it's
    celebrating.
    """
    try:
        toast = SaveToast(ffmpeg_path, clip_path, cache_dir, parent, title=title)
        _live_toasts.append(toast)
        toast.destroyed.connect(lambda _obj=None, t=toast: _on_toast_destroyed(t))
        toast.show()
        toast.start_entrance_animation()
    except Exception:  # noqa: BLE001 -- purely cosmetic, never fatal
        log.exception("Failed to show save toast for %s", clip_path)
