import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from clipersal import gallery_window_qt
from clipersal.gallery_window_qt import GalleryFrame, TrimDialog, _format_size, parse_timestamp


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_thumbnails(monkeypatch):
    monkeypatch.setattr(gallery_window_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "cleanup_orphaned_thumbnails", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_real_trim_workers(monkeypatch):
    """TrimDialog probes the clip's duration and grabs its preview frames
    on background threads; stub both so tests drive the delivered slots
    directly (and no real ffmpeg/ffprobe subprocess ever runs). The trim
    worker method itself is deliberately NOT stubbed -- the trim tests
    exercise it with a fake concat.trim_clip."""
    monkeypatch.setattr(gallery_window_qt._TrimWorker, "probe_duration", lambda self: None)
    monkeypatch.setattr(gallery_window_qt._TrimWorker, "grab_preview", lambda self, which, offset: None)


def _make_clip(clips_dir: Path, name: str, mtime: float) -> Path:
    clips_dir.mkdir(parents=True, exist_ok=True)
    path = clips_dir / name
    path.write_bytes(b"fake mp4 data")
    os.utime(path, (mtime, mtime))
    return path


def _make_gallery(clips_dir: Path) -> GalleryFrame:
    """GalleryFrame takes a live clips-dir provider (see its __init__); a
    fixed-value lambda keeps these tests on the folder-never-changes path."""
    return GalleryFrame("ffmpeg", lambda: clips_dir)


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
    gallery = _make_gallery(clips_dir)
    assert gallery._empty_container.isHidden() is False
    assert gallery._rows == {}


def test_lists_clips_sorted_newest_first(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    older = _make_clip(clips_dir, "clip-old.mp4", mtime=1000)
    newer = _make_clip(clips_dir, "clip-new.mp4", mtime=2000)

    gallery = _make_gallery(clips_dir)

    assert gallery._empty_container.isHidden() is True
    assert list(gallery._rows.keys()) == [newer, older]


def test_refresh_skips_a_clip_that_vanishes_between_glob_and_stat(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    survivor = _make_clip(clips_dir, "clip-survivor.mp4", mtime=1000)
    ghost = _make_clip(clips_dir, "clip-ghost.mp4", mtime=2000)
    real_stat = Path.stat

    def stat_that_deletes_ghost(self, *args, **kwargs):
        # The retention sweep (on the IPC thread) or an external delete can
        # remove a clip after refresh()'s glob but before its sort-key stat.
        if self == ghost:
            ghost.unlink()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_deletes_ghost)

    gallery = _make_gallery(clips_dir)  # must not raise FileNotFoundError

    assert list(gallery._rows.keys()) == [survivor]


def test_refresh_reads_the_providers_current_dir(tmp_path: Path) -> None:
    dir_a = tmp_path / "clips-a"
    dir_b = tmp_path / "clips-b"
    clip_a = _make_clip(dir_a, "clip-a.mp4", mtime=1000)
    clip_b = _make_clip(dir_b, "clip-b.mp4", mtime=2000)
    current = {"clips_dir": dir_a}

    gallery = GalleryFrame("ffmpeg", lambda: current["clips_dir"])
    assert list(gallery._rows.keys()) == [clip_a]

    # A Settings clips-folder change must be picked up on the next refresh --
    # no frozen Path captured at construction.
    current["clips_dir"] = dir_b
    gallery.refresh()
    assert list(gallery._rows.keys()) == [clip_b]


def test_open_folder_button_opens_clips_dir(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    opened = []
    monkeypatch.setattr(gallery_window_qt, "open_folder", lambda path: opened.append(path))

    gallery = _make_gallery(clips_dir)

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

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].open_button.click()

    assert opened == [clip_path]


def test_row_reveal_button_calls_reveal_in_file_manager(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    revealed = []
    monkeypatch.setattr(gallery_window_qt, "reveal_in_file_manager", lambda path: revealed.append(path))

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].reveal_button.click()

    assert revealed == [clip_path]


def test_rename_renames_file_and_refreshes(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip-old-name.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-renamed", True)))

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].rename_button.click()

    assert not clip_path.exists()
    assert (clips_dir / "clip-renamed.mp4").exists()
    assert clip_path not in gallery._rows
    assert (clips_dir / "clip-renamed.mp4") in gallery._rows


def test_rename_cancelled_leaves_file_untouched(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].rename_button.click()

    assert clip_path.exists()
    assert clip_path in gallery._rows


def test_rename_onto_existing_clip_is_refused(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip-a.mp4", mtime=1000)
    other_path = _make_clip(clips_dir, "clip-b.mp4", mtime=2000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-b", True)))
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok)
    )

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].rename_button.click()

    # Path.rename() would silently replace the destination on POSIX (only
    # Windows raises FileExistsError) -- the up-front refusal must keep BOTH
    # files, on every platform.
    assert clip_path.exists()
    assert other_path.exists()
    assert len(warnings) == 1
    assert clip_path in gallery._rows


@pytest.mark.parametrize("bad_name", ["sub/dir", "sub\\dir"])
def test_rename_with_path_separator_is_rejected_without_raising(tmp_path: Path, monkeypatch, bad_name: str) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (bad_name, True)))
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok)
    )

    gallery = _make_gallery(clips_dir)
    # with_name() would raise an uncaught ValueError here -- the name must be
    # rejected up front instead.
    gallery._rows[clip_path].rename_button.click()

    assert clip_path.exists()
    assert len(warnings) == 1
    assert clip_path in gallery._rows


