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


def test_linux_exec_line_double_quotes_space_containing_path(tmp_path, monkeypatch) -> None:
    # The Desktop Entry Spec does not shell-parse Exec: a single quote is a
    # reserved character treated literally, so a space-containing path must
    # be wrapped in double quotes or a spec-conforming launcher splits it
    # into bogus arguments.
    desktop_path = tmp_path / "autostart" / "clipersal.desktop"
    monkeypatch.setattr(autostart, "_autostart_desktop_path", lambda: desktop_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/home/user/My Apps/clipersal.AppImage", raising=False)

    autostart.enable(OS.LINUX)

    content = desktop_path.read_text(encoding="utf-8")
    assert 'Exec="/home/user/My Apps/clipersal.AppImage"\n' in content


def test_linux_exec_line_escapes_percent_as_field_code(tmp_path, monkeypatch) -> None:
    # "%" introduces Exec field codes (%f, %u, ...); a literal percent sign
    # in the command must be written as "%%".
    desktop_path = tmp_path / "autostart" / "clipersal.desktop"
    monkeypatch.setattr(autostart, "_autostart_desktop_path", lambda: desktop_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/100%/clipersal", raising=False)

    autostart.enable(OS.LINUX)

    content = desktop_path.read_text(encoding="utf-8")
    assert "Exec=/opt/100%%/clipersal\n" in content


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
