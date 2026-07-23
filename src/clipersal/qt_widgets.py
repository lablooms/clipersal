"""Custom Qt widgets with no native PySide6 equivalent -- built once here,
reused across every migrated tab, rather than re-implemented per call site.

ToggleSwitch gets its colors from theme.py's flat hex constants directly,
not from the global QSS stylesheet -- the same "opt out of blanket theming,
set this one directly" shape used elsewhere, see theme.build_stylesheet's
docstring for why.

StepperSpinBox/StepperDoubleSpinBox are the exception to "no native
equivalent": they re-skin QSpinBox/QDoubleSpinBox (themed line edit + two
glyph buttons) because the native up/down chrome can't be restyled far
enough with QSS alone -- see _StepperSpinBase's docstring.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Property, QEasingCurve, QEvent, QObject, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QFontMetrics, QIntValidator, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
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


class _StepperSpinBase(QWidget):
    """Shared guts of StepperSpinBox / StepperDoubleSpinBox: a themed
    QLineEdit plus two small stacked ▲/▼ buttons on the right, replacing a
    native spinbox's chrome. The QSS-only restyle of QAbstractSpinBox's
    up/down column kept rendering as tiny platform triangles on a grey strip
    next to the themed inputs (the recurring "the up and down choice is
    still bad" complaint), so the chrome is now two plain QPushButtons
    styled by the QPushButton#stepButton rule in theme.py.

    Deliberately NOT a QAbstractSpinBox subclass: the app-wide WheelGuard
    only eats wheel events for combo/spinbox/slider ancestry, and this
    widget must keep plain-QLineEdit wheel behavior -- ignore the wheel and
    let the event bubble up to scroll the page. Up/Down arrow keys step
    while the edit has focus; typing commits on Enter/focus-out (the line
    edit's own editingFinished), with out-of-range text clamped into the
    range and unparseable text reverted. The API is the subset of the Qt
    spinbox API the call sites use (settings' quick-save seconds, the GIF
    export dialog's fields), so those read like they always did.
    """

    editingFinished = Signal()
    # valueChanged lives on the subclasses -- its payload type differs
    # (int vs float), and a Qt signal's signature is fixed per class.

    _BUTTON_COLUMN_WIDTH = 22

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minimum = self._coerce(0)
        self._maximum = self._coerce(99)
        self._step = self._default_step()
        self._suffix = ""
        self._value = self._coerce(0)

        self._edit = QLineEdit(self)
        self._edit.setValidator(self._make_validator())

        self._up_button = QPushButton("▲", self)
        self._up_button.setObjectName("stepButton")
        self._down_button = QPushButton("▼", self)
        self._down_button.setObjectName("stepButton")
        for button in (self._up_button, self._down_button):
            button.setFixedWidth(self._BUTTON_COLUMN_WIDTH)
            # Click-to-step without stealing the line edit's focus, so the
            # arrow keys keep stepping right after a button click (native
            # spinbox behavior).
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoRepeat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self._edit, 1)
        buttons_column = QVBoxLayout()
        buttons_column.setContentsMargins(0, 0, 0, 0)
        buttons_column.setSpacing(2)
        buttons_column.addWidget(self._up_button)
        buttons_column.addWidget(self._down_button)
        layout.addLayout(buttons_column)

        self.setFocusProxy(self._edit)
        self._up_button.clicked.connect(self.stepUp)
        self._down_button.clicked.connect(self.stepDown)
        self._edit.editingFinished.connect(self._commit_edit)
        self._edit.installEventFilter(self)
        self._refresh_text()

    # ---- the Qt spinbox API subset the call sites use -------------------------

    def value(self):
        return self._value

    def setValue(self, value) -> None:
        # Clamps into the range and only emits on an actual change -- both
        # deliberate QSpinBox matches (the Settings tab's rollback blocks
        # signals and re-setValue()s; a no-change emit would re-schedule the
        # autosave it undoes).
        clamped = max(self._minimum, min(self._maximum, self._coerce(value)))
        if clamped == self._value:
            return
        self._value = clamped
        self._refresh_text()
        self.valueChanged.emit(clamped)

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum

    def setMinimum(self, minimum) -> None:
        self.setRange(minimum, self._maximum)

    def setMaximum(self, maximum) -> None:
        self.setRange(self._minimum, maximum)

    def setRange(self, minimum, maximum) -> None:
        self._minimum = self._coerce(minimum)
        self._maximum = self._coerce(maximum)
        self._update_validator_range()
        # A range change can pull the current value back inside -- the same
        # clamping (and valueChanged-on-clamp) QSpinBox applies.
        self.setValue(self._value)

    def singleStep(self):
        return self._step

    def setSingleStep(self, step) -> None:
        self._step = self._coerce(step)

    def suffix(self) -> str:
        return self._suffix

    def setSuffix(self, suffix: str) -> None:
        self._suffix = suffix
        self._refresh_text()

    def stepUp(self) -> None:  # noqa: N802 -- Qt's spinbox naming
        self.setValue(self._value + self._step)

    def stepDown(self) -> None:  # noqa: N802 -- Qt's spinbox naming
        self.setValue(self._value - self._step)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 -- Qt's naming
        # Propagate explicitly: a disabled container renders its children
        # disabled anyway, but their own isEnabled() flags (and the QSS
        # :disabled styling some reads key off) only follow if set directly.
        super().setEnabled(enabled)
        self._edit.setEnabled(enabled)
        self._up_button.setEnabled(enabled)
        self._down_button.setEnabled(enabled)

    # ---- internals --------------------------------------------------------------

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802 -- Qt's naming
        # Arrow-key stepping. The filter runs before the line edit's own key
        # handling, which would ignore Up/Down anyway; every other key falls
        # through to normal editing.
        if watched is self._edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Up:
                self.stepUp()
                return True
            if event.key() == Qt.Key.Key_Down:
                self.stepDown()
                return True
        return super().eventFilter(watched, event)

    def _commit_edit(self) -> None:
        parsed = self._parse(self._edit.text())
        if parsed is not None:
            self.setValue(parsed)  # clamps; emits valueChanged on change
        # Reformat no matter what: valid-but-unformatted entry ("45" typed
        # into a suffixed field) gets its canonical text, unparseable text
        # reverts to the current value.
        self._refresh_text()
        self.editingFinished.emit()

    def _refresh_text(self) -> None:
        self._edit.setText(f"{self._format(self._value)}{self._suffix}")

    def _strip_suffix(self, text: str) -> str:
        if self._suffix and text.endswith(self._suffix):
            text = text[: -len(self._suffix)]
        return text.strip()

    # Subclass hooks: numeric type, formatting, parsing, validator.
    def _coerce(self, value):  # pragma: no cover - abstract hook
        raise NotImplementedError

    def _default_step(self):  # pragma: no cover - abstract hook
        raise NotImplementedError

    def _make_validator(self):  # pragma: no cover - abstract hook
        raise NotImplementedError

    def _update_validator_range(self) -> None:  # pragma: no cover - abstract hook
        raise NotImplementedError

    def _format(self, value) -> str:  # pragma: no cover - abstract hook
        raise NotImplementedError

    def _parse(self, text: str):  # pragma: no cover - abstract hook
        raise NotImplementedError


class StepperSpinBox(_StepperSpinBase):
    """Integer stepper field (quick-save seconds, GIF fps/width). Default
    step 1; the GIF width field's setSingleStep(20) is the one override."""

    valueChanged = Signal(int)

    @staticmethod
    def _coerce(value) -> int:
        return int(value)

    @staticmethod
    def _default_step() -> int:
        return 1

    def _make_validator(self) -> QIntValidator:
        # Bounded, so interactive typing can't leave the range at all (a
        # programmatic setText bypasses validators -- _commit_edit clamps).
        return QIntValidator(self._minimum, self._maximum, self._edit)

    def _update_validator_range(self) -> None:
        self._edit.validator().setRange(self._minimum, self._maximum)

    @staticmethod
    def _format(value: int) -> str:
        return str(value)

    def _parse(self, text: str) -> int | None:
        try:
            return int(self._strip_suffix(text))
        except ValueError:
            return None


class StepperDoubleSpinBox(_StepperSpinBase):
    """Float stepper field (GIF start/duration). Default step 0.5: both
    double call sites run decimals=1 ranges where 0.5 is the useful nudge
    (and stays exact in binary float, so repeated stepping can't drift)."""

    valueChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        # _decimals must exist before the base __init__ runs _coerce/_format.
        self._decimals = 2
        super().__init__(parent)

    def decimals(self) -> int:
        return self._decimals

    def setDecimals(self, decimals: int) -> None:
        self._decimals = decimals
        self._edit.validator().setDecimals(decimals)
        self._refresh_text()

    def _coerce(self, value) -> float:
        # Round to the display precision on every ingest, like QDoubleSpinBox
        # -- value() then always matches what the field shows.
        return round(float(value), self._decimals)

    @staticmethod
    def _default_step() -> float:
        return 0.5

    def _make_validator(self) -> QDoubleValidator:
        validator = QDoubleValidator(self._minimum, self._maximum, self._decimals, self._edit)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        return validator

    def _update_validator_range(self) -> None:
        self._edit.validator().setRange(self._minimum, self._maximum)

    def _format(self, value: float) -> str:
        return f"{value:.{self._decimals}f}"

    def _parse(self, text: str) -> float | None:
        try:
            parsed = float(self._strip_suffix(text))
        except ValueError:
            return None
        # float() accepts "nan"/"inf", which a bounded field must reject.
        return parsed if math.isfinite(parsed) else None
