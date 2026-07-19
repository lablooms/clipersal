import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QFileDialog

from clipersal import first_run_qt
from clipersal.config import Config
from clipersal.first_run_qt import _FirstRunDialog
from clipersal.hotkey import DEFAULT_COMBO


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def scratch_appdata(tmp_path, monkeypatch):
    # config_store.save_overrides() writes to a real file path -- always
    # redirect APPDATA/XDG_CONFIG_HOME before touching it in a test.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    yield


def _make_config(tmp_path: Path) -> Config:
    return Config(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")


def test_skip_persists_config_and_accepts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)

    dialog._skip()

    assert dialog.result() == _FirstRunDialog.DialogCode.Accepted
    from clipersal import config_store

    saved = config_store.load_overrides()
    assert saved["clips_dir"] == str(config.clips_dir)


def test_get_started_rejects_empty_hotkey(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)
    dialog._hotkey_field.entry.setText("")

    dialog._get_started()

    assert dialog._error_label.text() == "Hotkey cannot be empty."
    assert dialog.result() != _FirstRunDialog.DialogCode.Accepted


def test_get_started_rejects_empty_clips_folder(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)
    dialog._clips_dir_edit.setText("   ")

    dialog._get_started()

    assert dialog._error_label.text() == "Clips folder cannot be empty."


def test_get_started_success_mutates_config_and_persists(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)
    new_clips_dir = tmp_path / "chosen_clips"
    dialog._clips_dir_edit.setText(str(new_clips_dir))
    dialog._hotkey_field.entry.setText("<ctrl>+<shift>+s")

    dialog._get_started()

    assert config.clips_dir == new_clips_dir
    assert config.hotkey_combo == "<ctrl>+<shift>+s"
    assert new_clips_dir.exists()
    assert dialog.result() == _FirstRunDialog.DialogCode.Accepted


def test_get_started_reports_error_for_unwritable_clips_folder(tmp_path: Path, monkeypatch) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)
    dialog._clips_dir_edit.setText(str(tmp_path / "somewhere"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(first_run_qt.Path, "mkdir", boom)

    dialog._get_started()

    assert "disk full" in dialog._error_label.text()
    assert dialog.result() != _FirstRunDialog.DialogCode.Accepted


def test_browse_updates_clips_dir_field(tmp_path: Path, monkeypatch) -> None:
    chosen = str(tmp_path / "browsed")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: chosen))
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)

    dialog._browse_clips_dir()

    assert dialog._clips_dir_edit.text() == chosen


def test_close_event_behaves_like_skip(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)

    event = QCloseEvent()
    dialog.closeEvent(event)

    assert dialog.result() == _FirstRunDialog.DialogCode.Accepted
    from clipersal import config_store

    saved = config_store.load_overrides()
    assert saved["clips_dir"] == str(config.clips_dir)


# ---- hotkey combo validation + the mid-record hazard -------------------------


class _FakeListener:
    """Stands in for pynput.keyboard.Listener -- tests never hook real global
    keyboard input (same rule as test_hotkey_widget_qt.py).
    """

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_get_started_rejects_invalid_hotkey_combo(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)
    dialog._hotkey_field.entry.setText("garbage combo")

    dialog._get_started()

    assert "Invalid hotkey combo" in dialog._error_label.text()
    assert dialog.result() != _FirstRunDialog.DialogCode.Accepted
    # The bad combo must not have been mutated in / persisted.
    assert config.hotkey_combo == DEFAULT_COMBO


def test_get_started_while_recording_cancels_recorder_instead_of_persisting_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    import pynput.keyboard

    monkeypatch.setattr(pynput.keyboard, "Listener", _FakeListener)
    config = _make_config(tmp_path)
    dialog = _FirstRunDialog(config)

    dialog._hotkey_field.record_button.click()  # entry now shows "Press keys..."
    assert dialog._hotkey_field.is_recording() is True

    dialog._get_started()

    # The recording was cancelled and the pre-record combo persisted --
    # previously the placeholder text itself became the saved hotkey.
    assert dialog._hotkey_field.is_recording() is False
    assert config.hotkey_combo == DEFAULT_COMBO
    assert dialog.result() == _FirstRunDialog.DialogCode.Accepted
    from clipersal import config_store

    saved = config_store.load_overrides()
    assert saved["hotkey_combo"] == DEFAULT_COMBO
