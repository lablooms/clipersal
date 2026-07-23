import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget

from clipersal import theme
from clipersal.qt_widgets import ToggleSwitch


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def reset_theme(qapp):
    # apply_theme() rewrites module-level state shared by the whole pytest
    # process -- every test here starts and ends in light mode, and the
    # shared QApplication's stylesheet is restored too, so no other Qt test
    # file ever sees a leftover dark palette.
    theme.apply_theme(False)
    yield
    theme.apply_theme(False)
    qapp.setStyleSheet(theme.build_stylesheet())


# ---- palette structure ------------------------------------------------------


def test_light_and_dark_palettes_have_identical_key_sets() -> None:
    assert set(theme.LIGHT_TOKENS) == set(theme.DARK_TOKENS)
    # Every palette key must also exist as a module-level constant -- a key
    # present in the dicts but missing as an attribute would never be read.
    for key in theme.LIGHT_TOKENS:
        assert isinstance(getattr(theme, key), str)


def test_light_palette_is_the_pre_dark_mode_light_theme_verbatim() -> None:
    # The light theme is pixel-frozen: these are the exact flat constants the
    # app shipped when it was light-only. Don't "improve" them -- tune the
    # dark palette instead.
    assert theme.LIGHT_TOKENS == {
        "BACKGROUND": "#FFFBF0",
        "SURFACE": "#FFFFFF",
        "SURFACE_RAISED": "#FFF3D6",
        "BORDER": "#F0DFA8",
        "TEXT": "#2E2410",
        "TEXT_MUTED": "#8A7550",
        "ACCENT": "#C8960C",
        "ACCENT_HOVER": "#A67908",
        "GOOD": "#4CAF50",
        "LIVE": "#E67E22",
        "NEUTRAL": "#A69374",
        "TRACK": "#F7E9C0",
        "ON_ACCENT_TEXT": "#FFFFFF",
    }


def test_dark_palette_differs_from_light_on_every_token() -> None:
    # A dark token that accidentally equals its light counterpart would be a
    # silently-unthemed element -- every role needs its own dark value.
    for key in theme.LIGHT_TOKENS:
        assert theme.DARK_TOKENS[key] != theme.LIGHT_TOKENS[key], key


# ---- apply_theme ------------------------------------------------------------


def test_theme_defaults_to_light() -> None:
    assert theme.current_theme() == "light"
    assert theme.BACKGROUND == theme.LIGHT_TOKENS["BACKGROUND"]


def test_apply_theme_rewrites_module_attributes_and_back() -> None:
    theme.apply_theme(True)
    assert theme.current_theme() == "dark"
    for key, value in theme.DARK_TOKENS.items():
        assert getattr(theme, key) == value

    theme.apply_theme(False)
    assert theme.current_theme() == "light"
    for key, value in theme.LIGHT_TOKENS.items():
        assert getattr(theme, key) == value


def test_build_stylesheet_reads_the_current_palette() -> None:
    light_sheet = theme.build_stylesheet()
    theme.apply_theme(True)
    dark_sheet = theme.build_stylesheet()

    assert dark_sheet != light_sheet
    assert theme.DARK_TOKENS["BACKGROUND"] in dark_sheet
    assert theme.DARK_TOKENS["ACCENT"] in dark_sheet
    assert theme.LIGHT_TOKENS["BACKGROUND"] not in dark_sheet


# ---- rendered pixels, both modes --------------------------------------------


def _themed_window() -> QWidget:
    window = QWidget()
    # Backgrounds are scoped, not blanket-painted: only containers that opt in
    # (object name or class) own a surface. "mainWindow" is the app's
    # top-level opt-in (see MainWindow.__init__ and theme.py).
    window.setObjectName("mainWindow")
    window.resize(120, 90)
    layout = QVBoxLayout(window)
    card = QFrame(window)
    card.setObjectName("card")
    layout.addWidget(card)
    return window


def test_qss_widget_background_pixel_differs_between_modes(qapp) -> None:
    window = _themed_window()
    qapp.setStyleSheet(theme.build_stylesheet())
    window.show()
    # (2, 2) sits on the window's own background, clear of the card's margins.
    light_pixel = window.grab().toImage().pixelColor(2, 2)

    # The same two steps cli.py's theme_changed slot performs, in order.
    theme.apply_theme(True)
    qapp.setStyleSheet(theme.build_stylesheet())
    dark_pixel = window.grab().toImage().pixelColor(2, 2)
    window.close()

    assert light_pixel.name() == theme.LIGHT_TOKENS["BACKGROUND"].lower()
    assert dark_pixel.name() == theme.DARK_TOKENS["BACKGROUND"].lower()
    assert light_pixel != dark_pixel


def test_qss_card_pixel_matches_each_modes_surface(qapp) -> None:
    window = _themed_window()
    qapp.setStyleSheet(theme.build_stylesheet())
    window.show()
    light_card = window.grab().toImage().pixelColor(60, 45)

    theme.apply_theme(True)
    qapp.setStyleSheet(theme.build_stylesheet())
    dark_card = window.grab().toImage().pixelColor(60, 45)
    window.close()

    assert light_card.name() == theme.LIGHT_TOKENS["SURFACE"].lower()
    assert dark_card.name() == theme.DARK_TOKENS["SURFACE"].lower()


