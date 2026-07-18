import subprocess

from clipersal.monitors import MonitorInfo, _list_linux_monitors, list_monitors
from clipersal.platform_detect import OS

_XRANDR_OUTPUT = """\
Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 16384 x 16384
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 530mm x 300mm
   1920x1080     60.00*+
DP-1 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 530mm x 300mm
   1920x1080     60.00*+
DP-2 disconnected (normal left inverted right x axis y axis)
"""


def test_list_linux_monitors_parses_xrandr_output(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=_XRANDR_OUTPUT, stderr="")

    monkeypatch.setattr("clipersal.monitors.subprocess.run", fake_run)

    result = _list_linux_monitors()

    assert result == [
        MonitorInfo(index=0, name="HDMI-1", x=0, y=0, width=1920, height=1080, is_primary=True),
        MonitorInfo(index=1, name="DP-1", x=1920, y=0, width=1920, height=1080, is_primary=False),
    ]


def test_list_linux_monitors_returns_empty_when_xrandr_missing(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("xrandr not found")

    monkeypatch.setattr("clipersal.monitors.subprocess.run", fake_run)

    assert _list_linux_monitors() == []


def test_list_linux_monitors_ignores_disconnected_outputs(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="DP-2 disconnected (normal ...)\n", stderr="")

    monkeypatch.setattr("clipersal.monitors.subprocess.run", fake_run)

    assert _list_linux_monitors() == []


def test_list_monitors_returns_empty_for_unsupported_os() -> None:
    assert list_monitors(OS.OTHER) == []
    assert list_monitors(OS.MACOS) == []
