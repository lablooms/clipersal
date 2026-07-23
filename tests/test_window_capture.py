import ctypes
import subprocess
import sys
from types import SimpleNamespace

import pytest

from clipersal import window_capture
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


def test_wmctrl_output_is_decoded_as_utf8_with_replacement(monkeypatch) -> None:
    # Bare text=True decodes with the LOCALE encoding under strict errors --
    # a non-ASCII window title under a C/POSIX locale would escape this
    # module's never-raises contract as UnicodeDecodeError.
    captured_kwargs = {}

    def fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="0x03400007  0 0 0 1920 1080 host.local App\n", stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    _list_linux_windows()

    assert captured_kwargs["encoding"] == "utf-8"
    assert captured_kwargs["errors"] == "replace"


def test_sticky_normal_windows_are_kept_and_desktop_background_is_dropped(monkeypatch) -> None:
    # wmctrl lumps the desktop background and genuine sticky ("always on the
    # visible workspace") user windows together under desktop -1; xprop's
    # _NET_WM_WINDOW_TYPE tells them apart -- NORMAL sticky windows stay in
    # the picker, the DESKTOP background does not.
    wmctrl_output = (
        "0x03400007  0 0    0    1920 1080 host.local Some Browser Window\n"
        "0x03600002 -1 0    0    1920 1080 host.local Desktop\n"
        "0x03a00011 -1 100  100  640  480  host.local Sticky Notes\n"
    )
    xprop_types = {
        "0x03600002": "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DESKTOP\n",
        "0x03a00011": "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n",
    }
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "xprop":
            return subprocess.CompletedProcess(cmd, 0, stdout=xprop_types[cmd[2]], stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=wmctrl_output, stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    result = _list_linux_windows()

    assert [w.title for w in result] == ["Some Browser Window", "Sticky Notes"]
    # Only the desktop<0 entries trigger an xprop probe...
    assert [cmd[:3] for cmd, _ in calls[1:]] == [
        ["xprop", "-id", "0x03600002"],
        ["xprop", "-id", "0x03a00011"],
    ]
    # ...with the same never-raises decoding as the wmctrl call.
    assert all(kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace" for _, kwargs in calls)


def test_sticky_windows_stay_excluded_when_xprop_cannot_answer(monkeypatch) -> None:
    # Conservative fallback: without a window type a sticky user window
    # can't be told apart from the desktop background, and offering the
    # background as a capturable "window" is the worse failure.
    def fake_run(cmd, **kwargs):
        if cmd[0] == "xprop":
            raise FileNotFoundError("xprop not found")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="0x03600002 -1 0 0 1920 1080 host.local Desktop\n", stderr=""
        )

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    assert _list_linux_windows() == []


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes prototypes are Windows-only")
def test_user32_prototypes_use_real_handle_types() -> None:
    # Without declared argtypes ctypes defaults every argument to C int and
    # truncates 64-bit HWNDs on their way back into the query functions --
    # these declarations (installed at import) are what keep handles intact.
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    assert user32.EnumWindows.argtypes == [window_capture._EnumWindowsProc, wintypes.LPARAM]
    assert user32.EnumWindows.restype == wintypes.BOOL
    assert user32.IsWindowVisible.argtypes == [wintypes.HWND]
    assert user32.IsWindowVisible.restype == wintypes.BOOL
    assert user32.GetWindowTextLengthW.argtypes == [wintypes.HWND]
    assert user32.GetWindowTextLengthW.restype == ctypes.c_int
    assert user32.GetWindowTextW.argtypes == [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    assert user32.GetWindowTextW.restype == ctypes.c_int
    assert user32.GetWindowRect.argtypes == [wintypes.HWND, ctypes.POINTER(window_capture._RECT)]
    assert user32.GetWindowRect.restype == wintypes.BOOL
    assert user32.IsIconic.argtypes == [wintypes.HWND]
    assert user32.IsIconic.restype == wintypes.BOOL
    assert user32.GetForegroundWindow.argtypes == []
    assert user32.GetForegroundWindow.restype == wintypes.HWND


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.windll is Windows-only")
def test_minimized_windows_are_excluded_from_the_picker(monkeypatch) -> None:
    # GetWindowRect on a MINIMIZED window returns the iconic rect -- a
    # (-32000,-32000) origin with a POSITIVE size -- so the old width/height
    # check alone let minimized windows into the picker; only the IsIconic
    # check keeps them out.
    titles = {1: "Visible App", 2: "Minimized App"}
    iconic = {2}

    class FakeUser32:
        def EnumWindows(self, proc, lparam):
            for hwnd in titles:
                proc(hwnd, lparam)
            return True

        def IsWindowVisible(self, hwnd):
            return True  # minimized windows still count as visible

        def IsIconic(self, hwnd):
            return hwnd in iconic

        def GetWindowTextLengthW(self, hwnd):
            return len(titles[hwnd])

        def GetWindowTextW(self, hwnd, buf, _count):
            buf.value = titles[hwnd]
            return len(titles[hwnd])

        def GetWindowRect(self, hwnd, rect_ptr):
            rect = ctypes.cast(rect_ptr, ctypes.POINTER(window_capture._RECT)).contents
            if hwnd in iconic:
                rect.left, rect.top = -32000, -32000
                rect.right, rect.bottom = -32000 + 160, -32000 + 32  # iconic rect: positive size
            else:
                rect.left, rect.top, rect.right, rect.bottom = 0, 0, 800, 600
            return True

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=FakeUser32()))

    result = window_capture._list_windows_windows()

    assert [w.title for w in result] == ["Visible App"]


