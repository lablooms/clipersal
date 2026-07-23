import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from clipersal.brand import DEFAULT_GLYPH, GLYPHS, LABLOOMS_URL, SUPPORT_URL, BrandMark, SprigAccent


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


def test_lablooms_url_points_at_the_studio_org() -> None:
    assert LABLOOMS_URL == "https://github.com/lablooms"


def test_support_url_points_at_the_project_repo() -> None:
    assert SUPPORT_URL == "https://github.com/lablooms/clipersal"


# ---- app_icon ----------------------------------------------------------------

# A valid 1x1 transparent PNG, used as the sentinel asset under a fake
# sys._MEIPASS -- unmistakably different from the repo's 1024x1024 icon.png.
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_app_icon_loads_the_repo_asset() -> None:
    from clipersal import brand

    icon = brand.app_icon()
    assert not icon.isNull()
    assert not icon.pixmap(32, 32).isNull()


def test_app_icon_prefers_the_meipass_asset_when_frozen(tmp_path, monkeypatch) -> None:
    import base64

    from PySide6.QtCore import QSize

    from clipersal import brand

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "icon.png").write_bytes(base64.b64decode(_TINY_PNG_B64))
    monkeypatch.setattr(brand.sys, "frozen", True, raising=False)
    monkeypatch.setattr(brand.sys, "_MEIPASS", str(tmp_path), raising=False)

    icon = brand.app_icon()
    assert not icon.isNull()
    # The 1x1 sentinel won over the repo's 1024x1024 asset.
    assert icon.availableSizes() == [QSize(1, 1)]


def test_app_icon_falls_back_to_the_repo_asset_when_meipass_has_none(tmp_path, monkeypatch) -> None:
    from clipersal import brand

    monkeypatch.setattr(brand.sys, "frozen", True, raising=False)
    monkeypatch.setattr(brand.sys, "_MEIPASS", str(tmp_path), raising=False)  # no assets/ inside

    icon = brand.app_icon()
    assert not icon.isNull()


def test_app_icon_is_null_when_no_asset_exists(tmp_path, monkeypatch) -> None:
    from clipersal import brand

    monkeypatch.setattr(brand.sys, "frozen", True, raising=False)
    monkeypatch.setattr(brand.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(brand, "_SOURCE_ICON_PATH", tmp_path / "missing" / "icon.png")

    assert brand.app_icon().isNull()
