import sys

import pytest

from clipersal import autostart
from clipersal.platform_detect import OS


def test_is_supported() -> None:
    assert autostart.is_supported(OS.WINDOWS) is True
    assert autostart.is_supported(OS.LINUX) is True
    assert autostart.is_supported(OS.MACOS) is False
    assert autostart.is_supported(OS.OTHER) is False


def test_launch_command_from_source(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert autostart.launch_command() == [sys.executable, "-m", "clipersal.cli"]


def test_launch_command_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert autostart.launch_command() == [sys.executable]


def test_enable_raises_on_unsupported_platform() -> None:
    with pytest.raises(NotImplementedError):
        autostart.enable(OS.MACOS)


def test_disable_raises_on_unsupported_platform() -> None:
    with pytest.raises(NotImplementedError):
        autostart.disable(OS.MACOS)


def test_is_enabled_false_on_unsupported_platform() -> None:
    assert autostart.is_enabled(OS.MACOS) is False


def test_linux_enable_disable_round_trip(tmp_path, monkeypatch) -> None:
    desktop_path = tmp_path / "autostart" / "clipersal.desktop"
    monkeypatch.setattr(autostart, "_autostart_desktop_path", lambda: desktop_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\apps\clipersal.exe", raising=False)

    assert autostart.is_enabled(OS.LINUX) is False

    autostart.enable(OS.LINUX)

    assert autostart.is_enabled(OS.LINUX) is True
    content = desktop_path.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content
    assert "Exec=" in content
    assert "clipersal.exe" in content
    assert "X-GNOME-Autostart-enabled=true" in content

    autostart.disable(OS.LINUX)

    assert autostart.is_enabled(OS.LINUX) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry only")
def test_windows_enable_disable_round_trip(monkeypatch) -> None:
    # A dedicated test-only value name -- never touches the real
    # "Clipersal" autostart entry a developer running this suite might
    # actually have registered.
    monkeypatch.setattr(autostart, "_REGISTRY_VALUE_NAME", "ClipersalTest")
    try:
        assert autostart.is_enabled(OS.WINDOWS) is False

        autostart.enable(OS.WINDOWS)
        assert autostart.is_enabled(OS.WINDOWS) is True

        autostart.disable(OS.WINDOWS)
        assert autostart.is_enabled(OS.WINDOWS) is False
    finally:
        autostart.disable(OS.WINDOWS)  # safety net if an assertion failed mid-test
