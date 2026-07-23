import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from clipersal import clip_metadata, gallery_window_qt
from clipersal.gallery_window_qt import (
    EMPTY_CLIPS_MESSAGE,
    WINDOW_FILTER_ALL,
    ClipCard,
    ClipDetailsDialog,
    CompressDialog,
    GalleryFrame,
    GifExportDialog,
    _format_size,
    window_name_from_clip_name,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_thumbnails(monkeypatch):
    monkeypatch.setattr(gallery_window_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "cleanup_orphaned_thumbnails", lambda *a, **k: None)
    # Never a real ffprobe subprocess either: the gallery's worker probes
    # durations when find_ffprobe returns a path, so it stays None here and
    # the duration tests install their own fakes on top.
    monkeypatch.setattr(gallery_window_qt.thumbnails, "find_ffprobe", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_real_video_info_probes(monkeypatch):
    """ClipDetailsDialog ensures its thumbnail and probes duration/resolution
    on a worker thread; stub the run so tests drive _on_thumbnail_ready /
    _on_info_ready directly -- same pattern as the thumbnail stubs above."""
    monkeypatch.setattr(gallery_window_qt._VideoInfoWorker, "run", lambda self: None)


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
    # sendPostedEvents, not processEvents: queued cross-thread signal
    # deliveries are what the workers need pumped, and processEvents also
    # fires TIMERS -- leftover windows from earlier tests have STATUS timers
    # that each do a real (refused, slow) socket connect, which can blow the
    # 2 s deadline and read as a flake when several files run in one process.
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.sendPostedEvents()


def test_format_size_formats_across_units() -> None:
    assert _format_size(500) == "500 B"
    assert _format_size(2048) == "2.0 KB"
    assert _format_size(5 * 1024 * 1024) == "5.0 MB"


def test_empty_clips_dir_shows_empty_label(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    gallery = _make_gallery(clips_dir)
    assert gallery._empty_container.isHidden() is False
    assert gallery._empty_label.text() == EMPTY_CLIPS_MESSAGE
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


def test_row_play_button_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    gallery._rows[clip_path].play_button.click()

    assert len(fake_player.instances) == 1
    assert fake_player.instances[0].clip_path == clip_path


def test_context_menu_reveal_action_calls_reveal_in_file_manager(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    revealed = []
    monkeypatch.setattr(gallery_window_qt, "reveal_in_file_manager", lambda path: revealed.append(path))

    gallery = _make_gallery(clips_dir)
    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Reveal in folder").trigger()

    assert revealed == [clip_path]


def test_rename_renames_file_and_refreshes(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip-old-name.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-renamed", True)))

    gallery = _make_gallery(clips_dir)
    gallery._do_rename(clip_path)

    assert not clip_path.exists()
    assert (clips_dir / "clip-renamed.mp4").exists()
    assert clip_path not in gallery._rows
    assert (clips_dir / "clip-renamed.mp4") in gallery._rows


def test_rename_cancelled_leaves_file_untouched(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))

    gallery = _make_gallery(clips_dir)
    gallery._do_rename(clip_path)

    assert clip_path.exists()
    assert clip_path in gallery._rows


def test_rename_onto_existing_clip_is_refused(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip-a.mp4", mtime=1000)
    other_path = _make_clip(clips_dir, "clip-b.mp4", mtime=2000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-b", True)))
    warnings = []
    monkeypatch.setattr(
        gallery_window_qt, "quiet_message", lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok
    )

    gallery = _make_gallery(clips_dir)
    gallery._do_rename(clip_path)

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
        gallery_window_qt, "quiet_message", lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok
    )

    gallery = _make_gallery(clips_dir)
    # with_name() would raise an uncaught ValueError here -- the name must be
    # rejected up front instead.
    gallery._do_rename(clip_path)

    assert clip_path.exists()
    assert len(warnings) == 1
    assert clip_path in gallery._rows


def test_delete_confirmed_removes_clip_and_row(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)

    gallery = _make_gallery(clips_dir)
    gallery._do_delete(clip_path)

    assert not clip_path.exists()
    assert clip_path not in gallery._rows
    assert gallery._empty_container.isHidden() is False


def test_delete_declined_keeps_clip(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.No)

    gallery = _make_gallery(clips_dir)
    gallery._do_delete(clip_path)

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


# ---- header controls: search / sort / favorites-first ----------------------


def _row_order(gallery: GalleryFrame) -> list[Path]:
    """The visible rows' actual layout order (what the user sees
    top-to-bottom), skipping search-hidden rows, the trailing stretch, and
    any non-row items."""
    order = []
    for i in range(gallery._list_layout.count()):
        widget = gallery._list_layout.itemAt(i).widget()
        if isinstance(widget, gallery_window_qt.ClipRow) and not widget.isHidden():
            order.append(widget.clip_path)
    return order


def _set_sort(gallery: GalleryFrame, key: str) -> None:
    gallery.sort_combo.setCurrentIndex(gallery.sort_combo.findData(key))


def test_search_filters_rows_case_insensitively(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    alpha = _make_clip(clips_dir, "Alpha-bloom.mp4", mtime=1000)
    beta = _make_clip(clips_dir, "beta-run.mp4", mtime=2000)

    gallery = _make_gallery(clips_dir)
    gallery.search_edit.setText("alpha")

    assert gallery._rows[alpha].isHidden() is False
    assert gallery._rows[beta].isHidden() is True
    # Filtering never destroys rows or lies about the folder's contents.
    assert set(gallery._rows.keys()) == {alpha, beta}
    assert gallery._empty_container.isHidden() is True
    assert gallery.footer_label.text() == "2 clips  ·  26 B  ·  0 favorites"

    gallery.search_edit.setText("")  # clearing restores every row
    assert gallery._rows[alpha].isHidden() is False
    assert gallery._rows[beta].isHidden() is False


def test_search_with_no_matches_shows_a_match_specific_empty_message(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    gallery.search_edit.setText("zzz-no-such-clip")

    assert gallery._empty_container.isHidden() is False
    assert "match" in gallery._empty_label.text()


def test_sort_combo_offers_the_five_spec_modes(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    gallery = _make_gallery(clips_dir)

    keys = [gallery.sort_combo.itemData(i) for i in range(gallery.sort_combo.count())]
    labels = [gallery.sort_combo.itemText(i) for i in range(gallery.sort_combo.count())]
    assert keys == ["newest", "oldest", "name", "largest", "window"]
    assert labels == ["Newest first", "Oldest first", "Name A–Z", "Largest first", "Window A–Z"]


def test_sort_oldest_first_reorders_rows(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    older = _make_clip(clips_dir, "clip-old.mp4", mtime=1000)
    newer = _make_clip(clips_dir, "clip-new.mp4", mtime=2000)

    gallery = _make_gallery(clips_dir)
    assert _row_order(gallery) == [newer, older]  # default: newest first

    _set_sort(gallery, "oldest")
    assert _row_order(gallery) == [older, newer]


def test_sort_by_name_a_to_z(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    bee = _make_clip(clips_dir, "b-clip.mp4", mtime=2000)
    ay = _make_clip(clips_dir, "a-clip.mp4", mtime=1000)

    gallery = _make_gallery(clips_dir)
    _set_sort(gallery, "name")

    assert _row_order(gallery) == [ay, bee]


def test_sort_by_largest_first_uses_cached_sizes(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    small = _make_clip(clips_dir, "clip-small.mp4", mtime=2000)
    big = clips_dir / "clip-big.mp4"
    big.write_bytes(b"x" * 4096)
    os.utime(big, (1000, 1000))

    gallery = _make_gallery(clips_dir)
    _set_sort(gallery, "largest")

    assert _row_order(gallery) == [big, small]


def test_sort_applies_on_top_of_search_filtering(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    older_match = _make_clip(clips_dir, "match-old.mp4", mtime=1000)
    newer_match = _make_clip(clips_dir, "match-new.mp4", mtime=2000)
    _make_clip(clips_dir, "other.mp4", mtime=3000)

    gallery = _make_gallery(clips_dir)
    gallery.search_edit.setText("match")
    _set_sort(gallery, "oldest")

    assert _row_order(gallery) == [older_match, newer_match]


def test_favorites_first_floats_favorites_within_the_current_sort(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    one = _make_clip(clips_dir, "clip-1.mp4", mtime=1000)
    two = _make_clip(clips_dir, "clip-2.mp4", mtime=2000)
    three = _make_clip(clips_dir, "clip-3.mp4", mtime=3000)
    clip_metadata.set_favorite(clips_dir, one.name, True)

    gallery = _make_gallery(clips_dir)
    assert _row_order(gallery) == [three, two, one]  # favorite not floated yet

    gallery.favorites_first_switch.setChecked(True)
    assert _row_order(gallery)[0] == one
    assert _row_order(gallery)[1:] == [three, two]  # the rest keep newest-first order

    gallery.favorites_first_switch.setChecked(False)
    assert _row_order(gallery) == [three, two, one]


def test_unfavoriting_with_favorites_first_on_sinks_the_row_back(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    fav = _make_clip(clips_dir, "clip-fav.mp4", mtime=1000)
    other = _make_clip(clips_dir, "clip-other.mp4", mtime=2000)

    gallery = _make_gallery(clips_dir)
    gallery.favorites_first_switch.setChecked(True)
    gallery._rows[fav].favorite_button.click()  # heart on -> floats to the top
    assert _row_order(gallery) == [fav, other]

    gallery._rows[fav].favorite_button.click()  # heart off -> sinks back
    assert _row_order(gallery) == [other, fav]


# ---- selection mode + batch delete ------------------------------------------


def test_selection_mode_toggles_checkboxes_and_leaving_clears_selection(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    assert row.select_checkbox.isHidden() is True
    # Outside selection mode the footer shows its normal stats line.
    assert gallery._footer_stack.currentWidget() is gallery._footer_normal_page

    gallery.select_button.click()
    assert gallery._selection_mode is True
    assert gallery.select_button.isChecked() is True
    assert row.select_checkbox.isHidden() is False
    # ...and in selection mode the footer swaps to the action bar -- the
    # strip itself never moves or resizes (its height is pinned).
    assert gallery._footer_stack.currentWidget() is gallery._selection_bar
    assert gallery.select_button.text() == "Select"

    row.select_checkbox.setChecked(True)
    assert gallery._selected == {clip_path}
    assert gallery._selected_count_label.text() == "1 selected"
    assert gallery.delete_selected_button.text() == "Delete selected (1)"
    assert gallery.delete_selected_button.isEnabled() is True

    gallery.select_button.click()  # leave selection mode
    assert gallery._selection_mode is False
    assert gallery.select_button.isChecked() is False
    assert row.select_checkbox.isHidden() is True
    assert gallery._selected == set()
    assert gallery._selected_count_label.text() == "0 selected"
    assert gallery.delete_selected_button.text() == "Delete selected (0)"
    assert gallery.delete_selected_button.isEnabled() is False
    assert gallery.select_button.text() == "Select"
    assert gallery._footer_stack.currentWidget() is gallery._footer_normal_page


def test_selection_mode_footer_height_is_pinned_so_the_list_never_moves(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    # A fixed min==max height is what guarantees the content swap (stats
    # line <-> action bar) can never push the clip list up or down.
    assert gallery._footer_stack.minimumHeight() == gallery._footer_stack.maximumHeight()
    gallery.select_button.click()
    assert gallery._footer_stack.minimumHeight() == gallery._footer_stack.maximumHeight()


def test_selection_bar_done_button_exits_selection_mode(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery._rows[clip_path].select_checkbox.setChecked(True)
    assert gallery._selection_mode is True

    gallery.done_selecting_button.click()

    assert gallery._selection_mode is False
    assert gallery.select_button.isChecked() is False
    assert gallery._selected == set()
    assert gallery._footer_stack.currentWidget() is gallery._footer_normal_page


def test_select_all_selects_only_visible_rows(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    shown = _make_clip(clips_dir, "clip-shown.mp4", mtime=1000)
    _make_clip(clips_dir, "other.mp4", mtime=2000)
    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery.search_edit.setText("shown")  # hides "other.mp4"

    gallery.select_all_button.click()
    assert gallery._selected == {shown}

    gallery.select_none_button.click()
    assert gallery._selected == set()
    assert gallery._rows[shown].select_checkbox.isChecked() is False


def test_batch_delete_confirmed_deletes_selected_clips(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    keep = _make_clip(clips_dir, "clip-keep.mp4", mtime=1000)
    drop1 = _make_clip(clips_dir, "clip-drop-1.mp4", mtime=2000)
    drop2 = _make_clip(clips_dir, "clip-drop-2.mp4", mtime=3000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)

    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery._rows[drop1].select_checkbox.setChecked(True)
    gallery._rows[drop2].select_checkbox.setChecked(True)
    assert gallery.delete_selected_button.text() == "Delete selected (2)"

    gallery.delete_selected_button.click()

    assert keep.exists()
    assert not drop1.exists()
    assert not drop2.exists()
    # Rows are removed in place -- no full refresh needed or wanted.
    assert list(gallery._rows.keys()) == [keep]
    assert gallery._selected == set()
    assert gallery.delete_selected_button.text() == "Delete selected (0)"
    assert gallery.footer_label.text() == "1 clip  ·  13 B  ·  0 favorites"


def test_batch_delete_declined_keeps_everything(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    one = _make_clip(clips_dir, "clip-1.mp4", mtime=1000)
    two = _make_clip(clips_dir, "clip-2.mp4", mtime=2000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.No)

    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery._rows[one].select_checkbox.setChecked(True)
    gallery._rows[two].select_checkbox.setChecked(True)
    gallery.delete_selected_button.click()

    assert one.exists() and two.exists()
    assert set(gallery._rows.keys()) == {one, two}
    # A declined confirm leaves the selection armed, not silently cleared.
    assert gallery._selected == {one, two}


def test_batch_delete_tolerates_an_unlink_oserror(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    stuck = _make_clip(clips_dir, "clip-stuck.mp4", mtime=1000)
    gone = _make_clip(clips_dir, "clip-gone.mp4", mtime=2000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    real_unlink = Path.unlink

    def unlink_that_fails_for_stuck(self, *args, **kwargs):
        # A locked/already-vanished file must log + skip, never abort the
        # rest of the batch (the _do_delete convention, applied per file).
        if self == stuck:
            raise OSError("simulated lock")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_that_fails_for_stuck)

    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery._rows[stuck].select_checkbox.setChecked(True)
    gallery._rows[gone].select_checkbox.setChecked(True)
    gallery.delete_selected_button.click()  # must not raise

    assert stuck.exists()
    assert not gone.exists()
    assert stuck in gallery._rows  # the failed file keeps its row...
    assert gone not in gallery._rows
    assert gallery._selected == {stuck}  # ...and stays selected
    assert gallery.footer_label.text() == "1 clip  ·  13 B  ·  0 favorites"


def test_batch_delete_of_a_favorited_clip_still_works(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    fav = _make_clip(clips_dir, "clip-fav.mp4", mtime=1000)
    clip_metadata.set_favorite(clips_dir, fav.name, True)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)

    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    gallery._rows[fav].select_checkbox.setChecked(True)
    gallery.delete_selected_button.click()

    assert not fav.exists()
    assert gallery._rows == {}
    # The orphaned sidecar entry is cleaned by prune on the next refresh.
    gallery.refresh()
    assert clip_metadata.load_metadata(clips_dir) == {}


# ---- context menu -------------------------------------------------------------


def _menu_actions(menu) -> list:
    return [action for action in menu.actions() if not action.isSeparator()]


def test_context_menu_has_all_actions_in_order(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)

    assert [a.text() for a in _menu_actions(menu)] == [
        "Play",
        "Open",
        "Reveal in folder",
        "Favorite",
        "Details…",
        "Rename…",
        "Trim…",
        "Export as GIF…",
        "Compress…",
        "Copy path",
        "Copy filename",
        "Delete",
    ]


def test_context_menu_open_reuses_the_row_open_handler(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(gallery_window_qt, "open_folder", lambda path: opened.append(path))
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Open").trigger()

    assert opened == [clip_path]


def test_context_menu_favorite_action_reflects_and_updates_state(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    favorite_action = next(a for a in _menu_actions(menu) if a.text() == "Favorite")
    assert favorite_action.isCheckable() is True
    assert favorite_action.isChecked() is False

    favorite_action.trigger()  # checkable -> toggles to checked

    assert clip_metadata.is_favorite(clips_dir, clip_path.name) is True
    assert gallery._rows[clip_path].favorite_button.isChecked() is True
    assert clip_path.name in gallery._favorites

    # A freshly built menu reflects the new persisted state.
    menu2 = gallery._build_context_menu(clip_path)
    favorite_action2 = next(a for a in _menu_actions(menu2) if a.text() == "Favorite")
    assert favorite_action2.isChecked() is True


def test_context_menu_copy_path_puts_the_path_on_the_clipboard(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Copy path").trigger()

    assert QGuiApplication.clipboard().text() == str(clip_path)


# ---- favorite heart -------------------------------------------------------------


def test_heart_toggle_persists_via_real_clip_metadata(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]

    assert row.favorite_button.objectName() == "heartButton"
    assert row.favorite_button.isChecked() is False
    assert row.favorite_button.text() == "♡"

    row.favorite_button.click()
    assert row.favorite_button.isChecked() is True
    assert row.favorite_button.text() == "♥"
    assert clip_metadata.is_favorite(clips_dir, clip_path.name) is True

    # A fresh gallery (a real refresh cycle against the real sidecar) reads
    # the state back onto the heart.
    gallery2 = _make_gallery(clips_dir)
    assert gallery2._rows[clip_path].favorite_button.isChecked() is True

    gallery2._rows[clip_path].favorite_button.click()
    assert clip_metadata.is_favorite(clips_dir, clip_path.name) is False
    assert gallery2._rows[clip_path].favorite_button.text() == "♡"


def test_heart_glyph_renders_under_the_offscreen_platform(tmp_path: Path) -> None:
    # Smoke test: grab() exercises real painting in both heart states -- the
    # ♡/♥ glyphs must not crash the offscreen platform (no pixel assertions).
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]

    row.show()
    assert not row.favorite_button.grab().isNull()
    row.favorite_button.click()
    assert row.favorite_button.text() == "♥"
    assert not row.favorite_button.grab().isNull()
    row.close()


def test_deleting_a_favorited_clip_prunes_the_sidecar_on_next_refresh(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    clip_metadata.set_favorite(clips_dir, clip_path.name, True)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)

    gallery = _make_gallery(clips_dir)
    gallery._do_delete(clip_path)

    assert not clip_path.exists()
    gallery.refresh()  # prune runs once per refresh
    assert clip_metadata.load_metadata(clips_dir) == {}


# ---- duration in the meta line -------------------------------------------------


def test_duration_probe_updates_the_meta_line(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "find_ffprobe", lambda *a, **k: "ffprobe")
    monkeypatch.setattr(gallery_window_qt.thumbnails, "get_duration_seconds", lambda *a, **k: 65.0)

    gallery = _make_gallery(clips_dir)
    meta = gallery._rows[clip_path].meta_label
    _process_events(lambda: "1:05" in meta.text())

    assert "1:05" in meta.text()
    assert "13 B" in meta.text()  # the size segment stays


def test_duration_probe_none_leaves_the_meta_line_unchanged(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "find_ffprobe", lambda *a, **k: "ffprobe")
    probe_called = threading.Event()

    def fake_probe(*args, **kwargs):
        probe_called.set()
        return None

    monkeypatch.setattr(gallery_window_qt.thumbnails, "get_duration_seconds", fake_probe)

    gallery = _make_gallery(clips_dir)
    meta = gallery._rows[clip_path].meta_label
    before = meta.text()

    # The probe runs on the worker thread; wait for it, then deliver the
    # queued duration_ready(None) to the GUI thread.
    assert probe_called.wait(2.0)
    QApplication.sendPostedEvents()

    assert meta.text() == before
    assert ":" not in meta.text().split("·")[-1]  # no duration segment snuck in


def test_no_ffprobe_means_no_duration_probe_at_all(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip.mp4", mtime=1000)
    calls = []
    # find_ffprobe stays None (autouse fixture): get_duration_seconds must
    # never even be called.
    monkeypatch.setattr(
        gallery_window_qt.thumbnails, "get_duration_seconds", lambda *a, **k: calls.append(a)
    )
    gallery = _make_gallery(clips_dir)
    _process_events(lambda: bool(calls), timeout=0.3)
    assert calls == []


# ---- footer ----------------------------------------------------------------------


def test_footer_counts_clips_and_total_size(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    gallery = _make_gallery(clips_dir)
    assert gallery.footer_label.text() == "0 clips  ·  0 B  ·  0 favorites"

    one = _make_clip(clips_dir, "clip-1.mp4", mtime=1000)
    _make_clip(clips_dir, "clip-2.mp4", mtime=2000)
    gallery.refresh()
    assert gallery.footer_label.text() == "2 clips  ·  26 B  ·  0 favorites"

    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    gallery._do_delete(one)
    assert gallery.footer_label.text() == "1 clip  ·  13 B  ·  0 favorites"


# ---- play in-app (0.1.4) --------------------------------------------------------


class _FakeSignal:
    """Stand-in for a Qt signal on a fake dialog: connect/emit, nothing else."""

    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)


class _FakePlayerDialog:
    """PlayerDialog replacement: records construction, exposes the two
    signals the gallery connects, and never touches QtMultimedia."""

    instances = []

    def __init__(self, clip_path, ffmpeg_path=None, parent=None, autoplay=True):
        self.clip_path = clip_path
        self.ffmpeg_path = ffmpeg_path
        self.parent_widget = parent
        self.autoplay = autoplay
        self.trim_exported = _FakeSignal()
        self.destroyed = _FakeSignal()
        self.delete_on_close = False
        self.shown = False
        _FakePlayerDialog.instances.append(self)

    def setAttribute(self, attribute) -> None:
        if attribute == Qt.WidgetAttribute.WA_DeleteOnClose:
            self.delete_on_close = True

    def show(self) -> None:
        self.shown = True


@pytest.fixture()
def fake_player(monkeypatch):
    _FakePlayerDialog.instances = []
    monkeypatch.setattr(gallery_window_qt.player_qt, "PlayerDialog", _FakePlayerDialog)
    monkeypatch.setattr(gallery_window_qt.player_qt, "multimedia_available", lambda: True)
    return _FakePlayerDialog


def test_double_click_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    gallery._rows[clip_path].double_clicked.emit()

    assert len(fake_player.instances) == 1
    dialog = fake_player.instances[0]
    assert dialog.clip_path == clip_path
    assert dialog.ffmpeg_path == "ffmpeg"
    assert dialog.delete_on_close is True
    assert dialog.shown is True
    # Kept referenced so the GC can't collect the modal-less dialog.
    assert gallery._players == [dialog]


def test_context_menu_play_action_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    play_action = next(a for a in _menu_actions(menu) if a.text() == "Play")
    assert _menu_actions(menu)[0] is play_action  # first: the double-click behavior
    play_action.trigger()

    assert len(fake_player.instances) == 1


def test_closed_player_is_pruned_from_the_reference_list(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    gallery._play_clip(clip_path)
    dialog = fake_player.instances[0]

    dialog.destroyed.emit()

    assert gallery._players == []


def test_player_trim_export_refreshes_the_gallery(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    gallery._play_clip(clip_path)

    trimmed = _make_clip(clips_dir, "clip-trimmed.mp4", mtime=2000)
    fake_player.instances[0].trim_exported.emit(trimmed)

    assert trimmed in gallery._rows


def test_play_falls_back_to_open_file_without_multimedia(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(gallery_window_qt.player_qt, "multimedia_available", lambda: False)
    monkeypatch.setattr(gallery_window_qt.player_qt, "open_file", lambda path: opened.append(path))
    gallery = _make_gallery(clips_dir)

    gallery._rows[clip_path].double_clicked.emit()

    assert opened == [clip_path]
    assert gallery._players == []


def test_context_menu_trim_action_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    # "Trim…" no longer opens the old modal TrimDialog (deleted) -- it opens
    # the player, whose trim card does the exporting.
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Trim…").trigger()

    assert len(fake_player.instances) == 1
    assert fake_player.instances[0].clip_path == clip_path
    # Trim opens PAUSED (autoplay=False): a moving playhead makes marks
    # impossible to land, and autoplay made Trim feel like "just a player".
    assert fake_player.instances[0].autoplay is False


def test_play_actions_open_the_player_with_autoplay(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Play").trigger()

    assert fake_player.instances[0].autoplay is True


# ---- row layout: Play + heart + "⋯" ----------------------------------------------


def test_row_has_only_play_heart_and_menu_buttons(tmp_path: Path) -> None:
    # The decluttered row: Play, the favorite heart, and the "⋯" overflow.
    # Open/Reveal/Rename/Trim/Delete live in the context menu now.
    from PySide6.QtWidgets import QPushButton

    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]

    assert row.play_button.text() == "Play"
    assert row.menu_button.objectName() == "menuButton"
    button_texts = [b.text() for b in row.findChildren(QPushButton)]
    for removed in ("Open", "Reveal", "Rename", "Trim", "Delete"):
        assert removed not in button_texts
    for gone in ("open_button", "reveal_button", "rename_button", "trim_button", "delete_button"):
        assert not hasattr(row, gone)


def test_menu_button_pops_the_same_context_menu_as_right_click(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    shown = []
    monkeypatch.setattr(gallery, "_show_context_menu", lambda path, pos: shown.append(path))

    gallery._rows[clip_path].menu_button.click()

    assert shown == [clip_path]


# ---- clips_changed signal ---------------------------------------------------------


def test_clips_changed_not_emitted_on_plain_refresh_search_or_sort(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery.refresh()
    gallery.search_edit.setText("clip")
    gallery.sort_combo.setCurrentIndex(gallery.sort_combo.findData(gallery_window_qt.SORT_OLDEST))

    assert emitted == []


def test_clips_changed_emitted_on_single_delete(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery._do_delete(clip_path)

    assert emitted == [True]


def test_clips_changed_emitted_on_batch_delete(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip-a.mp4", mtime=1000)
    _make_clip(clips_dir, "clip-b.mp4", mtime=2000)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery.select_button.setChecked(True)
    gallery.select_all_button.click()
    gallery.delete_selected_button.click()

    assert emitted == [True]


def test_clips_changed_emitted_on_rename(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("clip-renamed", True)))
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery._do_rename(clip_path)

    assert emitted == [True]


def test_clips_changed_emitted_on_player_trim_export(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery._play_clip(clip_path)
    trimmed = _make_clip(clips_dir, "clip-trimmed.mp4", mtime=2000)
    fake_player.instances[0].trim_exported.emit(trimmed)

    assert emitted == [True]


def test_clips_changed_emitted_on_compress_success(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    gallery._on_compress_succeeded(clips_dir / "clip-compressed.mp4")

    assert emitted == [True]


# ---- details dialog -------------------------------------------------------------


def test_details_dialog_shows_file_facts_and_probed_info(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    dialog = ClipDetailsDialog("ffmpeg", clip_path, clips_dir, favorite=True)

    assert dialog.name_label.text() == "clip.mp4"
    assert "13 B" in dialog.meta_label.text()
    assert dialog.favorite_switch.isChecked() is True
    dialog._on_info_ready((65.0, 1920, 1080))
    assert "1:05" in dialog.info_label.text()
    assert "1920×1080" in dialog.info_label.text()
    dialog._on_info_ready(None)
    assert "unknown" in dialog.info_label.text()


def test_details_dialog_preloads_the_saved_note(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    clip_metadata.set_note(clips_dir, clip_path.name, "existing note")

    dialog = ClipDetailsDialog("ffmpeg", clip_path, clips_dir)

    assert dialog.note_edit.toPlainText() == "existing note"


def test_details_accept_persists_the_note_and_cancel_does_not(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    dialog = ClipDetailsDialog("ffmpeg", clip_path, clips_dir)
    dialog.note_edit.setPlainText("  a great save  ")
    dialog.accept()
    assert clip_metadata.note_for(clips_dir, clip_path.name) == "a great save"

    dialog2 = ClipDetailsDialog("ffmpeg", clip_path, clips_dir)
    dialog2.note_edit.setPlainText("draft only")
    dialog2.reject()
    assert clip_metadata.note_for(clips_dir, clip_path.name) == "a great save"


def test_details_dialog_via_gallery_saves_note_updates_tooltip_and_syncs_favorite(
    tmp_path: Path, monkeypatch
) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    assert row.toolTip() == ""

    def fake_exec(self):
        # Drive the real accept() path (which persists the note), and flip
        # the favorite switch so the gallery's _set_favorite wiring fires.
        self.note_edit.setPlainText("great save\nsecond line")
        self.favorite_switch.setChecked(True)
        self.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ClipDetailsDialog, "exec", fake_exec)
    gallery._do_details(clip_path)

    assert clip_metadata.note_for(clips_dir, clip_path.name) == "great save\nsecond line"
    assert row.toolTip() == "great save"  # first line only
    assert gallery._notes[clip_path.name] == "great save\nsecond line"
    assert clip_metadata.is_favorite(clips_dir, clip_path.name) is True
    assert row.favorite_button.isChecked() is True


def test_details_dialog_rejected_leaves_note_and_tooltip_untouched(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    monkeypatch.setattr(ClipDetailsDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    gallery._do_details(clip_path)

    assert clip_metadata.note_for(clips_dir, clip_path.name) == ""
    assert gallery._rows[clip_path].toolTip() == ""


def test_context_menu_details_action_opens_the_dialog(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(ClipDetailsDialog, "exec", lambda self: opened.append(self) or QDialog.DialogCode.Rejected)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Details…").trigger()

    assert len(opened) == 1
    assert opened[0]._clip_path == clip_path


def test_refresh_sets_row_tooltips_from_saved_notes(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    noted = _make_clip(clips_dir, "clip-noted.mp4", mtime=1000)
    plain = _make_clip(clips_dir, "clip-plain.mp4", mtime=2000)
    clip_metadata.set_note(clips_dir, noted.name, "first line\nsecond line")

    gallery = _make_gallery(clips_dir)

    assert gallery._rows[noted].toolTip() == "first line"
    assert gallery._rows[plain].toolTip() == ""


def test_row_tooltip_elides_a_long_note(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    clip_metadata.set_note(clips_dir, clip_path.name, "x" * 120)

    gallery = _make_gallery(clips_dir)

    tooltip = gallery._rows[clip_path].toolTip()
    assert len(tooltip) == 80
    assert tooltip.endswith("…")


def test_deleting_the_note_via_the_dialog_clears_the_tooltip(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    clip_metadata.set_note(clips_dir, clip_path.name, "doomed note")
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    assert row.toolTip() == "doomed note"

    def fake_exec(self):
        self.note_edit.setPlainText("")
        self.accept()  # set_note("") clears the key
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ClipDetailsDialog, "exec", fake_exec)
    gallery._do_details(clip_path)

    assert clip_metadata.note_for(clips_dir, clip_path.name) == ""
    assert row.toolTip() == ""
    assert clip_path.name not in gallery._notes


# ---- export dialogs (GIF / compress) ---------------------------------------------


def test_gif_dialog_exports_with_the_field_values(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    captured = {}

    def fake_gif(ffmpeg_path, clip, out_dir, start, duration, fps, width):
        captured.update(
            ffmpeg_path=ffmpeg_path, clip=clip, out_dir=out_dir,
            start=start, duration=duration, fps=fps, width=width,
        )
        return out_dir / "clip.gif"

    monkeypatch.setattr(gallery_window_qt.export, "export_gif", fake_gif)
    emitted = []
    dialog = GifExportDialog("ffmpeg", clip_path, clips_dir)
    dialog.export_succeeded.connect(lambda path: emitted.append(path))
    dialog.start_spin.setValue(1.5)
    dialog.duration_spin.setValue(5.0)
    dialog.fps_spin.setValue(20)
    dialog.width_spin.setValue(640)

    dialog.export_button.click()

    _process_events(lambda: bool(emitted))
    assert captured == {
        "ffmpeg_path": "ffmpeg",
        "clip": clip_path,
        "out_dir": clips_dir,
        "start": 1.5,
        "duration": 5.0,
        "fps": 20,
        "width": 640,
    }
    assert emitted == [clips_dir / "clip.gif"]


def test_gif_dialog_failure_shows_the_error_inline(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    def failing_gif(*args, **kwargs):
        raise gallery_window_qt.export.ExportError("ffmpeg palette pass failed")

    monkeypatch.setattr(gallery_window_qt.export, "export_gif", failing_gif)
    emitted = []
    dialog = GifExportDialog("ffmpeg", clip_path, clips_dir)
    dialog.export_succeeded.connect(lambda path: emitted.append(path))

    dialog.export_button.click()

    # The status label says "Exporting GIF…" synchronously -- wait for the error.
    _process_events(lambda: "palette" in dialog.status_label.text())
    assert "palette" in dialog.status_label.text()
    assert emitted == []
    assert dialog.export_button.isEnabled() is True  # re-armed after a failure
    assert dialog.cancel_button.isEnabled() is True


def test_gif_export_success_reports_inline_and_stays_open(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    def fake_gif(ffmpeg_path, clip, out_dir, start, duration, fps, width):
        return out_dir / "clip.gif"

    monkeypatch.setattr(gallery_window_qt.export, "export_gif", fake_gif)
    emitted = []
    dialog = GifExportDialog("ffmpeg", clip_path, clips_dir)
    dialog.export_succeeded.connect(lambda path: emitted.append(path))

    dialog.export_button.click()

    _process_events(lambda: bool(emitted))
    assert emitted == [clips_dir / "clip.gif"]
    # Success is inline (and silent): no message box, the dialog stays open
    # for another tweak + export.
    assert "Saved as clip.gif" in dialog.status_label.text()
    assert dialog.status_label.property("state") == "success"
    assert dialog.cancel_button.text() == "Close"
    assert dialog.result() == 0
    assert dialog.export_button.isEnabled() is True


def test_compress_dialog_uses_the_provided_encoder_and_values(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    captured = {}

    def fake_compress(ffmpeg_path, encoder, clip, out_dir, bitrate, scale_height):
        captured.update(
            ffmpeg_path=ffmpeg_path, encoder=encoder, clip=clip, out_dir=out_dir,
            bitrate=bitrate, scale_height=scale_height,
        )
        return out_dir / "clip-compressed.mp4"

    monkeypatch.setattr(gallery_window_qt.export, "compress_clip", fake_compress)
    emitted = []
    dialog = CompressDialog("ffmpeg", "h264_nvenc", clip_path, clips_dir)
    dialog.export_succeeded.connect(lambda path: emitted.append(path))
    dialog.bitrate_combo.setCurrentText("8M")
    dialog.scale_combo.setCurrentIndex(dialog.scale_combo.findData(720))

    dialog.export_button.click()

    _process_events(lambda: bool(emitted))
    assert captured == {
        "ffmpeg_path": "ffmpeg",
        "encoder": "h264_nvenc",
        "clip": clip_path,
        "out_dir": clips_dir,
        "bitrate": "8M",
        "scale_height": 720,
    }
    assert emitted == [clips_dir / "clip-compressed.mp4"]


def test_compress_dialog_defaults_to_4m_and_no_scale(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    dialog = CompressDialog("ffmpeg", "libx264", clip_path, clips_dir)
    assert dialog.bitrate_combo.currentText() == "4M"
    assert dialog.scale_combo.currentData() is None


def test_compress_failure_shows_the_error_inline(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)

    def failing_compress(*args, **kwargs):
        raise gallery_window_qt.export.ExportError("ffmpeg compress failed")

    monkeypatch.setattr(gallery_window_qt.export, "compress_clip", failing_compress)
    emitted = []
    dialog = CompressDialog("ffmpeg", "libx264", clip_path, clips_dir)
    dialog.export_succeeded.connect(lambda path: emitted.append(path))

    dialog.export_button.click()

    _process_events(lambda: "compress failed" in dialog.status_label.text())
    assert emitted == []
    assert dialog.export_button.isEnabled() is True


def test_compress_success_refreshes_the_gallery_silently(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    emitted = []
    gallery.clips_changed.connect(lambda: emitted.append(True))

    # The worker finished and left the new file next to the clip.
    compressed = _make_clip(clips_dir, "clip-compressed.mp4", mtime=2000)
    gallery._on_compress_succeeded(compressed)

    assert compressed in gallery._rows
    assert clip_path in gallery._rows  # the original is always kept
    assert emitted == [True]


def test_resolve_encoder_uses_the_provider_and_degrades_to_libx264(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    assert _make_gallery(clips_dir)._resolve_encoder() == "libx264"  # no provider
    assert GalleryFrame("ffmpeg", lambda: clips_dir, encoder_provider=lambda: "h264_nvenc")._resolve_encoder() == "h264_nvenc"

    def broken_provider():
        raise RuntimeError("state not ready")

    assert GalleryFrame("ffmpeg", lambda: clips_dir, encoder_provider=broken_provider)._resolve_encoder() == "libx264"


def test_do_compress_hands_the_resolved_encoder_to_the_dialog(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    opened = []
    monkeypatch.setattr(
        CompressDialog, "exec", lambda self: opened.append(self) or QDialog.DialogCode.Rejected
    )
    gallery = GalleryFrame("ffmpeg", lambda: clips_dir, encoder_provider=lambda: "h264_nvenc")

    gallery._do_compress(clip_path)

    assert len(opened) == 1
    assert opened[0]._encoder == "h264_nvenc"


# ---- drag-and-drop export ---------------------------------------------------------


def test_drag_mime_data_carries_the_clip_file_url(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]

    mime = row._build_drag_mime_data()

    assert mime.urls() == [QUrl.fromLocalFile(str(clip_path))]


def test_start_drag_executes_a_copy_drag_with_the_mime_data(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    captured = {}

    def fake_exec(self, actions):
        captured["urls"] = self.mimeData().urls()
        captured["actions"] = actions
        return Qt.DropAction.CopyAction

    monkeypatch.setattr(gallery_window_qt.QDrag, "exec", fake_exec)

    row._start_drag()

    assert captured["urls"] == [QUrl.fromLocalFile(str(clip_path))]
    assert captured["actions"] == Qt.DropAction.CopyAction


def _make_mouse_event(event_type, pos, button, buttons):
    from PySide6.QtCore import QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    return QMouseEvent(event_type, QPointF(pos), QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier)


def _press_and_drag(row, press_pos, move_pos):
    from PySide6.QtCore import QEvent

    row.mousePressEvent(
        _make_mouse_event(QEvent.Type.MouseButtonPress, press_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    )
    row.mouseMoveEvent(
        _make_mouse_event(QEvent.Type.MouseMove, move_pos, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    )


def test_drag_gesture_from_the_thumbnail_starts_a_drag(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    drags = []
    monkeypatch.setattr(gallery_window_qt.QDrag, "exec", lambda *a, **k: drags.append(a))
    # Force the layout to compute real child geometries for the unshown row.
    row.resize(700, 140)
    row.layout().activate()

    thumb_center = row.thumb_label.geometry().center()
    _press_and_drag(row, thumb_center, thumb_center + type(thumb_center)(50, 0))

    assert len(drags) == 1


def test_drag_gesture_from_outside_the_thumbnail_starts_no_drag(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    drags = []
    monkeypatch.setattr(gallery_window_qt.QDrag, "exec", lambda *a, **k: drags.append(a))
    row.resize(700, 140)
    row.layout().activate()

    thumb = row.thumb_label.geometry()
    outside = thumb.topRight() + type(thumb.topRight())(300, 45)
    _press_and_drag(row, outside, outside + type(outside)(50, 0))

    assert drags == []


def test_mouse_move_without_a_press_starts_no_drag(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    row = gallery._rows[clip_path]
    drags = []
    monkeypatch.setattr(gallery_window_qt.QDrag, "exec", lambda *a, **k: drags.append(a))

    from PySide6.QtCore import QEvent, QPoint

    assert row._drag_press_pos is None  # no press armed a drag
    row.mouseMoveEvent(
        _make_mouse_event(QEvent.Type.MouseMove, QPoint(50, 50), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    )
    assert drags == []


def test_list_container_does_not_autofill_an_unthemed_background(tmp_path: Path) -> None:
    # QScrollArea.setWidget flips the container's autoFillBackground ON --
    # the unthemed palette Window grey behind the rows in BOTH modes (the
    # "rogue dark background" report). It must stay off.
    gallery = _make_gallery(tmp_path)
    assert gallery._list_container.autoFillBackground() is False


def test_clip_row_name_label_elides_long_names(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QLabel

    clip = _make_clip(tmp_path, "a-very-long-window-title-" * 5 + "2026-07-22-13-57-07.mp4", 1_700_000_000)
    row = gallery_window_qt.ClipRow(clip, clip.stat())
    from clipersal.qt_widgets import ElidedLabel

    # The name and meta labels both elide; the name label is the non-hint one.
    elided = [label for label in row.findChildren(ElidedLabel) if label.objectName() != "hint"]
    assert len(elided) == 1
    assert elided[0].text() == clip.name  # full string preserved for tooltips/copy
    elided[0].resize(120, 20)
    elided[0].show()
    assert "…" in QLabel.text(elided[0])
    elided[0].close()


# ---- window-name parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Valorant-20260722-011351.mp4", "Valorant"),
        # Dashes and numbers inside the window part: only the 8+6-digit
        # timestamp is a safe anchor.
        ("My-Game-2026-20260722-011351.mp4", "My-Game-2026"),
        ("a-very-long-window-title-with-dashes-20260722-120000.mp4", "a-very-long-window-title-with-dashes"),
        ("clip-20260722-120000.mp4", "clip"),  # the old default template groups together
        ("Valorant-20260722-011351-1.mp4", "Valorant"),  # collision suffixes
        ("Valorant-20260722-011351-12.mp4", "Valorant"),
        ("Valorant-20260722-011351-trimmed.mp4", "Valorant"),  # export suffixes
        ("Valorant-20260722-011351-1-trimmed.mp4", "Valorant"),
        ("Valorant-20260722-011351-trimmed-2.mp4", "Valorant"),
        ("Valorant-20260722-011351-compressed.mp4", "Valorant"),
        ("Valorant-20260722-011351-1-compressed-3.mp4", "Valorant"),
        ("Clipersal-20260722-011351.MP4", "Clipersal"),  # case-insensitive extension
        ("random name.mp4", None),
        ("clip.mp4", None),
        ("no-timestamp-here.mp4", None),
        ("Valorant-20260722.mp4", None),  # a date alone is not a timestamp
    ],
)
def test_window_name_from_clip_name(name: str, expected: str | None) -> None:
    assert window_name_from_clip_name(name) == expected


# ---- window filter ---------------------------------------------------------------


def _make_windowed_clips(clips_dir: Path) -> dict[str, Path]:
    """Two Valorant clips, one Minecraft, one old-template "clip", and one
    unparseable oddball -- the shared fixture for the window-filter tests
    (mtimes descend in insertion order, so the default newest-first order
    matches the dict's)."""
    return {
        "valorant_new": _make_clip(clips_dir, "Valorant-20260722-011351.mp4", mtime=4000),
        "valorant_old": _make_clip(clips_dir, "Valorant-20260721-225959.mp4", mtime=3000),
        "minecraft": _make_clip(clips_dir, "Minecraft-20260720-101112.mp4", mtime=2000),
        "old_template": _make_clip(clips_dir, "clip-20260719-093000.mp4", mtime=1000),
        "oddball": _make_clip(clips_dir, "random name.mp4", mtime=500),
    }


def _window_filter_entries(gallery: GalleryFrame) -> list:
    """(label, userData) pairs in combo order."""
    combo = gallery.window_filter_combo
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def _set_window_filter(gallery: GalleryFrame, value) -> None:
    """Select the window filter entry whose userData equals `value`:
    WINDOW_FILTER_ALL for "All windows", a window name str, or None for
    "Other"."""
    combo = gallery.window_filter_combo
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return
    raise AssertionError(f"no window filter entry with value {value!r}")


def test_window_filter_lists_each_window_with_its_clip_count(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_windowed_clips(clips_dir)

    gallery = _make_gallery(clips_dir)

    # Case-insensitive window-name order, "Other" last, counts included.
    assert _window_filter_entries(gallery) == [
        ("All windows", WINDOW_FILTER_ALL),
        ("clip (1)", "clip"),
        ("Minecraft (1)", "Minecraft"),
        ("Valorant (2)", "Valorant"),
        ("Other (1)", None),
    ]
    assert gallery.window_filter_combo.currentIndex() == 0  # All by default
    # A long window title must never squeeze the other controls at the
    # window minimum -- the combo is width-capped (spec: ~180px).
    assert gallery.window_filter_combo.maximumWidth() <= 180


def test_window_filter_shows_only_the_chosen_window(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _set_window_filter(gallery, "Valorant")
    visible = {p for p, row in gallery._rows.items() if not row.isHidden()}
    assert visible == {clips["valorant_new"], clips["valorant_old"]}

    _set_window_filter(gallery, None)  # the "Other" entry: unparseable names only
    visible = {p for p, row in gallery._rows.items() if not row.isHidden()}
    assert visible == {clips["oddball"]}

    _set_window_filter(gallery, WINDOW_FILTER_ALL)  # back to everything
    assert all(not row.isHidden() for row in gallery._rows.values())
    # Filtering never destroys rows or lies about the folder's contents.
    assert set(gallery._rows.keys()) == set(clips.values())


def test_window_filter_composes_with_search_and_sort(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _set_window_filter(gallery, "Valorant")
    gallery.search_edit.setText("011351")
    assert _row_order(gallery) == [clips["valorant_new"]]

    gallery.search_edit.setText("")
    _set_sort(gallery, "oldest")
    assert _row_order(gallery) == [clips["valorant_old"], clips["valorant_new"]]


def test_window_filter_selection_survives_a_refresh_when_the_window_still_exists(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)
    _set_window_filter(gallery, "Valorant")

    gallery.refresh()

    assert gallery._window_filter == "Valorant"
    assert gallery.window_filter_combo.currentData() == "Valorant"


def test_window_filter_falls_back_to_all_when_its_window_disappears(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)
    _set_window_filter(gallery, "Minecraft")

    clips["minecraft"].unlink()
    gallery.refresh()

    assert gallery._window_filter == WINDOW_FILTER_ALL
    assert gallery.window_filter_combo.currentIndex() == 0
    assert all(not row.isHidden() for row in gallery._rows.values())


def test_window_filter_repopulates_counts_on_refresh(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _make_clip(clips_dir, "Valorant-20260723-000001.mp4", mtime=5000)
    gallery.refresh()

    labels = [label for label, _value in _window_filter_entries(gallery)]
    assert "Valorant (3)" in labels


def test_sort_window_groups_by_window_name_then_newest_first(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _set_sort(gallery, "window")

    # Case-insensitive window order: "clip" < "Minecraft" < "Valorant";
    # within Valorant the newer clip leads; unparseable names ("Other")
    # sink to the end.
    assert _row_order(gallery) == [
        clips["old_template"],
        clips["minecraft"],
        clips["valorant_new"],
        clips["valorant_old"],
        clips["oddball"],
    ]


def test_window_filter_with_no_matches_shows_the_match_specific_empty_message(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _set_window_filter(gallery, "Valorant")
    gallery.search_edit.setText("zzz-no-such-clip")

    assert gallery._empty_container.isHidden() is False
    assert gallery._empty_label.text() == "No clips match your search/filter."


# ---- grid view -------------------------------------------------------------------


def _set_view(gallery: GalleryFrame, mode: str) -> None:
    """Click the view switch's "List"/"Grid" segment. Clicking, not
    setCurrent -- only a click emits currentTextChanged (the same rule as
    the Settings tab's segmented controls)."""
    gallery.view_switch._buttons[mode].click()


def test_every_clip_gets_a_card_as_well_as_a_row(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    assert set(gallery._cards.keys()) == set(clips.values())
    card = gallery._cards[clips["valorant_new"]]
    assert isinstance(card, ClipCard)
    # Fixed-width card (spec: ~196px) with the grid's 16:9 thumbnail box.
    assert card.minimumWidth() == card.maximumWidth() == 196
    assert (card.thumb_label.minimumWidth(), card.thumb_label.minimumHeight()) == (176, 99)
    assert card.thumb_label.minimumSize() == card.thumb_label.maximumSize()  # fixed box


def test_view_switch_toggles_between_the_list_and_grid_pages(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    assert gallery._view_mode == "list"  # List is the default
    assert gallery._view_stack.currentWidget() is gallery._scroll_area

    _set_view(gallery, "Grid")
    assert gallery._view_mode == "grid"
    assert gallery._view_stack.currentWidget() is gallery._grid_scroll_area

    _set_view(gallery, "List")
    assert gallery._view_mode == "list"
    assert gallery._view_stack.currentWidget() is gallery._scroll_area


def test_grid_cards_follow_the_same_filter_and_sort_as_the_rows(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)

    _set_window_filter(gallery, "Valorant")
    _set_sort(gallery, "oldest")
    _set_view(gallery, "Grid")

    # The grid places exactly the rows the list shows, in the same order.
    assert [card.clip_path for card in gallery._grid_order] == _row_order(gallery)
    assert [card.clip_path for card in gallery._grid_order] == [clips["valorant_old"], clips["valorant_new"]]
    for path in (clips["minecraft"], clips["old_template"], clips["oddball"]):
        assert gallery._cards[path].isHidden() is True


def test_view_toggle_round_trip_preserves_the_whole_view_state(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    gallery = _make_gallery(clips_dir)
    _set_window_filter(gallery, "Valorant")
    _set_sort(gallery, "oldest")
    gallery.select_button.click()
    gallery._rows[clips["valorant_new"]].select_checkbox.setChecked(True)

    _set_view(gallery, "Grid")
    assert gallery._view_mode == "grid"
    # The selection mirrored onto the card face.
    assert gallery._cards[clips["valorant_new"]].select_checkbox.isChecked() is True

    _set_view(gallery, "List")
    assert gallery._view_mode == "list"
    assert gallery._window_filter == "Valorant"
    assert gallery.sort_combo.currentData() == "oldest"
    assert gallery._selected == {clips["valorant_new"]}
    assert _row_order(gallery) == [clips["valorant_old"], clips["valorant_new"]]


def test_card_heart_toggles_the_favorite_and_syncs_the_row(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    gallery._cards[clip_path].favorite_button.click()

    assert clip_metadata.is_favorite(clips_dir, clip_path.name) is True
    assert gallery._rows[clip_path].favorite_button.isChecked() is True
    assert gallery._rows[clip_path].favorite_button.text() == "♥"


def test_card_menu_button_pops_the_same_context_menu(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    shown = []
    monkeypatch.setattr(gallery, "_show_context_menu", lambda path, pos: shown.append(path))

    gallery._cards[clip_path].menu_button.click()

    assert shown == [clip_path]


def test_card_double_click_opens_the_in_app_player(tmp_path: Path, fake_player) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    gallery._cards[clip_path].double_clicked.emit()

    assert len(fake_player.instances) == 1
    assert fake_player.instances[0].clip_path == clip_path


def test_card_selection_checkbox_mirrors_the_row_and_back(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)
    gallery.select_button.click()
    row = gallery._rows[clip_path]
    card = gallery._cards[clip_path]
    assert card.select_checkbox.isHidden() is False  # selection mode shows on cards too

    card.select_checkbox.setChecked(True)
    assert gallery._selected == {clip_path}
    assert row.select_checkbox.isChecked() is True

    row.select_checkbox.setChecked(False)
    assert gallery._selected == set()
    assert card.select_checkbox.isChecked() is False


def test_card_drag_mime_data_carries_the_clip_file_url(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    mime = gallery._cards[clip_path]._build_drag_mime_data()

    assert mime.urls() == [QUrl.fromLocalFile(str(clip_path))]


def test_card_receives_thumbnails_and_durations_from_the_worker(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "clip.mp4", mtime=1000)
    image_path = tmp_path / "thumb.png"
    pixmap = QPixmap(40, 40)
    pixmap.fill()
    pixmap.save(str(image_path), "PNG")
    monkeypatch.setattr(gallery_window_qt.thumbnails, "ensure_thumbnail", lambda *a, **k: image_path)
    monkeypatch.setattr(gallery_window_qt.thumbnails, "find_ffprobe", lambda *a, **k: "ffprobe")
    monkeypatch.setattr(gallery_window_qt.thumbnails, "get_duration_seconds", lambda *a, **k: 65.0)

    gallery = _make_gallery(clips_dir)
    card = gallery._cards[clip_path]
    _process_events(lambda: not card.thumb_label.pixmap().isNull() and "1:05" in card.meta_label.text())

    assert not card.thumb_label.pixmap().isNull()
    # The card's compact meta line: size + duration, no saved-at date.
    assert card.meta_label.text() == "13 B  ·  1:05"


def test_deleting_a_clip_removes_its_card_from_the_grid_order(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips = _make_windowed_clips(clips_dir)
    monkeypatch.setattr(gallery_window_qt, "quiet_message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    gallery = _make_gallery(clips_dir)
    _set_view(gallery, "Grid")
    assert len(gallery._grid_order) == 5

    gallery._do_delete(clips["valorant_new"])

    assert clips["valorant_new"] not in gallery._rows
    assert clips["valorant_new"] not in gallery._cards
    assert [card.clip_path for card in gallery._grid_order] == [
        clips["valorant_old"],
        clips["minecraft"],
        clips["old_template"],
        clips["oddball"],
    ]


def test_grid_container_does_not_autofill_an_unthemed_background(tmp_path: Path) -> None:
    # The grid twin of the list-container test above: QScrollArea.setWidget
    # flips the container's autoFillBackground ON (the "rogue dark
    # background" report) -- it must stay off here too.
    gallery = _make_gallery(tmp_path)
    assert gallery._grid_container.autoFillBackground() is False


# ---- footer favorites count + Copy filename ----------------------------------------


def test_footer_shows_the_favorites_count_and_tracks_heart_toggles(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    fav = _make_clip(clips_dir, "clip-fav.mp4", mtime=2000)
    _make_clip(clips_dir, "clip-plain.mp4", mtime=1000)
    clip_metadata.set_favorite(clips_dir, fav.name, True)

    gallery = _make_gallery(clips_dir)
    assert gallery.footer_label.text() == "2 clips  ·  26 B  ·  1 favorite"

    gallery._rows[fav].favorite_button.click()  # un-heart: the count updates live
    assert gallery.footer_label.text() == "2 clips  ·  26 B  ·  0 favorites"


def test_context_menu_copy_filename_puts_the_stem_on_the_clipboard(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clip_path = _make_clip(clips_dir, "Valorant-20260722-011351.mp4", mtime=1000)
    gallery = _make_gallery(clips_dir)

    menu = gallery._build_context_menu(clip_path)
    next(a for a in _menu_actions(menu) if a.text() == "Copy filename").trigger()

    assert QGuiApplication.clipboard().text() == "Valorant-20260722-011351"  # stem only, no extension