def test_delete_confirmed_removes_clip_and_row(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].delete_button.click()

    assert not clip_path.exists()
    assert clip_path not in gallery._rows
    assert gallery._empty_container.isHidden() is False


def test_delete_declined_keeps_clip(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))

    gallery = _make_gallery(clips_dir)
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

    gallery = _make_gallery(clips_dir)
    gallery._apply_thumbnail(clip_path, image_path)

    assert not gallery._rows[clip_path].thumb_label.pixmap().isNull()


def test_apply_thumbnail_ignores_unknown_clip_path(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    gallery = _make_gallery(clips_dir)
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

    gallery = _make_gallery(clips_dir)
    _process_events(lambda: not gallery._rows[clip_path].thumb_label.pixmap().isNull())

    assert not gallery._rows[clip_path].thumb_label.pixmap().isNull()


# ---- parse_timestamp (trim dialog's Start/End fields) ----------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", 0.0),
        ("90", 90.0),
        ("90.5", 90.5),
        (".5", 0.5),
        ("0:05", 5.0),
        ("1:30", 90.0),
        ("1:30.5", 90.5),
        ("10:00", 600.0),
        ("00:00.0", 0.0),
        (" 1:15 ", 75.0),
    ],
)
def test_parse_timestamp_accepts_plain_seconds_and_mm_ss(text: str, expected: float) -> None:
    assert parse_timestamp(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "1:2:3", "1:", ":30", "1:60", "1:75", "1:-5", "-5", "inf", "nan", "0x10"],
)
def test_parse_timestamp_rejects_invalid_or_out_of_range(text: str) -> None:
    assert parse_timestamp(text) is None


# ---- TrimDialog ------------------------------------------------------------


def _make_trim_dialog(clips_dir: Path, clip_path: Path, gallery: GalleryFrame, duration=120.0) -> TrimDialog:
    """Build a TrimDialog and drive the duration slot directly (the probe
    worker itself is stubbed by no_real_trim_workers, so nothing arrives
    asynchronously unless a test emits it)."""
    dialog = TrimDialog("ffmpeg", clip_path, clips_dir, gallery)
    dialog._on_duration_ready(duration)
    return dialog


