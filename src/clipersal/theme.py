"""Shared palette/font tokens -- used by every PySide6 window/widget in the
app (settings_window_qt.py, gallery_window_qt.py, toast_qt.py, main_window_qt.py,
first_run_qt.py, qt_widgets.py) so there's exactly one place to change colors.

Light-only as of the pre-v0.1.0-beta polish pass: dark mode (and the
sidebar's dark/light toggle) was removed at the user's request. Every
constant used to be a `(light, dark)` tuple -- see ARCHITECTURE.md's "PySide6
migration"/"Visual design" sections for that history -- and is now just the
light value, kept as a flat hex string.

Palette: Pollen Gold -- a warm gold/amber accent over a cream/parchment
background, matching Clipersal's "seed dispersal" identity (the phase after
a flower blooms -- see the app's tagline). Replaces the original Sakura
Pastel pink. Semantic status colors (GOOD/LIVE/NEUTRAL) are deliberately
their own palette, kept far enough from the gold hue that
"recording"/"saving" states don't blur into the accent color -- see
ARCHITECTURE.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QFont

BACKGROUND = "#FFFBF0"
SURFACE = "#FFFFFF"
SURFACE_RAISED = "#FFF3D6"
BORDER = "#F0DFA8"
TEXT = "#2E2410"
TEXT_MUTED = "#8A7550"
ACCENT = "#C8960C"
ACCENT_HOVER = "#A67908"
GOOD = "#4CAF50"  # recording, steady -- growth green, calm next to gold
LIVE = "#E67E22"  # actively saving/busy, pulses briefly -- bursting-seed-pod orange
NEUTRAL = "#A69374"  # paused -- warm taupe-grey, tinted toward the gold family
TRACK = "#F7E9C0"
ON_ACCENT_TEXT = "#FFFFFF"  # text/icon color sitting directly on an ACCENT-filled surface

MONO_FONT = ("Cascadia Code", "Consolas", "Courier New")


def qfont(size: int = 12, weight: str = "bold", mono: bool = False) -> "QFont":
    """QFont.setFamilies is Qt's own native ordered-fallback mechanism, so
    this needs no manual "pick index 0" fallback logic.
    """
    from PySide6.QtGui import QFont

    font = QFont()
    if mono:
        font.setFamilies(list(MONO_FONT))
    font.setPointSize(size)
    font.setBold(weight == "bold")
    return font


def build_stylesheet() -> str:
    """QSS applied once, globally, via `QApplication.setStyleSheet(build_stylesheet())`.

    Deliberately doesn't try to cover *every* pixel: the Home tab's status
    dot (imperative GOOD/LIVE/NEUTRAL recoloring + pulse animation) and the
    custom-painted ToggleSwitch/SegmentedControl widgets (qt_widgets.py) set
    colors directly from the constants above rather than through QSS
    selectors, the same "opt out of blanket theming, set this one directly"
    shape used throughout this file.

    Object names (`#card`, `#cardTitle`, `#hint`, `#primary`) are how a
    widget opts into this sheet's per-role look -- set via
    `setObjectName(...)` on whichever widget should get that look.
    """
    bg, surface, surface_raised, border, text, text_muted, accent, accent_hover, track, live, good = (
        BACKGROUND,
        SURFACE,
        SURFACE_RAISED,
        BORDER,
        TEXT,
        TEXT_MUTED,
        ACCENT,
        ACCENT_HOVER,
        TRACK,
        LIVE,
        GOOD,
    )
    on_accent = ON_ACCENT_TEXT

    return f"""
    QWidget {{
        background-color: {bg};
        color: {text};
    }}

    QFrame#card {{
        background-color: {surface};
        border: 1px solid {border};
        border-radius: 14px;
    }}

    QLabel#cardTitle {{
        color: {text_muted};
        font-weight: bold;
    }}

    QLabel#hint {{
        color: {text_muted};
    }}

    QLabel#toastTitle {{
        color: {accent};
        font-weight: bold;
    }}
    QLabel#thumbPlaceholder {{
        background-color: {surface_raised};
        border-radius: 8px;
    }}

    QLabel#valueBadge {{
        background-color: {track};
        color: {accent};
        border-radius: 6px;
        padding: 2px 8px;
    }}

    QLabel#statusLabel[state="error"] {{
        color: {live};
    }}
    QLabel#statusLabel[state="success"] {{
        color: {good};
    }}

    #sidebar {{
        background-color: {surface};
        border-right: 1px solid {border};
    }}
    QPushButton#navButton {{
        background-color: transparent;
        color: {text_muted};
        border: none;
        text-align: left;
        padding: 8px 12px;
        border-radius: 8px;
        font-weight: bold;
    }}
    QPushButton#navButton:hover {{
        background-color: {surface_raised};
    }}
    QPushButton#navButton:checked {{
        background-color: {surface_raised};
        color: {text};
    }}

    QPushButton {{
        background-color: {surface_raised};
        color: {text};
        border: 1px solid {border};
        border-radius: 8px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{
        background-color: {accent};
        color: {on_accent};
    }}
    QPushButton:disabled {{
        color: {text_muted};
    }}
    QPushButton#primary {{
        background-color: {accent};
        color: {on_accent};
        font-weight: bold;
        border: none;
    }}
    QPushButton#primary:hover {{
        background-color: {accent_hover};
    }}

    QPushButton#recordButton[recording="true"] {{
        background-color: {live};
        color: {on_accent};
        border: none;
    }}
    QPushButton#recordButton[recording="true"]:hover {{
        background-color: {live};
    }}

    #segmentedTrack {{
        background-color: {surface_raised};
        border: 1px solid {border};
        border-radius: 8px;
    }}
    QPushButton#segmentedButton {{
        background-color: transparent;
        border: none;
        border-radius: 6px;
        padding: 6px 12px;
    }}
    QPushButton#segmentedButton:hover {{
        background-color: {border};
        color: {text};
    }}
    QPushButton#segmentedButton:checked {{
        background-color: {accent};
        color: {on_accent};
        font-weight: bold;
    }}

    QLineEdit, QPlainTextEdit, QComboBox {{
        background-color: {surface_raised};
        color: {text};
        border: 1px solid {border};
        border-radius: 8px;
        padding: 4px 8px;
    }}
    QComboBox::drop-down {{
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background-color: {surface_raised};
        color: {text};
        selection-background-color: {accent};
        selection-color: {on_accent};
    }}

    QSlider::groove:horizontal {{
        background: {track};
        height: 6px;
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {accent};
        width: 16px;
        height: 16px;
        margin: -5px 0;
        border-radius: 8px;
    }}
    QSlider::sub-page:horizontal {{
        background: {accent};
        border-radius: 3px;
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: {bg};
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {border};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    """