# ---- active_window_title ({window} filename placeholder) -------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.windll is Windows-only")
def test_active_window_title_windows_reads_the_foreground_window(monkeypatch) -> None:
    class FakeUser32:
        def GetForegroundWindow(self):
            return 123

        def GetWindowTextLengthW(self, hwnd):
            assert hwnd == 123
            return len("My Game")

        def GetWindowTextW(self, hwnd, buf, _count):
            buf.value = "My Game"
            return len("My Game")

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=FakeUser32()))

    assert window_capture.active_window_title(OS.WINDOWS) == "My Game"


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.windll is Windows-only")
def test_active_window_title_windows_none_without_a_foreground_window_or_title(monkeypatch) -> None:
    class FakeUser32:
        def __init__(self, hwnd, length):
            self._hwnd = hwnd
            self._length = length

        def GetForegroundWindow(self):
            return self._hwnd

        def GetWindowTextLengthW(self, hwnd):
            return self._length

    # No foreground window (NULL handle)...
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=FakeUser32(None, 10)))
    assert window_capture.active_window_title(OS.WINDOWS) is None
    # ...and a foreground window with no title both read as "no title".
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=FakeUser32(123, 0)))
    assert window_capture.active_window_title(OS.WINDOWS) is None


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes.windll is Windows-only")
def test_active_window_title_windows_never_raises_on_a_failed_probe(monkeypatch) -> None:
    class FakeUser32:
        def GetForegroundWindow(self):
            raise OSError("win32 error (fake)")

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=FakeUser32()))

    assert window_capture.active_window_title(OS.WINDOWS) is None


def test_active_window_title_x11_parses_the_two_xprop_probes(monkeypatch) -> None:
    outputs = {
        ("xprop", "-root", "_NET_ACTIVE_WINDOW"): "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x03a00011\n",
        ("xprop", "-id", "0x03a00011", "_NET_WM_NAME"): '_NET_WM_NAME(UTF8_STRING) = "My Browser Tab"\n',
    }
    captured_kwargs = {}

    def fake_run(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs[tuple(cmd)], stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) == "My Browser Tab"
    # Same never-raises decoding as the other xprop call in this module.
    assert captured_kwargs["encoding"] == "utf-8"
    assert captured_kwargs["errors"] == "replace"
    assert captured_kwargs["timeout"] == window_capture._XPROP_TIMEOUT


def test_active_window_title_x11_none_when_nothing_has_focus(monkeypatch) -> None:
    # _NET_ACTIVE_WINDOW of 0x0 = no active window -- the second probe must
    # not even run.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n", stderr="")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", fake_run)

    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) is None
    assert len(calls) == 1


def test_active_window_title_x11_none_for_a_missing_or_blank_wm_name(monkeypatch) -> None:
    def make_run(name_stdout):
        def fake_run(cmd, **kwargs):
            if cmd[1] == "-root":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="_NET_ACTIVE_WINDOW(WINDOW): window id # 0x03a00011\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=name_stdout, stderr="")

        return fake_run

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", make_run("_NET_WM_NAME:  not found.\n"))
    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) is None

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", make_run('_NET_WM_NAME(UTF8_STRING) = "   "\n'))
    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) is None


def test_active_window_title_x11_never_raises_when_xprop_fails(monkeypatch) -> None:
    def missing_run(cmd, **kwargs):
        raise FileNotFoundError("xprop not found")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", missing_run)
    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) is None

    def slow_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", slow_run)
    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.X11) is None


def test_active_window_title_returns_none_on_wayland_and_unsupported_os(monkeypatch) -> None:
    # No probe may even run: Wayland's portal deliberately doesn't expose
    # other apps' titles, and macOS capture isn't started.
    def run_must_not_happen(*a, **k):
        raise AssertionError("no subprocess probe expected on this platform")

    monkeypatch.setattr("clipersal.window_capture.subprocess.run", run_must_not_happen)

    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.WAYLAND) is None
    assert window_capture.active_window_title(OS.LINUX, LinuxSessionType.UNKNOWN) is None
    assert window_capture.active_window_title(OS.MACOS) is None
    assert window_capture.active_window_title(OS.OTHER) is None
