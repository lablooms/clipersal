from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication

from clipersal.signals import AppSignals


@pytest.fixture(scope="module", autouse=True)
def qcoreapp():
    # AppSignals is a QObject; PySide6 wants a QCoreApplication instance to
    # exist before QObjects are constructed. No GUI/event loop is needed for
    # these direct same-thread emit/receive tests.
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def test_show_requested_carries_tab_name_or_none() -> None:
    signals = AppSignals()
    received = []
    signals.show_requested.connect(received.append)

    signals.show_requested.emit(None)
    signals.show_requested.emit("settings")

    assert received == [None, "settings"]


def test_toast_requested_carries_a_path() -> None:
    signals = AppSignals()
    received = []
    signals.toast_requested.connect(received.append)

    clip_path = Path("clip-20260101-000000.mp4")
    signals.toast_requested.emit(clip_path)

    assert received == [clip_path]


def test_save_completed_takes_no_arguments() -> None:
    signals = AppSignals()
    call_count = {"n": 0}
    signals.save_completed.connect(lambda: call_count.__setitem__("n", call_count["n"] + 1))

    signals.save_completed.emit()
    signals.save_completed.emit()

    assert call_count["n"] == 2


def test_quit_requested_takes_no_arguments() -> None:
    signals = AppSignals()
    call_count = {"n": 0}
    signals.quit_requested.connect(lambda: call_count.__setitem__("n", call_count["n"] + 1))

    signals.quit_requested.emit()

    assert call_count["n"] == 1


def test_theme_changed_takes_no_arguments() -> None:
    signals = AppSignals()
    call_count = {"n": 0}
    signals.theme_changed.connect(lambda: call_count.__setitem__("n", call_count["n"] + 1))

    signals.theme_changed.emit()

    assert call_count["n"] == 1


def test_update_available_carries_version_and_url() -> None:
    signals = AppSignals()
    received = []
    signals.update_available.connect(lambda v, u: received.append((v, u)))

    signals.update_available.emit("0.2.0", "https://example.invalid/releases/tag/v0.2.0")

    assert received == [("0.2.0", "https://example.invalid/releases/tag/v0.2.0")]


def test_each_instance_has_independent_connections() -> None:
    # Signals are defined on the class but connections are per-instance --
    # two AppSignals (e.g. in two separate tests, or hypothetically two
    # windows) shouldn't cross-deliver to each other's slots.
    a, b = AppSignals(), AppSignals()
    received_by_a = []
    a.save_completed.connect(lambda: received_by_a.append(1))

    b.save_completed.emit()

    assert received_by_a == []
