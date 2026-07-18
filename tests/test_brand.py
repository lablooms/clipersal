import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal.brand import DEFAULT_GLYPH, GLYPHS, BrandMark, SprigAccent


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_default_glyph_is_a_registered_glyph() -> None:
    assert DEFAULT_GLYPH in GLYPHS


def test_all_registered_glyphs_are_callable() -> None:
    assert len(GLYPHS) >= 3
    for draw_fn in GLYPHS.values():
        assert callable(draw_fn)


def test_brand_mark_defaults_to_requested_size() -> None:
    mark = BrandMark(size=40)
    assert mark.width() == 40
    assert mark.height() == 40


def test_brand_mark_defaults_to_default_glyph() -> None:
    mark = BrandMark()
    assert mark._glyph == DEFAULT_GLYPH


def test_brand_mark_accepts_specific_glyph() -> None:
    mark = BrandMark(glyph="bloom")
    assert mark._glyph == "bloom"


def test_set_glyph_updates_state() -> None:
    mark = BrandMark(glyph="bloom")
    mark.set_glyph("scattering_seed")
    assert mark._glyph == "scattering_seed"


def test_paints_without_raising_for_every_glyph() -> None:
    # Smoke test: constructing and grabbing a pixmap exercises paintEvent for
    # real (via the offscreen platform), catching any drawing-code exception
    # that unit-level assertions on internal state wouldn't.
    for glyph in GLYPHS:
        mark = BrandMark(size=32, glyph=glyph)
        mark.show()
        pixmap = mark.grab()
        assert not pixmap.isNull()
        mark.close()


def test_sprig_accent_defaults_to_requested_size() -> None:
    sprig = SprigAccent(size=40)
    assert sprig.width() == 40
    assert sprig.height() == 40


def test_sprig_accent_paints_without_raising() -> None:
    sprig = SprigAccent(size=48)
    sprig.show()
    pixmap = sprig.grab()
    assert not pixmap.isNull()
    sprig.close()