def test_trim_dialog_disables_trim_when_start_not_before_end(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    dialog = _make_trim_dialog(clips_dir, clip_path, gallery)

    dialog.start_field.setText("1:00")
    dialog.end_field.setText("0:30")

    assert dialog.trim_button.isEnabled() is False
    assert "before" in dialog._error_label.text()
    assert dialog._result_label.text() == "Result: --"


def test_trim_dialog_enables_trim_for_a_valid_range(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    dialog = _make_trim_dialog(clips_dir, clip_path, gallery)

    dialog.start_field.setText("0:10")
    dialog.end_field.setText("1:30")

    assert dialog.trim_button.isEnabled() is True
    assert dialog._error_label.text() == ""
    assert dialog._result_label.text() == "Result: 1:20.0"


def test_trim_dialog_rejects_end_beyond_duration(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    dialog = _make_trim_dialog(clips_dir, clip_path, gallery, duration=120.0)

    dialog.end_field.setText("3:00")

    assert dialog.trim_button.isEnabled() is False
    assert "duration" in dialog._error_label.text()


def test_trim_dialog_without_duration_disables_trim(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    dialog = _make_trim_dialog(clips_dir, clip_path, gallery, duration=None)

    dialog.start_field.setText("0:01")
    dialog.end_field.setText("0:02")

    assert dialog.trim_button.isEnabled() is False
    assert "duration" in dialog._error_label.text()


def test_row_trim_button_opens_the_trim_dialog(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(TrimDialog, "exec", lambda self: opened.append(self))

    gallery = _make_gallery(clips_dir)
    gallery._rows[clip_path].trim_button.click()

    assert len(opened) == 1
    assert opened[0]._clip_path == clip_path


def test_trim_success_refreshes_gallery(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    trim_calls = []

    def fake_trim(ffmpeg_path, clip, start, end, out_dir, duration_seconds=None):
        trim_calls.append((start, end, duration_seconds))
        output = out_dir / "clip-trimmed.mp4"
        output.write_bytes(b"trimmed data")
        return output

    monkeypatch.setattr(gallery_window_qt.concat, "trim_clip", fake_trim)
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a) or QMessageBox.StandardButton.Ok)
    )

    gallery = _make_gallery(clips_dir)
    dialog = TrimDialog("ffmpeg", clip_path, clips_dir, gallery)
    dialog.trim_succeeded.connect(gallery._on_trim_succeeded)
    dialog._on_duration_ready(120.0)
    dialog.start_field.setText("0:01")
    dialog.end_field.setText("0:02")

    dialog.trim_button.click()

    trimmed = clips_dir / "clip-trimmed.mp4"
    _process_events(lambda: trimmed in gallery._rows)

    assert trim_calls == [(1.0, 2.0, 120.0)]  # parsed fields + probed duration handed to trim_clip
    assert trimmed in gallery._rows
    assert clip_path in gallery._rows  # the original clip is still listed
    assert len(infos) == 1
    assert "clip-trimmed.mp4" in infos[0][2]


def test_trim_failure_shows_the_error_inline(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    def failing_trim(*args, **kwargs):
        raise gallery_window_qt.concat.ConcatFailedError("ffmpeg trim failed:\nsome ffmpeg stderr")

    monkeypatch.setattr(gallery_window_qt.concat, "trim_clip", failing_trim)

    gallery = _make_gallery(clips_dir)
    dialog = TrimDialog("ffmpeg", clip_path, clips_dir, gallery)
    dialog.trim_succeeded.connect(gallery._on_trim_succeeded)
    dialog._on_duration_ready(120.0)
    dialog.start_field.setText("0:01")
    dialog.end_field.setText("0:02")

    dialog.trim_button.click()

    _process_events(lambda: bool(dialog._error_label.text()))

    assert "ffmpeg trim failed" in dialog._error_label.text()
    # The dialog stays usable: fields re-enabled, Trim re-armed, no new clip.
    assert dialog.trim_button.isEnabled() is True
    assert dialog._result_label.text() == "Result: 0:01.0"
    assert list(gallery._rows.keys()) == [clip_path]
