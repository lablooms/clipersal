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
    # so these tests never touch ffmpeg or a real clip file. Same for the
    # meta line's ffprobe discovery: default to "no ffprobe" (duration
    # omitted); tests that exercise the meta line stub the probe itself.
    monkeypatch.setattr(toast_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(toast_qt.thumbnails, "find_ffprobe", lambda ffmpeg_path: None)


@pytest.fixture(autouse=True)
def clean_live_toasts():
    # Stacking state is module-level -- never let one test's toasts shift
    # another test's geometry math.
    toast_qt._live_toasts.clear()
    yield
    toast_qt._live_toasts.clear()


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


def test_save_toast_is_deleted_on_close(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    # close() only hides a parented widget -- WA_DeleteOnClose is what keeps
    # every save from leaving a permanent hidden child on the MainWindow.
    toast = _make_toast(tmp_path)
    assert toast.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose) is True


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


def test_save_toast_uses_the_given_title(tmp_path: Path) -> None:
    toast = toast_qt.SaveToast(
        ffmpeg_path="ffmpeg",
        clip_path=tmp_path / "screenshot-20260101-000000.png",
        cache_dir=tmp_path / ".thumbnails",
        title="Screenshot saved",
    )

    title_label = toast.findChild(object, "toastTitle")
    assert title_label is not None and title_label.text() == "Screenshot saved"


def test_save_toast_default_title_is_clip_saved(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)

    title_label = toast.findChild(object, "toastTitle")
    assert title_label is not None and title_label.text() == "Clip saved"


def test_png_clip_path_is_shown_directly_without_a_thumbnail_fetch(tmp_path: Path, monkeypatch) -> None:
    # A screenshot's "thumbnail" is the file itself -- loading it directly
    # must not touch ffmpeg (ensure_thumbnail is stubbed to None by the
    # autouse fixture, so a pixmap here proves the direct-load path ran).
    png_path = tmp_path / "screenshot-20260101-000000.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(png_path), "PNG")

    toast = toast_qt.SaveToast(
        ffmpeg_path="ffmpeg",
        clip_path=png_path,
        cache_dir=tmp_path / ".thumbnails",
        title="Screenshot saved",
    )

    assert not toast._thumb_label.pixmap().isNull()


def test_show_save_toast_passes_title_through_and_never_raises(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    class FakeToast:
        def __init__(self, ffmpeg_path, clip_path, cache_dir, parent, title="Clip saved"):
            seen["title"] = title

        def show(self):
            pass

        def start_entrance_animation(self):
            pass

    monkeypatch.setattr(toast_qt, "SaveToast", FakeToast)

    toast_qt.show_save_toast(None, "ffmpeg", tmp_path / "shot.png", tmp_path / ".thumbnails", title="Screenshot saved")

    assert seen["title"] == "Screenshot saved"


# ---- action buttons -----------------------------------------------------------


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Wait for an async (worker-thread) condition. Pumps sendPostedEvents(),
    NOT processEvents() -- see test_main_window_qt.py's _wait_for for why."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.sendPostedEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_open_button_opens_the_file_without_triggering_the_whole_toast_click(tmp_path: Path, monkeypatch) -> None:
    opened_files = []
    opened_folders = []
    monkeypatch.setattr(toast_qt, "open_file", lambda path: opened_files.append(path))
    monkeypatch.setattr(toast_qt, "open_folder", lambda path: opened_folders.append(path))

    toast = _make_toast(tmp_path)
    toast._open_button.click()

    assert opened_files == [toast._clip_path]
    # The button consumed the click -- the whole-toast behavior (open the
    # parent folder + close) must NOT also fire.
    assert opened_folders == []
    assert toast._closed is False


def test_show_in_folder_button_reveals_without_triggering_the_whole_toast_click(tmp_path: Path, monkeypatch) -> None:
    revealed = []
    opened_folders = []
    monkeypatch.setattr(toast_qt, "reveal_in_file_manager", lambda path: revealed.append(path))
    monkeypatch.setattr(toast_qt, "open_folder", lambda path: opened_folders.append(path))

    toast = _make_toast(tmp_path)
    toast._reveal_button.click()

    assert revealed == [toast._clip_path]
    assert opened_folders == []
    assert toast._closed is False


def test_reveal_button_never_raises_when_reveal_fails(tmp_path: Path, monkeypatch) -> None:
    def boom(path):
        raise OSError("no file manager (fake)")

    monkeypatch.setattr(toast_qt, "reveal_in_file_manager", boom)
    toast = _make_toast(tmp_path)
    toast._reveal_button.click()  # log-and-continue, not an exception out of the slot


# ---- stacking -------------------------------------------------------------------


def test_new_toast_offsets_up_per_already_open_toast(tmp_path: Path) -> None:
    toast1 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast1)
    toast2 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast2)

    assert toast1._stack_index == 0
    assert toast2._stack_index == 1
    gap = toast1._final_rect.y() - toast2._final_rect.y()
    assert gap == toast2._final_rect.height() + toast_qt._STACK_GAP


