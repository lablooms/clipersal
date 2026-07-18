import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from clipersal import toast_qt


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_thumbnail_fetch(monkeypatch):
    # Every SaveToast spins up a background thread calling
    # thumbnails.ensure_thumbnail (a real ffmpeg subprocess call) -- stub it
    # so these tests never touch ffmpeg or a real clip file.
    monkeypatch.setattr(toast_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: None)


def _make_toast(tmp_path: Path, monkeypatch=None) -> toast_qt.SaveToast:
    return toast_qt.SaveToast(
        ffmpeg_path="ffmpeg",
        clip_path=tmp_path / "clip-20260101-000000.mp4",
        cache_dir=tmp_path / ".thumbnails",
    )


def test_save_toast_constructs_with_correct_object_names(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)
    assert toast._final_rect.width() == toast_qt._TOAST_WIDTH
    assert toast.findChild(object, "card") is not None
    assert toast.findChild(object, "toastTitle") is not None
    assert toast.findChild(object, "hint") is not None
    assert toast.findChild(object, "thumbPlaceholder") is not None


def test_save_toast_is_frameless_and_stays_on_top(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    toast = _make_toast(tmp_path)
    flags = toast.windowFlags()
    assert bool(flags & Qt.WindowType.FramelessWindowHint)
    assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)


def test_save_toast_final_geometry_within_available_screen_geometry(tmp_path: Path) -> None:
    from PySide6.QtGui import QGuiApplication

    toast = _make_toast(tmp_path)
    screen_rect = QGuiApplication.primaryScreen().availableGeometry()
    final_rect = toast._final_rect
    assert final_rect.right() <= screen_rect.right()
    assert final_rect.bottom() <= screen_rect.bottom()
    assert final_rect.width() == toast_qt._TOAST_WIDTH


def test_save_toast_starts_small_and_transparent_before_entrance_animation(tmp_path: Path) -> None:
    # The "bud opening" entrance animates from this state to _final_geometry()
    # at full opacity -- constructing the toast sets the starting state but
    # does not itself start the animation (see start_entrance_animation()).
    toast = _make_toast(tmp_path)
    assert toast.windowOpacity() == 0.0
    assert toast.geometry().width() <= 4
    assert toast.geometry().height() <= 4


def test_entrance_animation_targets_final_geometry_and_full_opacity(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)
    final_rect = toast._final_rect

    geometry_anim, opacity_anim = toast._entrance_animation.animationAt(0), toast._entrance_animation.animationAt(1)
    assert geometry_anim.endValue() == final_rect
    assert opacity_anim.startValue() == 0.0
    assert opacity_anim.endValue() == 1.0


def test_start_entrance_animation_actually_starts_it(tmp_path: Path) -> None:
    from PySide6.QtCore import QAbstractAnimation

    toast = _make_toast(tmp_path)
    toast.show()
    toast.start_entrance_animation()
    assert toast._entrance_animation.state() == QAbstractAnimation.State.Running


def test_entrance_animation_finish_repins_final_fixed_size(tmp_path: Path) -> None:
    # setFixedWidth() in __init__ pins min==max width, which would clamp
    # every setGeometry() call in the animation straight back to full size
    # unless that constraint is lifted first -- regression coverage for that.
    toast = _make_toast(tmp_path)
    final_rect = toast._final_rect

    toast.setGeometry(toast.geometry().x(), toast.geometry().y(), 4, 4)
    assert toast.geometry().width() == 4  # constraint was lifted, so this isn't clamped back to 300

    toast._entrance_animation.finished.emit()
    assert toast.minimumWidth() == final_rect.width()
    assert toast.maximumWidth() == final_rect.width()


def test_left_click_opens_folder_and_closes(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt as QtCore_Qt

    opened = []
    monkeypatch.setattr(toast_qt, "open_folder", lambda path: opened.append(path))

    toast = _make_toast(tmp_path)
    toast.show()

    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        QtCore_Qt.MouseButton.LeftButton,
        QtCore_Qt.MouseButton.LeftButton,
        QtCore_Qt.KeyboardModifier.NoModifier,
    )
    toast.mousePressEvent(event)

    assert opened == [toast._clip_path.parent]
    assert toast._closed is True


def test_on_thumbnail_ready_sets_pixmap_for_valid_image(tmp_path: Path) -> None:
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")

    toast = _make_toast(tmp_path)
    toast._on_thumbnail_ready(image_path)

    assert not toast._thumb_label.pixmap().isNull()


def test_on_thumbnail_ready_ignores_none(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)
    toast._on_thumbnail_ready(None)
    assert toast._thumb_label.pixmap() is None or toast._thumb_label.pixmap().isNull()


def test_on_thumbnail_ready_ignores_invalid_path(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)
    toast._on_thumbnail_ready(tmp_path / "does-not-exist.png")
    assert toast._thumb_label.pixmap() is None or toast._thumb_label.pixmap().isNull()


def test_on_thumbnail_ready_ignores_result_after_close(tmp_path: Path) -> None:
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")

    toast = _make_toast(tmp_path)
    toast.close()
    toast._on_thumbnail_ready(image_path)

    assert toast._thumb_label.pixmap() is None or toast._thumb_label.pixmap().isNull()


def test_show_save_toast_never_raises_on_internal_failure(tmp_path: Path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(toast_qt, "SaveToast", boom)

    # Must not raise -- a toast failure must never break the save it celebrates.
    toast_qt.show_save_toast(None, "ffmpeg", tmp_path / "clip.mp4", tmp_path / ".thumbnails")


def test_thumbnail_fetcher_emits_result_from_background_thread(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(toast_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: tmp_path / "found.png")

    fetcher = toast_qt._ThumbnailFetcher("ffmpeg", tmp_path / "clip.mp4", tmp_path / ".thumbnails")
    received = []
    fetcher.ready.connect(received.append)

    import threading

    thread = threading.Thread(target=fetcher.fetch)
    thread.start()
    thread.join(timeout=2)

    # Process the queued cross-thread signal delivery on the GUI thread.
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        QApplication.processEvents()

    assert received == [tmp_path / "found.png"]
