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

# Typography scale (point sizes) -- the ONLY sizes widgets should use, so a
# rogue ad-hoc `pointSize()+N` or a bare "size 10 here, 13 there" can't creep
# back in. The rules (enforced by the audit below, kept by hand afterwards):
#
#   H1 (18, bold)  -- page titles: the Home/Clips/Settings tab headers, the
#                     About card's app name, dialog main titles (the first-run
#                     wizard's welcome line).
#   H2 (14)        -- card/section titles: the #cardTitle style (settings
#                     cards, the Home "RECENT CLIPS" section, the player's
#                     "TRIM" card) and the banner/toast titles
#                     (#crashTitle/#bannerTitle/#toastTitle); also the Home
#                     status word ("Recording") and the sidebar wordmark.
#   BODY (12)      -- everything interactive or read as prose: buttons,
#                     combos, inputs, menu items, tab labels, field labels.
#                     This is the QApplication default font (cli.py installs
#                     it), so plain widgets need no setFont call at all.
#   HINT (11)      -- secondary text: hints, meta lines, footers, empty
#                     states (the #hint QSS rule pins the size).
#   MONO (11)      -- code-ish readouts: the Logs textbox, the status card's
#                     meta/stats lines, the settings value badges.
#
# Weight: bold is reserved for page titles, card/section titles, the status
# word, clip names (gallery faces + details/export dialogs), and #primary
# buttons -- nothing else, not even nav buttons or the checked segment of a
# SegmentedControl. Card titles are authored in LITERAL caps (Qt QSS has no
# text-transform), matching the settings cards that established the look.
FONT_H1 = 18
FONT_H2 = 14
FONT_BODY = 12
FONT_HINT = 11
FONT_MONO = 11


