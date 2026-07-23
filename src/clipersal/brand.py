"""Shared brand-mark glyph -- a botanical shape replacing the plain "▶" play
triangle used throughout the app. Drawn via QPainter paths rather than an
image asset, consistent with how qt_widgets.py's ToggleSwitch/SegmentedControl
and tray_qt.py's tray icon are already hand-painted.

Three candidate glyphs are kept side by side (`GLYPHS`) rather than deleted
once one is picked -- see ARCHITECTURE.md's visual design notes for why the
shape was chosen, and so a future revisit doesn't have to reinvent the
alternatives.

The system tray icon (tray_qt.py) deliberately does NOT use this glyph -- system
tray icons render at ~16-24px, too small for a multi-part botanical shape to stay
legible, and a plain colored dot is already the OS-conventional shape for an
at-a-glance status indicator there.

`packaging/generate_icon.py` (Pillow, a separate rendering stack from Qt) draws the
same glyph shape a second time using `ImageDraw` primitives for the app icon --
duplicated by hand rather than shared, the same way the accent color itself is
already duplicated as literal hex constants between that file and theme.py.

`app_icon()` loads that rendered asset (`assets/icon.png`) as the QApplication's
window icon -- the one place the actual image file is used at runtime instead of
a hand-painted Qt shape.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from clipersal import theme

log = logging.getLogger(__name__)


def draw_bloom(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """A simple 5-petal bloom outline: five overlapping circles arranged in a ring."""
    cx, cy = rect.center().x(), rect.center().y()
    r = min(rect.width(), rect.height()) / 2
    petal_r = r * 0.55
    petal_dist = r * 0.48
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for i in range(5):
        angle = math.radians(90 + i * 72)
        px = cx + petal_dist * math.cos(angle)
        py = cy - petal_dist * math.sin(angle)
        painter.drawEllipse(QPointF(px, py), petal_r, petal_r)


def draw_seed_puff(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """A dandelion/seed-puff silhouette: a small center circle with fine radiating
    lines ending in tiny dots -- the most literal match for "seed dispersal".
    """
    cx, cy = rect.center().x(), rect.center().y()
    r = min(rect.width(), rect.height()) / 2
    center_r = r * 0.22
    stem_len = r * 0.85
    tip_r = r * 0.09

    line_pen = QPen(color)
    line_pen.setWidthF(max(1.0, r * 0.07))
    line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    spoke_count = 9
    for i in range(spoke_count):
        angle = math.radians(360 / spoke_count * i)
        x1 = cx + center_r * math.cos(angle)
        y1 = cy + center_r * math.sin(angle)
        x2 = cx + stem_len * math.cos(angle)
        y2 = cy + stem_len * math.sin(angle)
        painter.setPen(line_pen)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(x2, y2), tip_r, tip_r)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(cx, cy), center_r, center_r)


def draw_scattering_seed(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """A single teardrop-shaped seed with small trailing dots implying motion --
    "the moment you bloomed" as a single scattering seed caught mid-flight.
    """
    cx, cy = rect.center().x(), rect.center().y()
    r = min(rect.width(), rect.height()) / 2
    seed_w, seed_h = r * 0.5, r * 0.9

    path = QPainterPath()
    path.moveTo(0, -seed_h / 2)
    path.cubicTo(seed_w / 2, -seed_h / 4, seed_w / 2, seed_h / 4, 0, seed_h / 2)
    path.cubicTo(-seed_w / 2, seed_h / 4, -seed_w / 2, -seed_h / 4, 0, -seed_h / 2)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.save()
    painter.translate(cx, cy)
    painter.rotate(-30)
    painter.drawPath(path)
    painter.restore()

    for dx, dy, s in ((-0.55, 0.5, 0.14), (-0.85, 0.75, 0.09), (-1.05, 0.95, 0.06)):
        painter.drawEllipse(QPointF(cx + r * dx, cy + r * dy), r * s, r * s)


def draw_sprig(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """A quiet curving sprig with a few small leaves and a couple of scattered
    seed dots near the tip, echoing the brand mark -- used as a subtle
    empty-state flourish, not a loud illustration. Deliberately line-art only
    (no filled background), so it reads as texture rather than an icon.
    """
    w, h = rect.width(), rect.height()
    x0, y0 = rect.left() + w * 0.5, rect.bottom()
    x1, y1 = rect.left() + w * 0.65, rect.top() + h * 0.1

    pen = QPen(color)
    pen.setWidthF(max(1.2, w * 0.035))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    stem = QPainterPath()
    stem.moveTo(x0, y0)
    stem.cubicTo(x0 + w * 0.05, y0 - h * 0.5, x1 - w * 0.1, y1 + h * 0.35, x1, y1)
    painter.drawPath(stem)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for i, t in enumerate((0.32, 0.58, 0.8)):
        px = x0 + (x1 - x0) * t
        py = y0 + (y1 - y0) * t
        side = 1 if i % 2 == 0 else -1
        leaf_w, leaf_h = w * 0.16, h * 0.09
        painter.save()
        painter.translate(px + side * leaf_w * 0.5, py)
        painter.rotate(side * 35)
        painter.drawEllipse(QRectF(-leaf_w / 2, -leaf_h / 2, leaf_w, leaf_h))
        painter.restore()

    for dx, dy, s in ((0.05, -0.08, 0.045), (0.14, -0.16, 0.03)):
        painter.drawEllipse(QPointF(x1 + w * dx, y1 + h * dy), w * s, w * s)


class SprigAccent(QWidget):
    """A small, quiet decorative flourish for empty states (the Clips tab's "no
    clips saved yet" message, the Home tab's "no clips yet", the first-run
    wizard header) -- muted TEXT_MUTED-toned line art, deliberately subtle
    rather than a loud illustration, matching the restraint already applied
    to hint text throughout the app.
    """

    def __init__(self, size: int = 48, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.TEXT_MUTED)
        rect = QRectF(0, 0, self.width(), self.height())
        draw_sprig(painter, rect, color)


GLYPHS: dict[str, Callable[[QPainter, QRectF, QColor], None]] = {
    "bloom": draw_bloom,
    "seed_puff": draw_seed_puff,
    "scattering_seed": draw_scattering_seed,
}

DEFAULT_GLYPH = "seed_puff"

# Studio identity + support links, opened from the main window's sidebar
# footer. Module constants (not inline literals) so the sidebar and tests
# reference exactly one value each.
LABLOOMS_URL = "https://github.com/lablooms"
SUPPORT_URL = "https://github.com/lablooms/clipersal"

# src/clipersal/brand.py -> repo root is two levels up. A module constant so
# tests can point the lookup elsewhere.
_SOURCE_ICON_PATH = Path(__file__).resolve().parents[2] / "assets" / "icon.png"


def app_icon() -> QIcon:
    """The app's window/taskbar icon, loaded from assets/icon.png (rendered by
    packaging/generate_icon.py; the .ico sibling covers the exe/installer
    side). Under a frozen PyInstaller build, data files live in sys._MEIPASS,
    so that location is tried first. A genuinely missing asset degrades to a
    null QIcon with a warning -- a missing icon must never break startup.
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets" / "icon.png")
    candidates.append(_SOURCE_ICON_PATH)
    for candidate in candidates:
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    log.warning(
        "App icon asset not found (tried %s); falling back to a null icon",
        ", ".join(str(candidate) for candidate in candidates),
    )
    return QIcon()


class BrandMark(QWidget):
    """The rounded accent-colored square/circle behind the glyph, shared by the
    main window's sidebar and the first-run wizard header so both read as the
    same identity.
    """

    def __init__(
        self,
        size: int = 32,
        glyph: str = DEFAULT_GLYPH,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self.setFixedSize(size, size)

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QColor(theme.ACCENT)
        fg = QColor(theme.ON_ACCENT_TEXT)

        rect = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        radius = min(self.width(), self.height()) * 0.28
        painter.drawRoundedRect(rect, radius, radius)

        margin = self.width() * 0.22
        inner_rect = rect.adjusted(margin, margin, -margin, -margin)
        GLYPHS[self._glyph](painter, inner_rect, fg)
