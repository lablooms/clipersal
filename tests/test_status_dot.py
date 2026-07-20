import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal.status_dot import StatusDot


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_status_dot_defaults_to_requested_size() -> None:
    dot = StatusDot(size=20)
    assert dot.width() == 20
    assert dot.height() == 20


def test_set_color_updates_state() -> None:
    dot = StatusDot(color="#112233")
    dot.set_color("#445566")
    assert dot._color.name() == "#445566"


def test_pulse_starts_progress_animation() -> None:
    from PySide6.QtCore import QAbstractAnimation

    dot = StatusDot()
    dot.pulse("#ff8800")
    assert dot._pulse_anim.state() == QAbstractAnimation.State.Running
    assert dot._pulse_color.name() == "#ff8800"


def test_pulse_can_be_retriggered_before_finishing() -> None:
    # A second save shouldn't crash even if the previous pulse hasn't
    # finished yet -- pulse() stops and re-arms the ONE reused animation
    # rather than letting two animations drive the same property (the old
    # per-save QPropertyAnimation leaked one stopped child QObject per save).
    from PySide6.QtCore import QAbstractAnimation

    dot = StatusDot()
    dot.pulse("#ff8800")
    first_anim = dot._pulse_anim
    dot.pulse("#3fae4a")
    assert dot._pulse_anim is first_anim
    assert dot._pulse_anim.state() == QAbstractAnimation.State.Running
    assert dot._progress == 0.0  # restarted from the beginning


def test_pulse_reuses_a_single_animation_child_across_pulses() -> None:
    from PySide6.QtCore import QPropertyAnimation

    dot = StatusDot()
    dot.pulse("#ff8800")
    dot.pulse("#3fae4a")
    dot.pulse("#ff8800")
    animations = [child for child in dot.children() if isinstance(child, QPropertyAnimation)]
    assert animations == [dot._pulse_anim]


def test_reused_animation_reaches_its_end_state_after_retrigger() -> None:
    # The re-armed animation must run 0.0 -> 1.0 exactly like a freshly
    # constructed one -- the pixel-verified Phase-8.6 behavior unchanged.
    from PySide6.QtCore import QAbstractAnimation
    from clipersal.status_dot import _PULSE_DURATION_MS

    dot = StatusDot()
    dot.pulse("#ff8800")
    dot._pulse_anim.setCurrentTime(_PULSE_DURATION_MS)  # drive to the end deterministically
    assert dot._progress == 1.0
    assert dot._pulse_anim.state() == QAbstractAnimation.State.Stopped

    dot.pulse("#3fae4a")
    assert dot._progress == 0.0
    assert dot._pulse_anim.state() == QAbstractAnimation.State.Running
    dot._pulse_anim.setCurrentTime(_PULSE_DURATION_MS)
    assert dot._progress == 1.0


def test_paints_without_raising_idle_and_mid_pulse() -> None:
    dot = StatusDot()
    dot.show()
    assert not dot.grab().isNull()

    dot.pulse("#ff8800")
    dot._set_progress(0.5)
    assert not dot.grab().isNull()
    dot.close()


def test_widget_bounds_leave_room_for_satellites_beyond_the_resting_dot() -> None:
    # Regression coverage: a widget sized exactly to the resting dot's own
    # diameter would clip every scattering satellite invisibly, since Qt
    # never paints outside a widget's own bounds. The bounding box must be
    # strictly larger than the visible dot so the scatter has room to render.
    dot = StatusDot(size=36, dot_diameter=14)
    assert dot.width() > dot._dot_diameter
    assert dot.height() > dot._dot_diameter


def test_default_dot_diameter_is_half_the_bounding_box() -> None:
    dot = StatusDot(size=28)
    assert dot._dot_diameter == 14


def test_satellite_pixels_are_visible_outside_the_resting_dot_at_mid_pulse() -> None:
    # Renders actual pixels and checks a pulse-colored pixel exists beyond
    # the resting dot's own radius -- catches the "satellites are drawn but
    # the resting dot is painted on top of them, hiding them completely" bug
    # at the pixel level, since that bug left every internal *value* (color,
    # progress, radius) correct while the rendered result was still empty.
    from PySide6.QtGui import QColor

    dot = StatusDot(size=36, dot_diameter=14, color="#000000")
    dot._pulse_color = QColor("#ff0000")
    dot._set_progress(0.5)
    dot.show()
    image = dot.grab().toImage()

    base_radius = dot._dot_diameter / 2
    center_x, center_y = dot.width() / 2, dot.height() / 2
    found_satellite_pixel = False
    for x in range(dot.width()):
        for y in range(dot.height()):
            dist = ((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5
            if dist > base_radius + 1:
                # A "redness" comparison rather than an absolute threshold --
                # the satellite fades via alpha, so mid-pulse it's blended
                # with the background into a lighter pink, not solid red.
                pixel = image.pixelColor(x, y)
                if pixel.red() > 200 and (pixel.red() - pixel.green()) > 60:
                    found_satellite_pixel = True
                    break
        if found_satellite_pixel:
            break
    assert found_satellite_pixel, "expected a red satellite pixel outside the resting dot's radius"
