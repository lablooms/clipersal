"""Status-dot save-pulse -- a custom-painted circle.

Replaces a flat GOOD/LIVE/SURFACE_RAISED color-swap pulse
(a plain QLabel styled via stylesheet) with a QPainter-drawn dot whose
`pulse()` briefly scatters a few small satellite dots outward from the
center and fades them, reading as "seed dispersal" rather than a color
flicker -- consistent with the botanical motif the brand mark and empty-state
sprigs (brand.py) already carry. The resting dot color itself is still fully
controlled by the caller via `set_color()` (MainWindow._set_status_dot keeps
deciding GOOD/LIVE/NEUTRAL based on capture state); this widget only adds the
decorative scatter animation on top, exactly like `brand.py`'s glyphs are
decoration layered onto existing layout rather than a state machine of their
own.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

_PULSE_DURATION_MS = 700
_SATELLITE_COUNT = 3


class StatusDot(QWidget):
    def __init__(
        self,
        size: int = 28,
        dot_diameter: int | None = None,
        color: str = "#3fae4a",
        parent: QWidget | None = None,
    ) -> None:
        # `size` is the widget's own bounding box, deliberately larger than
        # the visible resting dot (`dot_diameter`, default size/2): Qt clips
        # all painting to a widget's own bounds, so satellites scattering
        # outward from a dot painted edge-to-edge in its widget would be
        # clipped away invisibly the instant they moved past the resting
        # dot's own radius. The extra padding is transparent background, not
        # a visible box, so it just reads as breathing room around the dot.
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._dot_diameter = dot_diameter if dot_diameter is not None else size / 2
        self._color = QColor(color)
        self._pulse_color = QColor(color)
        self._progress = 0.0
        self._pulse_anim: QPropertyAnimation | None = None

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def pulse(self, color: str) -> None:
        """Scatter satellite seeds outward from the dot and fade them. Called
        once per save (from MainWindow._run_pulse's first step) -- reverting
        the resting dot color back to GOOD is still handled entirely by that
        same timer via set_color(), not by this animation.
        """
        self._pulse_color = QColor(color)
        if self._pulse_anim is None:
            # ONE animation object for the dot's whole lifetime, re-armed on
            # every pulse. The old version constructed a new
            # QPropertyAnimation(parent=self) per save, and the stopped
            # previous one stayed a child QObject of this long-lived widget
            # forever -- one leaked animation per save.
            anim = QPropertyAnimation(self, b"progress", self)
            anim.setDuration(_PULSE_DURATION_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._pulse_anim = anim
        # stop() + start() restarts from 0 even mid-pulse -- the same visual
        # behavior as the old stop-the-old-one / start-a-new-one pair.
        self._pulse_anim.stop()
        self._progress = 0.0
        self._pulse_anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802 -- Qt's own naming convention
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        bounds = QRectF(0, 0, self.width(), self.height())
        center = bounds.center()
        base_radius = self._dot_diameter / 2
        dot_rect = QRectF(center.x() - base_radius, center.y() - base_radius, base_radius * 2, base_radius * 2)

        if self._progress > 0.0:
            satellite_radius = base_radius * 0.32
            # Must start beyond the resting dot's own radius -- the resting
            # dot is drawn last (on top, so it never looks eaten into by a
            # fading satellite), which silently hid every satellite behind
            # it when travel started from dead-center: at low progress a
            # satellite's whole disc sat entirely inside the opaque resting
            # dot and was painted over completely. Ends within the widget's
            # own bounds (half the smaller side) so a caller passing a
            # tighter `size` can't accidentally clip the scatter instead.
            min_travel = base_radius + satellite_radius
            max_travel = min(bounds.width(), bounds.height()) / 2 - satellite_radius
            travel = min_travel + (max_travel - min_travel) * self._progress
            fade = max(0.0, 1.0 - self._progress)
            satellite_color = QColor(self._pulse_color)
            satellite_color.setAlphaF(fade)
            painter.setBrush(satellite_color)
            for i in range(_SATELLITE_COUNT):
                angle = math.radians(90 + i * (360 / _SATELLITE_COUNT))
                dx = travel * math.cos(angle)
                dy = -travel * math.sin(angle)
                painter.drawEllipse(
                    QRectF(
                        center.x() + dx - satellite_radius,
                        center.y() + dy - satellite_radius,
                        satellite_radius * 2,
                        satellite_radius * 2,
                    )
                )

        painter.setBrush(self._color)
        painter.drawEllipse(dot_rect)