def qfont(size: int = FONT_BODY, weight: str = "bold", mono: bool = False) -> "QFont":
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
    /* Gallery list rows and grid cards alike: an accent edge on hover.
       Same 1px width as the resting border, so there's no layout shift. */
    QFrame#card:hover {{
        border: 1px solid {accent};
    }}

    QLabel#cardTitle {{
        color: {text_muted};
        font-size: {FONT_H2}pt;
        font-weight: bold;
    }}

    QLabel#hint {{
        color: {text_muted};
        font-size: {FONT_HINT}pt;
    }}

    QLabel#toastTitle {{
        color: {accent};
        font-size: {FONT_H2}pt;
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

    /* Banner titles on the Home tab: LIVE (danger-adjacent orange, see the
       module docstring's semantic color note) for the crash banner, accent
       gold for the low-disk warning. */
    QLabel#crashTitle {{
        color: {live};
        font-size: {FONT_H2}pt;
        font-weight: bold;
    }}
    QLabel#bannerTitle {{
        color: {accent};
        font-size: {FONT_H2}pt;
        font-weight: bold;
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
    }}
    QPushButton#navButton:hover {{
        background-color: {surface_raised};
    }}
    QPushButton#navButton:checked {{
        background-color: {surface_raised};
        color: {text};
    }}

    /* The sidebar's "♥ Support" link: the navButton's shape (transparent,
       muted, left-aligned) but never checkable -- its hover cue is accent
       text instead of the raised fill alone. */
    QPushButton#supportButton {{
        background-color: transparent;
        color: {text_muted};
        border: none;
        text-align: left;
        padding: 8px 12px;
        border-radius: 8px;
    }}
    QPushButton#supportButton:hover {{
        background-color: {surface_raised};
        color: {accent};
    }}

    QPushButton {{
        background-color: {surface_raised};
        color: {text};
        border: 1px solid {border};
        border-radius: 8px;
        padding: 6px 14px;
        /* Legibility floor: with 6px vertical padding alone the buttons
           rendered short enough that their text read cramped/truncated. */
        min-height: 28px;
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

    /* Gallery batch delete -- the destructive-action look. LIVE is the
       palette's danger-adjacent color (see the module docstring's semantic
       color note); the disabled state falls back to a plain raised button
       so "Delete selected (0)" doesn't read as armed. */
    QPushButton#danger {{
        background-color: {live};
        color: {on_accent};
        border: none;
    }}
    QPushButton#danger:hover {{
        background-color: {live};
    }}
    QPushButton#danger:disabled {{
        background-color: {surface_raised};
        color: {text_muted};
    }}

    /* Gallery favorite heart: a borderless glyph button whose only state
       cue is its text color -- muted when off, accent when checked. Reading
       the tokens here (at stylesheet-build time) keeps the heart following
       live theme switches like every QSS-styled widget. */
    QPushButton#heartButton {{
        background-color: transparent;
        border: none;
        border-radius: 8px;
        color: {text_muted};
        font-size: 17px;  /* icon glyph (♥), intentionally px-sized */
        padding: 0 0 2px 0;  /* optical centering: the glyph rides high in its line box */
        min-height: 0;  /* glyph button -- exempt from the QPushButton legibility floor */
    }}
    QPushButton#heartButton:hover {{
        background-color: {surface_raised};
        color: {accent};
    }}
    QPushButton#heartButton:checked {{
        color: {accent};
    }}

    /* Gallery row's "⋯" overflow button (opens the row's full right-click
       menu): the default raised-button look, minus the legibility floor and
       side-padding that would make a single glyph oddly wide. */
    QPushButton#menuButton {{
        padding: 0 0 4px 0;  /* ⋯ sits high in the line box -- push it to optical center */
        min-height: 0;
        font-size: 15px;  /* icon glyph (⋯), intentionally px-sized */
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
        min-height: 0;  /* segmented buttons keep their compact track size */
    }}
    QPushButton#segmentedButton:hover {{
        background-color: {border};
        color: {text};
    }}
    QPushButton#segmentedButton:checked {{
        background-color: {accent};
        color: {on_accent};
    }}

    QLineEdit, QPlainTextEdit, QComboBox {{
        background-color: {surface_raised};
        color: {text};
        border: 1px solid {border};
        border-radius: 8px;
        padding: 4px 8px;
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{
        /* The platform style's own disabled paint can be an unthemed grey
           box; pin it to the palette and just mute the text. */
        background-color: {surface_raised};
        color: {text_muted};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
    }}
    QComboBox::drop-down:hover {{
        background-color: {track};
    }}
    QComboBox QAbstractItemView {{
        background-color: {surface_raised};
        color: {text};
        selection-background-color: {accent};
        selection-color: {on_accent};
    }}

    /* StepperSpinBox's stacked ▲/▼ buttons (qt_widgets.py): two small glyph
       buttons in a TRACK column beside the themed line edit, replacing the
       native spinbox's up/down chrome (tiny platform triangles on a grey
       strip) that QSS could never restyle far enough. Exempt from the
       QPushButton legibility floor: the pair shares one line-edit height. */
    QPushButton#stepButton {{
        background-color: {track};
        color: {text};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 0;
        min-height: 0;
        font-size: 9px;  /* ▲/▼ icon glyphs, intentionally px-sized */
    }}
    QPushButton#stepButton:hover {{
        background-color: {surface_raised};
    }}
    QPushButton#stepButton:pressed {{
        background-color: {accent};
        color: {on_accent};
    }}
    QPushButton#stepButton:disabled {{
        color: {text_muted};
    }}

    /* Checkbox indicator (gallery selection mode): a themed rounded square,
       accent-filled when on -- mirrors the ToggleSwitch's on/off reading. */
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 5px;
        border: 1px solid {border};
        background-color: {surface_raised};
    }}
    QCheckBox::indicator:hover {{
        border-color: {accent};
    }}
    QCheckBox::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
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

    /* Settings' tab bar: raised-gold tabs on the pane, muted text until
       selected, and an ACCENT top edge as the selected indicator. The pane
       itself stays transparent (cards paint their own surfaces); the -1px
       top overlap lets the selected tab's bottom edge merge into it. */
    QTabWidget::pane {{
        border: 1px solid {border};
        border-radius: 10px;
        background: transparent;
        top: -1px;
    }}
    QTabBar::tab {{
        background-color: {surface_raised};
        color: {text_muted};
        border: 1px solid {border};
        border-bottom: none;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 8px 16px;
        margin-right: 4px;
    }}
    QTabBar::tab:hover:!selected {{
        color: {text};
    }}
    QTabBar::tab:selected {{
        background-color: {bg};
        color: {text};
        border-top: 2px solid {accent};
    }}

    /* The player's idle surface. QVideoWidget paints its own (black) frame
       during playback -- expected -- but before/without media it showed an
       unthemed black box; idle chrome now reads as a raised panel. */
    QVideoWidget#videoSurface {{
        background-color: {surface_raised};
        border: 1px solid {border};
        border-radius: 10px;
    }}

    QScrollArea QWidget#qt_scrollarea_viewport {{
        /* A scroll viewport can otherwise paint an unthemed (platform-grey
           or dark) box behind the scrolled page. */
        background: transparent;
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}
    /* Modern minimal scrollbar, both orientations symmetric: a fully
       transparent track (no BACKGROUND-colored strip), no add/sub-line
       buttons, no page-step fill, and an inset handle that reads as a
       floating pill rather than a strip filling the gutter. */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {border};
        border-radius: 4px;
        min-height: 24px;
        margin: 1px;  /* the inset that makes it a pill, not a strip */
    }}
    QScrollBar::handle:vertical:hover {{
        background: {text_muted};
    }}
    QScrollBar::handle:vertical:pressed {{
        background: {accent};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}

    /* Horizontal twin (the Logs textbox's NoWrap box, wide pages): left
       unthemed it painted a platform-default white/grey strip, glaring in
       dark mode. */
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {border};
        border-radius: 4px;
        min-width: 24px;
        margin: 1px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {text_muted};
    }}
    QScrollBar::handle:horizontal:pressed {{
        background: {accent};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
    }}
    """
