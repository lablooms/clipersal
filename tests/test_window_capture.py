import subprocess

from clipersal.ffmpeg_utils import build_window_capture_source
from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.window_capture import WindowInfo, _list_linux_windows, list_windows

_WMCTRL_OUTPUT = (
    "0x03400007  0 0    0    1920 1080 host.local Some Browser Window\n"
    "0x03600002 -1 0    0    1920 1080 host.local Desktop\n"
    "0x03800009  0 1920 100  800  600  host.local  \n"
)


def test_list_linux_windows_parses_wmctrl_output(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=_WMCTRL_OUTPUT, stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    result = _list_linux_windows()

    assert result == [
        WindowInfo(handle="0x03400007", title="Some Browser Window", x=0, y=0, width=1920, height=1080),
    ]


def test_list_linux_windows_skips_sticky_desktop_and_blank_titles(monkeypatch) -> None:
    # Covered by the assertion above (only one of three lines survives), but
    # spelled out explicitly for clarity: desktop=-1 (sticky) and a blank
    # title are both excluded.
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=_WMCTRL_OUTPUT, stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    titles = [w.title for w in _list_linux_windows()]
    assert "Desktop" not in titles
    assert "" not in titles


def test_list_linux_windows_returns_empty_when_wmctrl_missing(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("wmctrl not found")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    assert _list_linux_windows() == []


def test_list_linux_windows_parses_negative_coordinates(monkeypatch) -> None:
    # Real "wmctrl -lG" output reports negative x/y for a window on another
    # viewport or hanging off the left/top screen edge; the regex must not
    # silently drop such windows from the Settings picker.
    output = "0x0201c24f  0 -2552 96   1920 1080 host.local Offscreen Window\n"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    assert _list_linux_windows() == [
        WindowInfo(handle="0x0201c24f", title="Offscreen Window", x=-2552, y=96, width=1920, height=1080),
    ]


def test_window_capture_geometry_args_clamp_negative_origin(monkeypatch) -> None:
    # XParseGeometry reads "display+-2552,96" as an offset relative to the
    # right edge, so the x11grab input string must clamp a negative origin
    # to the screen edge rather than pass it through.
    monkeypatch.setenv("DISPLAY", ":0.0")
    windows = [WindowInfo(handle="0x0201c24f", title="My App", x=-2552, y=-96, width=1920, height=1080)]
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_windows", lambda os_: windows)

    source = build_window_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, "My App", framerate=30)

    assert source.kind == "x11grab-window"
    assert source.input_args == [
        "-f", "x11grab", "-framerate", "30",
        "-video_size", "1920x1080",
        "-i", ":0.0+0,0",
    ]


def test_list_windows_returns_empty_for_unsupported_os() -> None:
    assert list_windows(OS.OTHER) == []
    assert list_windows(OS.MACOS) == []
