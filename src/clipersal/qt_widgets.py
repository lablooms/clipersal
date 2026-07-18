"""Custom Qt widgets with no native PySide6 equivalent -- built once here,
reused across every migrated tab, rather than re-implemented per call site.

ToggleSwitch gets its colors from theme.py's flat hex constants directly,
not from the global QSS stylesheet -- the same "opt out of blanket theming,
set this one directly" shape used elsewhere, see theme.build_stylesheet's
docstring for why.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QPushButton, QSizePolicy, QWidget

from clipersal import theme


class ToggleSwitch(QAbstractButton):
    """A pill+knob toggle switch, painted directly via QPainter -- Qt has no
    native toggle-switch widget (only QCheckBox, which renders as a literal
    checkbox on every platform). Replaces CTkSwitch (the encoder auto-detect
    switch and the launch-on-startup toggle).
    """

    _WIDTH = 44
    _HEIGHT = 24
    _MARGIN = 2

    def __init__(self, parent: QWidget | None = None, checked: bool = False) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._WIDTH, self._HEIGHT)
        # Set directly (not via the animated setter) so construction never
        # animates from a wrong starting position -- the `toggled` signal
        # is only connected afterward, so setChecked() above didn't trigger it.
        self._knob_position = self._end_position()
        self._animation = QPropertyAnimation(self, b"knobPosition", self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate_to_current_state)

    def sizeHint(self):  # noqa: N802 -- Qt's own naming convention
        return self.size()

    def _end_position(self) -> float:
        return float(self._WIDTH - self._HEIGHT + self._MARGIN) if self.isChecked() else float(self._MARGIN)

    def _animate_to_current_state(self, _checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._knob_position)
        self._animation.setEndValue(self._end_position())
        self._animation.start()

    def _get_knob_position(self) -> float:
        return self._knob_position

    def _set_knob_position(self, value: float) -> None:
        self._knob_position = value
        self.update()

    knobPosition = Property(float, _get_knob_position, _set_knob_position)  # noqa: N815 -- Qt property naming

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track_color = QColor(theme.ACCENT if self.isChecked() else theme.TRACK)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        radius = self._HEIGHT / 2
        painter.drawRoundedRect(self.rect(), radius, radius)

        knob_diameter = self._HEIGHT - self._MARGIN * 2
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(self._knob_position, self._MARGIN, knob_diameter, knob_diameter))


class SegmentedControl(QWidget):
    """Exclusive multi-way choice control -- Qt has no native segmented-button
    widget (QTabBar implies page-switching semantics, a different visual
    language). Mirrors CTkSegmentedButton's values=/variable=/command= shape:
    construct with the choices, read/set the current one, connect
    currentTextChanged for the equivalent of CTk's command= callback.
    Used 3x: capture target, quality preset, encoder picker.
    """

    currentTextChanged = Signal(str)

    def __init__(self, values: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segmentedTrack")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._current: str | None = values[0] if values else None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        for value in values:
            button = QPushButton(value, self)
            button.setObjectName("segmentedButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, v=value: self._on_button_clicked(v))
            self._group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button)

        if values:
            self._buttons[values[0]].setChecked(True)

    def _on_button_clicked(self, value: str) -> None:
        if value != self._current:
            self._current = value
            self.currentTextChanged.emit(value)

    def current(self) -> str | None:
        return self._current

    def setCurrent(self, value: str) -> None:  # noqa: N802 -- matches Qt's own setter naming convention
        button = self._buttons.get(value)
        if button is not None:
            button.setChecked(True)
            self._current = value