def test_custom_painted_toggle_switch_repaints_against_new_tokens(qapp) -> None:
    # ToggleSwitch reads theme.ACCENT/theme.TRACK directly in paintEvent (no
    # QSS) -- the theme_changed slot's update() sweep is what refreshes it.
    switch = ToggleSwitch(checked=False)
    switch.show()
    # Sample the track interior, clear of the knob (parked left when off).
    sample_x, sample_y = (switch.width() * 3) // 4, switch.height() // 2
    light_track = switch.grab().toImage().pixelColor(sample_x, sample_y)

    theme.apply_theme(True)
    switch.update()
    dark_track = switch.grab().toImage().pixelColor(sample_x, sample_y)
    switch.close()

    assert light_track.name() == theme.LIGHT_TOKENS["TRACK"].lower()
    assert dark_track.name() == theme.DARK_TOKENS["TRACK"].lower()


def test_label_inside_a_card_shows_the_card_surface_not_a_background_box(qapp) -> None:
    # The regression pin for the scoped-background architecture: a blanket
    # QWidget background used to paint a visible BACKGROUND-colored rectangle
    # behind every label sitting on a SURFACE card.
    from PySide6.QtWidgets import QLabel

    window = _themed_window()
    card = window.findChild(QFrame, "card")
    label = QLabel("X", card)
    label.setGeometry(5, 5, 85, 20)
    qapp.setStyleSheet(theme.build_stylesheet())
    window.show()
    # (80, 12) is inside the label's rect, clear of its left-aligned ink.
    light_pixel = window.grab().toImage().pixelColor(80, 12)

    theme.apply_theme(True)
    qapp.setStyleSheet(theme.build_stylesheet())
    dark_pixel = window.grab().toImage().pixelColor(80, 12)
    window.close()

    assert light_pixel.name() == theme.LIGHT_TOKENS["SURFACE"].lower()
    assert dark_pixel.name() == theme.DARK_TOKENS["SURFACE"].lower()
    # ...and specifically NOT the window background that used to box it.
    assert light_pixel.name() != theme.LIGHT_TOKENS["BACKGROUND"].lower()


def test_value_badge_keeps_its_raised_track_background(qapp) -> None:
    # The transparency rule is for plain labels -- object-name opt-ins like
    # the value badge keep their intended raised background.
    from PySide6.QtWidgets import QLabel

    window = _themed_window()
    badge = QLabel("60s", window)
    badge.setObjectName("valueBadge")
    badge.setGeometry(10, 60, 50, 20)
    qapp.setStyleSheet(theme.build_stylesheet())
    window.show()
    pixel = window.grab().toImage().pixelColor(20, 70)
    window.close()

    assert pixel.name() == theme.LIGHT_TOKENS["TRACK"].lower()


# ---- stylesheet structure guards -----------------------------------------------


def test_stylesheet_has_settings_tab_styling() -> None:
    sheet = theme.build_stylesheet()
    assert "QTabWidget::pane" in sheet
    assert "QTabBar::tab:selected" in sheet
    # The selected tab's indicator is the accent top edge on both palettes.
    assert f"border-top: 2px solid {theme.LIGHT_TOKENS['ACCENT']}" in sheet
    theme.apply_theme(True)
    dark_sheet = theme.build_stylesheet()
    assert f"border-top: 2px solid {theme.DARK_TOKENS['ACCENT']}" in dark_sheet


def test_stylesheet_has_scroll_viewport_transparency_rule() -> None:
    # Regression pin: an unthemed scroll viewport painted a platform-grey /
    # dark box behind scrolled pages (the "rogue dark background" report).
    assert "QScrollArea QWidget#qt_scrollarea_viewport" in theme.build_stylesheet()
    assert "background: transparent;" in theme.build_stylesheet()


def test_stylesheet_pins_disabled_input_colors() -> None:
    sheet = theme.build_stylesheet()
    assert "QLineEdit:disabled" in sheet
    assert "QPlainTextEdit:disabled" in sheet


def test_stylesheet_has_themed_idle_video_surface() -> None:
    sheet = theme.build_stylesheet()
    assert "QVideoWidget#videoSurface" in sheet
    assert theme.LIGHT_TOKENS["SURFACE_RAISED"] in sheet


def test_pushbuttons_have_a_legibility_min_height() -> None:
    sheet = theme.build_stylesheet()
    assert "min-height: 28px" in sheet
    # ...but compact controls are explicitly exempt from the floor.
    assert "min-height: 0" in sheet


# ---- typography scale ----------------------------------------------------------


def test_typography_scale_values() -> None:
    assert theme.FONT_H1 == 18
    assert theme.FONT_H2 == 14
    assert theme.FONT_BODY == 12
    assert theme.FONT_HINT == 11
    assert theme.FONT_MONO == 11


def test_qfont_defaults_to_body_size() -> None:
    assert theme.qfont().pointSize() == theme.FONT_BODY