def test_show_save_toast_tracks_live_toasts(tmp_path: Path) -> None:
    toast_qt.show_save_toast(None, "ffmpeg", tmp_path / "clip-1.mp4", tmp_path / ".thumbnails")
    toast_qt.show_save_toast(None, "ffmpeg", tmp_path / "clip-2.mp4", tmp_path / ".thumbnails")
    assert len(toast_qt._live_toasts) == 2


def test_closing_a_toast_shifts_the_survivors_down(tmp_path: Path) -> None:
    toast1 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast1)
    toast2 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast2)
    # Park both at their final positions (their entrance animations are
    # unstarted in this test, so reflow treats them as settled).
    toast1.setGeometry(toast1._final_rect)
    toast2.setGeometry(toast2._final_rect)

    toast_qt._on_toast_destroyed(toast1)

    assert toast_qt._live_toasts == [toast2]
    assert toast2._stack_index == 0
    assert toast2.y() == toast1._final_rect.y()  # dropped into the gap


def test_reflow_skips_a_toast_still_in_its_entrance_animation(tmp_path: Path) -> None:
    toast1 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast1)
    toast2 = _make_toast(tmp_path)
    toast_qt._live_toasts.append(toast2)
    toast2._entrance_animation.start()  # Running -> its animation target owns its geometry

    toast_qt._on_toast_destroyed(toast1)

    assert toast_qt._live_toasts == [toast2]
    assert toast2.y() != toast1._final_rect.y()  # untouched by the reflow
    toast2._entrance_animation.stop()


# ---- meta line ------------------------------------------------------------------


def test_meta_line_shows_duration_and_size(tmp_path: Path, monkeypatch) -> None:
    clip_path = tmp_path / "clip-20260101-000000.mp4"
    clip_path.write_bytes(b"x" * 2048)  # 2.0 KB
    monkeypatch.setattr(toast_qt.thumbnails, "find_ffprobe", lambda ffmpeg_path: "ffprobe")
    monkeypatch.setattr(toast_qt.thumbnails, "get_duration_seconds", lambda ffprobe, path: 65.0)

    toast = toast_qt.SaveToast(ffmpeg_path="ffmpeg", clip_path=clip_path, cache_dir=tmp_path / ".thumbnails")

    assert _wait_for(lambda: toast._meta_label.text() == "1:05 · 2.0 KB")


def test_meta_line_omits_duration_without_ffprobe(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip-20260101-000000.mp4"
    clip_path.write_bytes(b"x" * 1024)

    toast = toast_qt.SaveToast(ffmpeg_path="ffmpeg", clip_path=clip_path, cache_dir=tmp_path / ".thumbnails")

    # fixture: find_ffprobe -> None, so only the size survives.
    assert _wait_for(lambda: toast._meta_label.text() == "1.0 KB")


def test_meta_line_stays_empty_when_nothing_is_known(tmp_path: Path) -> None:
    # No ffprobe (fixture) and the clip itself vanishes before the stat.
    toast = _make_toast(tmp_path)

    assert _wait_for(lambda: toast._meta_label.text() == "" and not toast._closed)
    # Give the worker a beat to deliver; the label must remain empty (no "None").
    QApplication.sendPostedEvents()
    assert "None" not in toast._meta_label.text()


def test_on_meta_ready_ignores_results_after_close(tmp_path: Path) -> None:
    toast = _make_toast(tmp_path)
    toast.close()
    toast._on_meta_ready("1:05 · 2.0 KB")
    assert toast._meta_label.text() == ""


def test_format_duration_and_size_helpers() -> None:
    assert toast_qt._format_duration(65) == "1:05"
    assert toast_qt._format_duration(0) == "0:00"
    assert toast_qt._format_size(2048) == "2.0 KB"
    assert toast_qt._format_size(13_000_000) == "12.4 MB"
