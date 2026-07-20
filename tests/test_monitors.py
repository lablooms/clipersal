import ctypes
import subprocess
import sys

import pytest

from clipersal import monitors
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


def test_xrandr_output_is_decoded_as_utf8_with_replacement(monkeypatch) -> None:
    # Bare text=True decodes with the LOCALE encoding under strict errors --
    # a non-ASCII output name under a C/POSIX locale would escape this
    # module's never-raises contract as UnicodeDecodeError.
    captured_kwargs = {}

    def fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=_XRANDR_OUTPUT, stderr="")

    monkeypatch.setattr("clipersal.monitors.subprocess.run", fake_run)

    _list_linux_monitors()

    assert captured_kwargs["encoding"] == "utf-8"
    assert captured_kwargs["errors"] == "replace"


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes prototypes are Windows-only")
def test_user32_prototypes_use_real_handle_types() -> None:
    # Without declared argtypes ctypes defaults every argument to C int and
    # truncates 64-bit HMONITORs on their way back into GetMonitorInfoW --
    # these declarations are what keep handles intact (the import of the
    # module itself installed them).
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    assert user32.EnumDisplayMonitors.argtypes == [
        wintypes.HDC,
        ctypes.POINTER(monitors._RECT),
        monitors._MonitorEnumProc,
        wintypes.LPARAM,
    ]
    assert user32.EnumDisplayMonitors.restype == wintypes.BOOL
    assert user32.GetMonitorInfoW.argtypes == [wintypes.HMONITOR, ctypes.POINTER(monitors._MONITORINFOEX)]
    assert user32.GetMonitorInfoW.restype == wintypes.BOOL


def test_module_docstring_does_not_overstate_ddagrab_ordering() -> None:
    # EnumDisplayMonitors' order is OS-unspecified and is a different
    # enumeration from DXGI's EnumOutputs (what ddagrab's output_idx numbers
    # by) -- the docstring must say "usually", not claim a guarantee.
    doc = monitors.__doc__
    assert "unspecified" in doc
    assert "usually" in doc.lower()
    assert "same as ddagrab" not in doc