def test_scrollbars_are_minimal_floating_pills_in_both_orientations() -> None:
    # The modern minimal scrollbar: transparent track (no BACKGROUND-colored
    # strip), no add/sub-line buttons, no page-step fill, and an inset BORDER
    # handle (hover -> TEXT_MUTED, pressed -> ACCENT) reading as a floating
    # pill. The horizontal orientation is the Logs textbox's NoWrap box --
    # left unthemed it painted a platform-default white/grey strip in dark mode.
    sheet = theme.build_stylesheet()

    vertical_bar = sheet.split("QScrollBar:vertical")[1].split("}")[0]
    assert "background: transparent;" in vertical_bar
    assert "width: 8px;" in vertical_bar
    horizontal_bar = sheet.split("QScrollBar:horizontal")[1].split("}")[0]
    assert "background: transparent;" in horizontal_bar
    assert "height: 8px;" in horizontal_bar

    for orientation, extent, min_length in (
        ("vertical", "min-height", "height"),
        ("horizontal", "min-width", "width"),
    ):
        handle = sheet.split(f"QScrollBar::handle:{orientation}")[1].split("}")[0]
        assert f"background: {theme.LIGHT_TOKENS['BORDER']};" in handle
        assert "border-radius: 4px;" in handle
        assert f"{extent}: 24px;" in handle
        assert "margin: 1px;" in handle  # the inset that makes it a pill, not a strip

        hover = sheet.split(f"QScrollBar::handle:{orientation}:hover")[1].split("}")[0]
        assert f"background: {theme.LIGHT_TOKENS['TEXT_MUTED']};" in hover
        pressed = sheet.split(f"QScrollBar::handle:{orientation}:pressed")[1].split("}")[0]
        assert f"background: {theme.LIGHT_TOKENS['ACCENT']};" in pressed

        lines = sheet.split(f"QScrollBar::add-line:{orientation}, QScrollBar::sub-line:{orientation}")[1].split("}")[0]
        assert f"{min_length}: 0;" in lines
        pages = sheet.split(f"QScrollBar::add-page:{orientation}, QScrollBar::sub-page:{orientation}")[1].split("}")[0]
        assert "background: none;" in pages

    # The handle recolors follow a live theme switch like everything else.
    theme.apply_theme(True)
    dark_sheet = theme.build_stylesheet()
    dark_pressed = dark_sheet.split("QScrollBar::handle:vertical:pressed")[1].split("}")[0]
    assert f"background: {theme.DARK_TOKENS['ACCENT']};" in dark_pressed


def test_stepper_spin_buttons_have_their_own_compact_rule() -> None:
    # StepperSpinBox's stacked ▲/▼ buttons (qt_widgets.py): TRACK resting,
    # SURFACE_RAISED hover, ACCENT pressed -- and exempt from the QPushButton
    # legibility floor since the pair shares one line-edit height.
    sheet = theme.build_stylesheet()
    block = sheet.split("QPushButton#stepButton")[1].split("}")[0]
    assert f"background-color: {theme.LIGHT_TOKENS['TRACK']};" in block
    assert f"color: {theme.LIGHT_TOKENS['TEXT']};" in block
    assert "min-height: 0;" in block
    hover = sheet.split("QPushButton#stepButton:hover")[1].split("}")[0]
    assert f"background-color: {theme.LIGHT_TOKENS['SURFACE_RAISED']};" in hover
    pressed = sheet.split("QPushButton#stepButton:pressed")[1].split("}")[0]
    assert f"background-color: {theme.LIGHT_TOKENS['ACCENT']};" in pressed


def test_native_spinbox_qss_is_gone_with_the_native_spinboxes() -> None:
    # StepperSpinBox/StepperDoubleSpinBox replaced every QSpinBox/
    # QDoubleSpinBox in the app, so the old chrome rules would be dead weight.
    sheet = theme.build_stylesheet()
    assert "QAbstractSpinBox" not in sheet
    assert "QSpinBox" not in sheet


def test_heart_button_has_no_side_padding_to_clip_its_glyph() -> None:
    # 14px side-padding inside a 32px-fixed glyph button clipped the heart;
    # only a small bottom padding remains, for optical centering.
    heart_block = theme.build_stylesheet().split("QPushButton#heartButton")[1].split("}")[0]
    assert "padding: 0 0 2px 0;" in heart_block


def test_menu_button_is_exempt_from_the_legibility_floor() -> None:
    # The gallery row's "⋯" overflow button: tight glyph sizing, not the
    # base QPushButton 28px min-height / 14px side-padding.
    menu_block = theme.build_stylesheet().split("QPushButton#menuButton")[1].split("}")[0]
    assert "padding: 0 0 4px 0;" in menu_block
    assert "min-height: 0;" in menu_block


def test_support_button_hover_uses_accent_text() -> None:
    # The sidebar's "♥ Support" link: navButton-shaped, accent text on hover.
    sheet = theme.build_stylesheet()
    assert "QPushButton#supportButton" in sheet
    hover_block = sheet.split("QPushButton#supportButton:hover")[1].split("}")[0]
    assert f"color: {theme.ACCENT};" in hover_block
