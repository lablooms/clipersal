from clipersal.platform_detect import LinuxSessionType, get_linux_session_type


def test_session_type_from_xdg_session_type_wayland(monkeypatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert get_linux_session_type() == LinuxSessionType.WAYLAND


def test_session_type_from_xdg_session_type_x11(monkeypatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert get_linux_session_type() == LinuxSessionType.X11


def test_session_type_falls_back_to_wayland_display(monkeypatch) -> None:
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert get_linux_session_type() == LinuxSessionType.WAYLAND


def test_session_type_falls_back_to_display(monkeypatch) -> None:
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert get_linux_session_type() == LinuxSessionType.X11


def test_session_type_unknown_when_nothing_set(monkeypatch) -> None:
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert get_linux_session_type() == LinuxSessionType.UNKNOWN
