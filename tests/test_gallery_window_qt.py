import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from clipersal import gallery_window_qt
from clipersal.gallery_window_qt import GalleryFrame, _format_size


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_thumbnails(monkeypatch):
    monkeypatch.setattr(gallery_window_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "cleanup_orphaned_thumbnails", lambda *a, **k: None)


def _make_clip(clips_dir: Path, name: str, mtime: float) -> Path:
    clips_dir.mkdir(parents=True, exist_ok=True)
    path = clips_dir / name
    path.write_bytes(b"fake mp4 data")
    os.utime(path, (mtime, mtime))
    return path


def _process_events(condition, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()


def test_format_size_formats_across_units() -> None:
    assert _format_size(500) == "500 B"
    assert _format_size(2048) == "2.0 KB"
    assert _format_size(5 * 1024 * 1024) == "5.0 MB"


def test_empty_clips_dir_shows_empty_label(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    gallery = GalleryFrame("ffmpeg", clips_dir)
    assert gallery._empty_container.isHidden() is False
    assert gallery._rows == {}


def test_lists_clips_sorted_newest_first(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    older = _make_clip(clips_dir, "clip-old.mp4", mtime=1000)
    newer = _make_clip(clips_dir, "clip-new.mp4", mtime=2000)

    gallery = GalleryFrame("ffmpeg", clips_dir)

    assert gallery._empty_container.isHidden() is True
    assert list(gallery._rows.keys()) == [newer, older]


def test_open_folder_button_opens_clips_dir(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    opened = []
    monkeypatch.setattr(gallery_window_qt, "open_folder", lambda path: opened.append(path))

    gallery = GalleryFrame("ffmpeg", clips_dir)

    from PySide6.QtWidgets import QPushButton

    buttons = [b for b in gallery.findChildren(QPushButton) if b.text() == "Open folder"]
    assert len(buttons) == 1
    buttons[0].click()

    assert opened == [clips_dir]


def test_row_open_button_opens_the_clip_itself(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(gallery_window_qt, "open_folder", lambda path: opened.append(path))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].open_button.click()

    assert opened == [clip_path]


def test_row_reveal_button_calls_reveal_in_file_manager(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    revealed = []
    monkeypatch.setattr(gallery_window_qt, "reveal_in_file_manager", lambda path: revealed.append(path))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].reveal_button.click()

    assert revealed == [clip_path]


def test_rename_renames_file_and_refreshes(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip-old-name.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-renamed", True)))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].rename_button.click()

    assert not clip_path.exists()
    assert (clips_dir / "clip-renamed.mp4").exists()
    assert clip_path not in gallery._rows
    assert (clips_dir / "clip-renamed.mp4") in gallery._rows


def test_rename_cancelled_leaves_file_untouched(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].rename_button.click()

    assert clip_path.exists()
    assert clip_path in gallery._rows


def test_delete_confirmed_removes_clip_and_row(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].delete_button.click()

    assert not clip_path.exists()
    assert clip_path not in gallery._rows
    assert gallery._empty_container.isHidden() is False


def test_delete_declined_keeps_clip(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._rows[clip_path].delete_button.click()

    assert clip_path.exists()
    assert clip_path in gallery._rows


def test_apply_thumbnail_sets_pixmap_for_known_clip(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")

    gallery = GalleryFrame("ffmpeg", clips_dir)
    gallery._apply_thumbnail(clip_path, image_path)

    assert not gallery._rows[clip_path].thumb_label.pixmap().isNull()


def test_apply_thumbnail_ignores_unknown_clip_path(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    gallery = GalleryFrame("ffmpeg", clips_dir)
    # Should not raise even though this clip path was never added as a row.
    gallery._apply_thumbnail(tmp_path / "nonexistent.mp4", tmp_path / "thumb.png")


def test_refresh_worker_delivers_thumbnails_via_signal(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")

    monkeypatch.setattr(gallery_window_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: image_path)

    gallery = GalleryFrame("ffmpeg", clips_dir)
    _process_events(lambda: not gallery._rows[clip_path].thumb_label.pixmap().isNull())

    assert not gallery._rows[clip_path].thumb_label.pixmap().isNull()
