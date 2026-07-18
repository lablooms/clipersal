"""One-off generator for the app icon -- run manually if the design changes,
not part of the shipped package or the build. Reuses the same Pollen Gold
accent and seed-puff botanical mark as the main window's sidebar BrandMark
(see theme.ACCENT and brand.py's draw_seed_puff), so the app icon, taskbar
icon, and in-window brand mark all read as the same identity.

The seed-puff glyph is hand-duplicated here using ImageDraw primitives
(brand.py's version is QPainter-drawn) -- Pillow and Qt are two entirely
separate rendering stacks with no shared drawing code, the same reason the
accent color itself is already duplicated as literal hex constants between
this file and theme.py rather than imported from one shared source.

Needs Pillow installed separately (`pip install Pillow`) -- the main app
dropped it as a dependency once the PySide6 migration moved every other use
of it (thumbnails, tray icon, toast previews) to QPixmap/QPainter instead;
this script is the one intentional holdout, since rewriting a
rarely-run-by-hand dev tool in Qt just to avoid one manual pip install
wasn't worth it.

Usage: python packaging/generate_icon.py
Outputs: assets/icon.png (1024x1024), assets/icon.ico (multi-res, Windows)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ACCENT_DARK = (232, 185, 35)  # #E8B923, matches theme.ACCENT (dark side)
ACCENT_LIGHT = (200, 150, 12)  # #C8960C, matches theme.ACCENT (light side)
ON_ACCENT_TEXT = (255, 255, 255)  # matches theme.ON_ACCENT_TEXT
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _draw_seed_puff(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int]) -> None:
    """Mirrors brand.py's draw_seed_puff() proportions exactly (a small
    center circle with 9 radiating spokes ending in tiny dots) -- see that
    function's docstring for why this shape was chosen over the other two
    brand-mark candidates.
    """
    center_r = r * 0.22
    stem_len = r * 0.85
    tip_r = r * 0.09
    # Pillow's line `width` is a total stroke thickness, the same convention
    # as QPen.setWidthF() in brand.py -- no unit conversion needed between
    # the two formulas.
    line_width = max(1, round(r * 0.07))
    spoke_count = 9

    for i in range(spoke_count):
        angle = math.radians(360 / spoke_count * i)
        x1 = cx + center_r * math.cos(angle)
        y1 = cy + center_r * math.sin(angle)
        x2 = cx + stem_len * math.cos(angle)
        y2 = cy + stem_len * math.sin(angle)
        draw.line([(x1, y1), (x2, y2)], fill=color, width=line_width)
        draw.ellipse([x2 - tip_r, y2 - tip_r, x2 + tip_r, y2 + tip_r], fill=color)

    draw.ellipse([cx - center_r, cy - center_r, cx + center_r, cy + center_r], fill=color)


def _build_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = round(size * 0.06)
    radius = round(size * 0.22)
    # Vertical gradient approximated with horizontal bands -- avoids a
    # per-pixel loop, plenty smooth at icon sizes.
    bands = 48
    band_height = (size - 2 * margin) / bands
    for i in range(bands):
        t = i / (bands - 1)
        color = tuple(round(ACCENT_DARK[c] + (ACCENT_LIGHT[c] - ACCENT_DARK[c]) * t) for c in range(3))
        y0 = margin + i * band_height
        y1 = margin + (i + 1) * band_height + 1
        band = Image.new("RGBA", (size, int(y1 - y0) + 1), (*color, 255))
        image.paste(band, (0, int(y0)), band)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, size - margin, size - margin), radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    image = rounded
    draw = ImageDraw.Draw(image)

    # Seed-puff glyph, inset from the rounded background the same way
    # BrandMark insets it from its own widget bounds (margin = 22% of the
    # background's own size, not of the full 1024px canvas including the
    # small edge padding above) -- keeps the glyph-to-background proportions
    # identical between the Qt sidebar mark and this icon.
    bg_size = size - 2 * margin
    glyph_margin = bg_size * 0.22
    inner_size = bg_size - 2 * glyph_margin
    cx, cy = size / 2, size / 2
    _draw_seed_puff(draw, cx, cy, inner_size / 2, ON_ACCENT_TEXT)
    return image


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    master = _build_icon(1024)
    master.save(ASSETS_DIR / "icon.png")

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(ASSETS_DIR / "icon.ico", sizes=[(s, s) for s in ico_sizes])

    print(f"Wrote {ASSETS_DIR / 'icon.png'} and {ASSETS_DIR / 'icon.ico'}")


if __name__ == "__main__":
    main()
