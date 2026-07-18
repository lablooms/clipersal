import subprocess

from clipersal.platform_detect import OS
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


def test_list_windows_returns_empty_for_unsupported_os() -> None:
    assert list_windows(OS.OTHER) == []
    assert list_windows(OS.MACOS) == []
