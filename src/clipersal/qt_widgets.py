"""Custom Qt widgets with no native PySide6 equivalent -- built once here,
reused across every migrated tab, rather than re-implemented per call site.

ToggleSwitch gets its colors from theme.py's flat hex constants directly,
not from the global QSS stylesheet -- the same "opt out of blanket theming,
set this one directly" shape used elsewhere, see theme.build_stylesheet's
docstring for why.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QEvent, QObject, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QWidget,
)

from clipersal import theme


class WheelGuard(QObject):
    """App-wide event filter that stops stray scroll-wheel edits: scrolling
    the Settings page (or any dialog) must never rewrite whichever
    combo/spinbox/slider the cursor happens to cross. With this filter
    installed on the QApplication (see cli._ensure_qapplication), a wheel
    event reaching one of those widgets is consumed, period -- no focus
    exception: a clicked combo keeps focus indefinitely, so "only when
    unfocused" still let page-scrolls change it later, which is exactly the
    corruption the user reported. Focused widgets keep keyboard adjustment
    (arrows), just not the wheel.

    The ancestry walk matters too: a QSpinBox's wheel events are delivered
    to its inner QLineEdit, not to the spinbox itself, so checking only the
    event target leaves the spinbox unguarded in the real app even though
    the naive direct-delivery test passes.
    """

    _GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QSlider)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802 -- Qt's naming
        if event.type() == QEvent.Type.Wheel:
            widget = watched
            while isinstance(widget, QWidget):
                if isinstance(widget, self._GUARDED_TYPES):
                    return True  # consume: the value never sees the wheel event
                widget = widget.parentWidget()
        return super().eventFilter(watched, event)


def quiet_message(
    parent: QWidget | None,
    title: str,
    text: str,
    buttons: "QMessageBox.StandardButton" = None,
    default_button: "QMessageBox.StandardButton | None" = None,
) -> "QMessageBox.StandardButton":
    """A QMessageBox without an icon, for the app's routine confirmations and
    results. On Windows the icon is what makes the system play its alert
    sound (Information -> "Asterisk", Warning -> "Exclamation") -- that
    "dudun~" on every export/confirm was flagged as annoying, so the app's
    message boxes are icon-less and silent. cli.py's startup CRITICAL box
    keeps its icon/sound: a real startup failure is exactly what system
    alert sounds are for.
    """
    from PySide6.QtWidgets import QMessageBox

    if buttons is None:
        buttons = QMessageBox.StandardButton.Ok
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.NoIcon)
    box.setStandardButtons(buttons)
    if default_button is not None:
        box.setDefaultButton(default_button)
    return box.exec()


class ElidedLabel(QLabel):
    """A single-line label that elides its text with an ellipsis when the
    available width is smaller than the text. setText()/text() keep the FULL
    string (callers never see the elided copy); the ellipsis follows the
    widget's actual width via resizeEvent. The Ignored horizontal size
    policy + zero minimum let the layout shrink it below the full text
    width -- without that, a long line (e.g. a full clips path on the Home
    status card) forces its row wider than the window minimum and the
    buttons sharing the row get squeezed into clipped text.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self._elide_mode = elide_mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self._update_elided()

    def setText(self, text: str) -> None:
        self._full_text = text
        self._update_elided()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        self._update_elided()
        super().resizeEvent(event)

    def _update_elided(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full_text, self._elide_mode, self.width()))


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

    def set_checked_silently(self, checked: bool) -> None:
        """setChecked without emitting `toggled` -- for hosts rolling a field
        back to a known-good value (the Settings tab's failed-apply restore),
        where re-emitting would re-trigger the very change being undone. The
        knob is snapped directly because the toggled-driven animation is
        suppressed along with everything else.
        """
        if self.isChecked() == checked:
            return
        self.blockSignals(True)
        try:
            self.setChecked(checked)
        finally:
            self.blockSignals(False)
        self._knob_position = self._end_position()
        self.update()

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
        # Plain QWidgets don't paint QSS backgrounds without this -- the
        # #segmentedTrack rule (raised fill + border) never rendered, and
        # the control read as loose floating buttons instead of a track.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
