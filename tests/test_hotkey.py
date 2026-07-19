from clipersal.hotkey import is_supported, is_valid_combo
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


# ---- is_valid_combo: the gate that keeps free-typed garbage out of the
# persisted hotkey_combo (which is rebound at every launch) -------------------


def test_is_valid_combo_accepts_pynput_format_combos() -> None:
    assert is_valid_combo("<ctrl>+<alt>+r") is True
    assert is_valid_combo("<shift>+k") is True
    assert is_valid_combo("a") is True


def test_is_valid_combo_rejects_unparseable_text() -> None:
    assert is_valid_combo("Press keys...") is False  # the recorder's placeholder
    assert is_valid_combo("garbage combo") is False
    assert is_valid_combo("<bogus>") is False


def test_is_valid_combo_rejects_empty_and_whitespace() -> None:
    assert is_valid_combo("") is False
    assert is_valid_combo("   ") is False
