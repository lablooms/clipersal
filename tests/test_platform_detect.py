import subprocess
import sys
from types import SimpleNamespace

from clipersal import platform_detect
from clipersal.platform_detect import LinuxSessionType, OS, get_linux_session_type, system_dark_preferred


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


# ---- system_dark_preferred ----------------------------------------------------
#
# The probes are faked at their boundary: a stand-in `winreg` module in
# sys.modules (the real import inside platform_detect picks it up from there)
# and a stand-in subprocess.run for the gsettings call. No real registry or
# gsettings is touched, so the tests run identically on every OS.


class _FakeRegKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_fake_winreg(monkeypatch, value=None, open_error=None, query_error=None):
    fake = SimpleNamespace(HKEY_CURRENT_USER="HKCU")
    if open_error is not None:
        def raise_on_open(*_args):
            raise open_error

        fake.OpenKey = raise_on_open
    else:
        fake.OpenKey = lambda *_args: _FakeRegKey()
        if query_error is not None:
            def raise_on_query(*_args):
                raise query_error

            fake.QueryValueEx = raise_on_query
        else:
            fake.QueryValueEx = lambda _key, _name: (value, 4)  # (DWORD value, REG_DWORD)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_windows_dark_when_apps_use_light_theme_is_zero(monkeypatch) -> None:
    _install_fake_winreg(monkeypatch, value=0)
    assert system_dark_preferred(OS.WINDOWS) is True


def test_windows_light_when_apps_use_light_theme_is_one(monkeypatch) -> None:
    _install_fake_winreg(monkeypatch, value=1)
    assert system_dark_preferred(OS.WINDOWS) is False


def test_windows_missing_key_or_value_reads_as_light(monkeypatch) -> None:
    _install_fake_winreg(monkeypatch, open_error=FileNotFoundError())
    assert system_dark_preferred(OS.WINDOWS) is False

    _install_fake_winreg(monkeypatch, query_error=FileNotFoundError())
    assert system_dark_preferred(OS.WINDOWS) is False


def test_windows_registry_failure_never_raises(monkeypatch) -> None:
    _install_fake_winreg(monkeypatch, open_error=OSError("registry is wedged"))
    assert system_dark_preferred(OS.WINDOWS) is False

    # Even a missing winreg module (inconceivable but cheap to cover) is fine.
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert system_dark_preferred(OS.WINDOWS) is False


def test_linux_dark_when_color_scheme_prefers_dark(monkeypatch) -> None:
    monkeypatch.setattr(
        platform_detect.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="'prefer-dark'\n"),
    )
    assert system_dark_preferred(OS.LINUX) is True


def test_linux_light_when_color_scheme_is_default(monkeypatch) -> None:
    monkeypatch.setattr(
        platform_detect.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="'default'\n"),
    )
    assert system_dark_preferred(OS.LINUX) is False


def test_linux_gsettings_failure_reads_as_light(monkeypatch) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("gsettings not installed")

    monkeypatch.setattr(platform_detect.subprocess, "run", missing)
    assert system_dark_preferred(OS.LINUX) is False

    def hangs(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="gsettings", timeout=3)

    monkeypatch.setattr(platform_detect.subprocess, "run", hangs)
    assert system_dark_preferred(OS.LINUX) is False


def test_gsettings_probe_uses_the_gnome_color_scheme_key(monkeypatch) -> None:
    seen = {}

    def record(cmd, **_kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout="'prefer-dark'\n")

    monkeypatch.setattr(platform_detect.subprocess, "run", record)
    assert system_dark_preferred(OS.LINUX) is True
    assert seen["cmd"] == ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]


def test_macos_and_other_have_no_dark_mode_probe(monkeypatch) -> None:
    # No probe is even attempted off Windows/Linux: any subprocess/registry
    # access would blow up these fakes, so False proves none ran.
    monkeypatch.setattr(
        platform_detect.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert system_dark_preferred(OS.MACOS) is False
    assert system_dark_preferred(OS.OTHER) is False
