from clipersal.hotkey import is_supported
from clipersal.platform_detect import OS, LinuxSessionType


def test_windows_always_supported() -> None:
    assert is_supported(OS.WINDOWS, None) is True


def test_linux_x11_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.X11) is True


def test_linux_wayland_not_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.WAYLAND) is False


def test_linux_unknown_session_not_supported() -> None:
    assert is_supported(OS.LINUX, LinuxSessionType.UNKNOWN) is False


def test_macos_not_supported_yet() -> None:
    assert is_supported(OS.MACOS, None) is False
