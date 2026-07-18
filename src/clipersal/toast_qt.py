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
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QParallelAnimationGroup, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from clipersal import thumbnails
from clipersal.tray import open_folder

log = logging.getLogger(__name__)

_TOAST_WIDTH = 300
_THUMBNAIL_WIDTH = 90
_THUMBNAIL_HEIGHT = _THUMBNAIL_WIDTH * 9 // 16
_DISPLAY_MS = 4500
_MARGIN = 20
_ENTRANCE_GEOMETRY_MS = 320
_ENTRANCE_OPACITY_MS = 220


class _ThumbnailFetcher(QObject):
    """Constructed on the GUI thread, `.fetch()` run on a background thread
    -- `ready.emit(...)` from that thread is automatically delivered to
    whatever's connected on the GUI thread via Qt.QueuedConnection.
    """

    ready = Signal(object)  # Path | None

    def __init__(self, ffmpeg_path: str, clip_path: Path, cache_dir: Path) -> None:
        super().__init__()
        self._ffmpeg_path = ffmpeg_path
        self._clip_path = clip_path
        self._cache_dir = cache_dir

    def fetch(self) -> None:
        thumb = thumbnails.ensure_thumbnail(self._ffmpeg_path, self._clip_path, self._cache_dir)
        self.ready.emit(thumb)


class SaveToast(QWidget):
    def __init__(self, ffmpeg_path: str, clip_path: Path, cache_dir: Path, parent: QWidget | None = None) -> None:
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

        title = QLabel("Clip saved", card)
        title.setObjectName("toastTitle")
        text_col.addWidget(title)

        name_label = QLabel(clip_path.name, card)
        name_label.setWordWrap(True)
        text_col.addWidget(name_label)

        hint_label = QLabel("Click to open folder", card)
        hint_label.setObjectName("hint")
        text_col.addWidget(hint_label)

        self.setFixedWidth(_TOAST_WIDTH)
        self.adjustSize()
        # Captured now, while width/height still reflect the fully laid-out
        # toast -- _build_entrance_animation() below immediately shrinks the
        # widget for the entrance animation, after which self.width()/
        # height() no longer describe the final size _final_geometry()
        # computes from, so this can't be recomputed later.
        self._final_rect = self._final_geometry()
        self._entrance_animation = self._build_entrance_animation()

        self._fetcher = _ThumbnailFetcher(ffmpeg_path, clip_path, cache_dir)
        self._fetcher.ready.connect(self._on_thumbnail_ready)
        threading.Thread(target=self._fetcher.fetch, daemon=True).start()

        QTimer.singleShot(_DISPLAY_MS, self.close)

    def _final_geometry(self) -> QRect:
        # availableGeometry() natively excludes the taskbar, unlike the old
        # Tk version's manual "_MARGIN * 3" extra-headroom guess.
        screen_rect = QGuiApplication.primaryScreen().availableGeometry()
        x = screen_rect.right() - self.width() - _MARGIN
        y = screen_rect.bottom() - self.height() - _MARGIN
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

    def mousePressEvent(self, event) -> None:  # noqa: N802 -- Qt's own naming convention
        if event.button() == Qt.MouseButton.LeftButton:
            open_folder(self._clip_path.parent)
            self.close()
        else:
            super().mousePressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        super().closeEvent(event)


def show_save_toast(parent: QWidget | None, ffmpeg_path: str, clip_path: Path, cache_dir: Path) -> None:
    """Show a toast for a just-saved clip. Never raises -- a toast is
    cosmetic, and a failure here must never take down the save it's
    celebrating.
    """
    try:
        toast = SaveToast(ffmpeg_path, clip_path, cache_dir, parent)
        toast.show()
        toast.start_entrance_animation()
    except Exception:  # noqa: BLE001 -- purely cosmetic, never fatal
        log.exception("Failed to show save toast for %s", clip_path)
