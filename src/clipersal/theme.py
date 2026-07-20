"""Shared palette/font tokens -- used by every PySide6 window/widget in the
app (settings_window_qt.py, gallery_window_qt.py, toast_qt.py, main_window_qt.py,
first_run_qt.py, qt_widgets.py) so there's exactly one place to change colors.

Dark mode lives here as two full palettes behind the same flat module-level
constants. History: the constants used to be `(light, dark)` tuples, then
collapsed to light-only flat strings in the pre-v0.1.0-beta polish pass
(see ARCHITECTURE.md's "PySide6 migration"/"Visual design" sections), and
dark mode is now back -- this time as `LIGHT_TOKENS`/`DARK_TOKENS` dicts
with `apply_theme()` rewriting the module-level attributes in place, so
every existing `theme.ACCENT`-style read keeps working unchanged. The one
import shape that does NOT follow a theme switch is a by-value import
(`from clipersal.theme import ACCENT`): that binds the string at import
time, so any module reading tokens must import the module itself
(`from clipersal import theme`) and read attributes at call time --
main_window_qt.py's GOOD/LIVE/NEUTRAL status-dot reads are the canonical
example.

Palette: Pollen Gold -- a warm gold/amber accent over a cream/parchment
background, matching Clipersal's "seed dispersal" identity (the phase after
a flower blooms -- see the app's tagline). Replaces the original Sakura
Pastel pink. The dark palette is a true Pollen Gold dark variant, not a
generic blue-grey dark theme: warm dark-brown backgrounds, cream text, the
same gold accent family brightened for dark-surface contrast. Semantic
status colors (GOOD/LIVE/NEUTRAL) are deliberately their own palette in
both modes, kept far enough from the gold hue that "recording"/"saving"
states don't blur into the accent color -- see ARCHITECTURE.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QFont

# The light values are exactly the flat constants the app shipped with when
# it was light-only -- they are the source of truth for the light theme and
# must stay pixel-identical, so don't "improve" them here.
LIGHT_TOKENS = {
    "BACKGROUND": "#FFFBF0",
    "SURFACE": "#FFFFFF",
    "SURFACE_RAISED": "#FFF3D6",
    "BORDER": "#F0DFA8",
    "TEXT": "#2E2410",
    "TEXT_MUTED": "#8A7550",
    "ACCENT": "#C8960C",
    "ACCENT_HOVER": "#A67908",
    "GOOD": "#4CAF50",  # recording, steady -- growth green, calm next to gold
    "LIVE": "#E67E22",  # actively saving/busy, pulses briefly -- bursting-seed-pod orange
    "NEUTRAL": "#A69374",  # paused -- warm taupe-grey, tinted toward the gold family
    "TRACK": "#F7E9C0",
    "ON_ACCENT_TEXT": "#FFFFFF",  # text/icon color sitting directly on an ACCENT-filled surface
}

# Pollen Gold after dark: the light palette's roles mapped onto a warm
# espresso-bronze scale (never a blue-grey), with the gold accent brightened
# ~20% so it keeps its contrast against dark surfaces, and hover going
# LIGHTER instead of darker (dark-theme convention). GOOD/LIVE/NEUTRAL are
# lifted and LIVE is pushed further toward red-orange, because the brighter
# accent would otherwise sit closer to LIVE's hue than the light mode's
# pairing did. ON_ACCENT_TEXT inverts: dark brown text on the bright gold
# reads better than white on a lighter accent.
DARK_TOKENS = {
    "BACKGROUND": "#1B1409",  # deep espresso brown -- warm, not charcoal-blue
    "SURFACE": "#271E0E",  # card brown, one step above the background
    "SURFACE_RAISED": "#3A2C14",  # hover/fill bronze-brown
    "BORDER": "#5A4626",  # muted bronze edge, visible against both surface tones
    "TEXT": "#F4E9CD",  # the light palette's own cream, inverted into the text role
    "TEXT_MUTED": "#B39B6E",  # warm tan -- muted but readable on dark surfaces
    "ACCENT": "#E3B52E",  # the same gold family as light's #C8960C, brightened for dark
    "ACCENT_HOVER": "#F2CB4D",  # lighter on hover (dark-theme convention, not light's deepen)
    "GOOD": "#63BD6B",  # growth green lifted for dark, still far from the gold hue
    "LIVE": "#EF7633",  # red-shifted orange, keeping its hue distance from the brighter gold
    "NEUTRAL": "#9C8B6D",  # warm taupe, same family as light's NEUTRAL, readable on dark
    "TRACK": "#4A3A1D",  # dark bronze groove / off-toggle track, visible on SURFACE
    "ON_ACCENT_TEXT": "#2A1F07",  # dark brown on the bright gold -- inverts the light pairing
}

_ACTIVE_THEME = "light"


def current_theme() -> str:
    """Which palette the module-level constants currently hold: "light" | "dark"."""
    return _ACTIVE_THEME


def apply_theme(dark: bool) -> None:
    """Rewrite the module-level token constants (BACKGROUND, ACCENT, ...)
    from the chosen palette, in place.

    Called once at startup BEFORE the QApplication's global stylesheet is
    first built (so a configured dark mode never flashes light), and again
    on every live Settings theme flip -- after which cli.py re-applies
    `build_stylesheet()` and emits `theme_changed` so QSS-styled widgets
    re-polish and custom-painted widgets repaint against the new values.
    """
    global _ACTIVE_THEME
    globals().update(DARK_TOKENS if dark else LIGHT_TOKENS)
    _ACTIVE_THEME = "dark" if dark else "light"


apply_theme(False)  # establish the module-level constants at import time

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
    """QSS applied globally via `QApplication.setStyleSheet(build_stylesheet())`
    -- once at startup, then again on every live theme switch. Reads the
    module-level constants at call time, so it always renders whichever
    palette `apply_theme()` last installed.

    Backgrounds are scoped to the containers that own a surface (the main
    window, dialogs, menus, cards, inputs) rather than painted by a blanket
    `QWidget` rule -- a blanket background put a visible box behind every
    label sitting on a card. Everything else stays transparent so the surface
    beneath shows through.

    Deliberately doesn't try to cover *every* pixel: the Home tab's status
    dot (imperative GOOD/LIVE/NEUTRAL recoloring + pulse animation) and the
    custom-painted ToggleSwitch/SegmentedControl widgets (qt_widgets.py) set
    colors directly from the constants above rather than through QSS
    selectors, the same "opt out of blanket theming, set this one directly"
    shape used throughout this file.

    Object names (`#mainWindow`, `#card`, `#cardTitle`, `#hint`, `#primary`)
    are how a widget opts into this sheet's per-role look -- set via
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
        color: {text};
    }}

    /* Backgrounds are scoped to the containers that own a surface -- no
       blanket QWidget background, which painted a visible BACKGROUND-colored
       box behind every label sitting on a SURFACE card (and read as "a
       background behind each label"). Plain container widgets and labels stay
       transparent so the surface beneath them shows through. */
    QWidget#mainWindow, QDialog {{
        background-color: {bg};
    }}

    QLabel {{
        background: transparent;
    }}

    QMenu {{
        background-color: {surface};
        color: {text};
        border: 1px solid {border};
    }}

    QToolTip {{
        background-color: {surface};
        color: {text};
        border: 1px solid {border};
        padding: 4px 8px;
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
