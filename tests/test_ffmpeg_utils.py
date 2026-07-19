import subprocess

import pytest

from clipersal.ffmpeg_utils import (
    QUALITY_PRESETS,
    WAYLAND_PORTAL_KIND,
    build_video_capture_source,
    build_wayland_input_args,
    build_window_capture_source,
    encoder_output_args,
    find_microphone_source,
    list_encoders,
    list_filters,
    list_microphones,
    resolve_quality_preset,
)
from clipersal.monitors import MonitorInfo
from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.wayland_gstreamer import GStreamerNotFoundError
from clipersal.window_capture import WindowInfo

_WINDOWS = [WindowInfo(handle="12345", title="My App", x=100, y=50, width=800, height=600)]

_MONITORS = [
    MonitorInfo(index=0, name="Monitor 0", x=0, y=0, width=1920, height=1080, is_primary=True),
    MonitorInfo(index=1, name="Monitor 1", x=1920, y=0, width=2560, height=1440, is_primary=False),
]


def test_resolve_quality_preset_custom_uses_bitrate_directly_with_no_speed() -> None:
    bitrate, speed = resolve_quality_preset("custom", "libx264", custom_bitrate="12M")
    assert bitrate == "12M"
    assert speed is None


def test_resolve_quality_preset_unrecognized_falls_back_to_custom_behavior() -> None:
    # A hand-edited config with a typo'd preset name should never crash --
    # fall back to the raw bitrate exactly like "custom" does.
    bitrate, speed = resolve_quality_preset("typo", "libx264", custom_bitrate="6M")
    assert bitrate == "6M"
    assert speed is None


def test_resolve_quality_preset_balanced_maps_bitrate_and_encoder_speed() -> None:
    bitrate, speed = resolve_quality_preset("balanced", "h264_nvenc", custom_bitrate="99M")
    assert bitrate == f"{QUALITY_PRESETS['balanced']['bitrate_mbps']}M"
    assert speed == "p4"


def test_resolve_quality_preset_performance_and_quality_differ() -> None:
    perf_bitrate, perf_speed = resolve_quality_preset("performance", "libx264", custom_bitrate="0M")
    quality_bitrate, quality_speed = resolve_quality_preset("quality", "libx264", custom_bitrate="0M")
    assert perf_bitrate != quality_bitrate
    assert perf_speed != quality_speed


def test_resolve_quality_preset_missing_encoder_speed_entry_returns_none() -> None:
    # h264_vaapi has no speed knob in the preset tables (see encoder_output_args).
    _bitrate, speed = resolve_quality_preset("balanced", "h264_vaapi", custom_bitrate="0M")
    assert speed is None


def test_encoder_output_args_default_speed_unchanged_when_none() -> None:
    assert encoder_output_args("h264_nvenc", "8M") == ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "8M"]
    assert encoder_output_args("libx264", "8M") == ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "8M"]


def test_encoder_output_args_speed_override_applied() -> None:
    assert encoder_output_args("h264_nvenc", "4M", speed="p1") == [
        "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-b:v", "4M",
    ]
    assert encoder_output_args("libx264", "16M", speed="fast") == [
        "-c:v", "libx264", "-preset", "fast", "-b:v", "16M",
    ]


def test_encoder_output_args_qsv_speed_only_added_when_given() -> None:
    assert encoder_output_args("h264_qsv", "8M") == ["-c:v", "h264_qsv", "-b:v", "8M"]
    assert encoder_output_args("h264_qsv", "8M", speed="slow") == ["-c:v", "h264_qsv", "-b:v", "8M", "-preset", "slow"]


def test_encoder_output_args_vaapi_ignores_speed() -> None:
    assert encoder_output_args("h264_vaapi", "8M", speed="fast") == ["-c:v", "h264_vaapi", "-b:v", "8M"]


def test_list_encoders_returns_empty_when_probe_times_out(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    assert list_encoders("ffmpeg") == set()


def test_list_encoders_returns_empty_when_probe_fails(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    assert list_encoders("ffmpeg") == set()


def test_list_filters_returns_empty_when_probe_times_out(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    assert list_filters("ffmpeg") == set()


def test_list_filters_returns_empty_when_probe_fails(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    assert list_filters("ffmpeg") == set()


def test_windows_ddagrab_monitor_index_zero_matches_current_default(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_filters", lambda ffmpeg_path: {"ddagrab"})

    source = build_video_capture_source("ffmpeg", OS.WINDOWS, None, framerate=30, monitor_index=0)

    assert source.kind == "ddagrab"
    assert source.input_args == ["-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30"]


def test_windows_ddagrab_uses_output_idx_directly_for_monitor_index(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_filters", lambda ffmpeg_path: {"ddagrab"})

    source = build_video_capture_source("ffmpeg", OS.WINDOWS, None, framerate=30, monitor_index=2)

    assert source.input_args == ["-f", "lavfi", "-i", "ddagrab=output_idx=2:framerate=30"]


def test_windows_gdigrab_fallback_monitor_index_zero_captures_whole_desktop(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_filters", lambda ffmpeg_path: set())

    source = build_video_capture_source("ffmpeg", OS.WINDOWS, None, framerate=30, monitor_index=0)

    assert source.kind == "gdigrab"
    assert source.input_args == ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]


def test_windows_gdigrab_fallback_crops_to_specific_monitor(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_filters", lambda ffmpeg_path: set())
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_monitors", lambda os_: _MONITORS)

    source = build_video_capture_source("ffmpeg", OS.WINDOWS, None, framerate=30, monitor_index=1)

    assert source.kind == "gdigrab-monitor"
    assert source.input_args == [
        "-f", "gdigrab", "-framerate", "30",
        "-offset_x", "1920", "-offset_y", "0",
        "-video_size", "2560x1440",
        "-i", "desktop",
    ]


def test_windows_gdigrab_falls_back_to_whole_desktop_when_monitor_not_found(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_filters", lambda ffmpeg_path: set())
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_monitors", lambda os_: _MONITORS)

    source = build_video_capture_source("ffmpeg", OS.WINDOWS, None, framerate=30, monitor_index=9)

    assert source.kind == "gdigrab"
    assert source.input_args == ["-f", "gdigrab", "-framerate", "30", "-i", "desktop"]


def test_linux_x11grab_monitor_index_zero_captures_whole_display(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0.0")

    source = build_video_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, framerate=30, monitor_index=0)

    assert source.kind == "x11grab"
    assert source.input_args == ["-f", "x11grab", "-framerate", "30", "-i", ":0.0"]


def test_linux_x11grab_crops_to_specific_monitor(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0.0")
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_monitors", lambda os_: _MONITORS)

    source = build_video_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, framerate=30, monitor_index=1)

    assert source.kind == "x11grab-monitor"
    assert source.input_args == [
        "-f", "x11grab", "-framerate", "30",
        "-video_size", "2560x1440",
        "-i", ":0.0+1920,0",
    ]


def test_linux_x11grab_falls_back_to_whole_display_when_monitor_not_found(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0.0")
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_monitors", lambda os_: _MONITORS)

    source = build_video_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, framerate=30, monitor_index=9)

    assert source.kind == "x11grab"
    assert source.input_args == ["-f", "x11grab", "-framerate", "30", "-i", ":0.0"]


_DSHOW_AUDIO_STDERR = (
    '"Microphone (Realtek Audio)"  (audio)\n'
    '"CABLE Output (VB-Audio Virtual Cable)"  (audio)\n'
    '"Stereo Mix (Realtek Audio)"  (audio)\n'
)


def test_list_microphones_windows_excludes_loopback_hint_devices(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr=_DSHOW_AUDIO_STDERR)

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    names = list_microphones("ffmpeg", OS.WINDOWS)

    assert names == ["Microphone (Realtek Audio)"]


def test_list_microphones_linux_excludes_monitor_sources(monkeypatch) -> None:
    pactl_output = (
        "0\talsa_input.pci-0000_00_1f.3.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tRUNNING\n"
        "1\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tRUNNING\n"
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=pactl_output, stderr="")

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    names = list_microphones("ffmpeg", OS.LINUX)

    assert names == ["alsa_input.pci-0000_00_1f.3.analog-stereo"]


def test_list_microphones_returns_empty_when_probe_fails(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr("clipersal.ffmpeg_utils.subprocess.run", fake_run)

    assert list_microphones("ffmpeg", OS.WINDOWS) == []


def test_find_microphone_source_windows_builds_dshow_args() -> None:
    source = find_microphone_source(OS.WINDOWS, "Microphone (Realtek Audio)")
    assert source.input_args == ["-f", "dshow", "-i", "audio=Microphone (Realtek Audio)"]
    assert source.description == "Microphone (Realtek Audio)"


def test_find_microphone_source_linux_builds_pulse_args() -> None:
    source = find_microphone_source(OS.LINUX, "alsa_input.usb-mic")
    assert source.input_args == ["-f", "pulse", "-i", "alsa_input.usb-mic"]


def test_find_microphone_source_unsupported_os_returns_none() -> None:
    assert find_microphone_source(OS.MACOS, "whatever") is None


def test_windows_window_capture_always_uses_gdigrab_by_title() -> None:
    source = build_window_capture_source("ffmpeg", OS.WINDOWS, None, "My App", framerate=30)

    assert source.kind == "gdigrab-window"
    assert source.input_args == ["-f", "gdigrab", "-framerate", "30", "-i", "title=My App"]


def test_linux_window_capture_crops_to_window_geometry(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0.0")
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_windows", lambda os_: _WINDOWS)

    source = build_window_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, "My App", framerate=30)

    assert source.kind == "x11grab-window"
    assert source.input_args == [
        "-f", "x11grab", "-framerate", "30",
        "-video_size", "800x600",
        "-i", ":0.0+100,50",
    ]


def test_linux_window_capture_falls_back_to_whole_display_when_window_not_found(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0.0")
    monkeypatch.setattr("clipersal.ffmpeg_utils.list_windows", lambda os_: _WINDOWS)

    source = build_window_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.X11, "Nonexistent Window", framerate=30)

    assert source.kind == "x11grab"
    assert source.input_args == ["-f", "x11grab", "-framerate", "30", "-i", ":0.0"]


# ---- Wayland portal capture source ------------------------------------------
#
# resolve_setup must stay dialog-free, so the Wayland branch only returns a
# marker CaptureSource (the real input args need the portal handshake's stream
# size, known at capture-start) -- but it DOES probe GStreamer, so a missing
# gst-launch fails at startup with the actionable message.


def test_linux_wayland_desktop_returns_portal_marker_and_probes_gstreamer(monkeypatch) -> None:
    probes = []
    monkeypatch.setattr(
        "clipersal.wayland_gstreamer.ensure_gstreamer",
        lambda: probes.append("probe") or "/usr/bin/gst-launch-1.0",
    )

    source = build_video_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.WAYLAND, framerate=30, monitor_index=0)

    assert source.kind == WAYLAND_PORTAL_KIND
    assert source.input_args == []  # real args are built at capture-start from the stream size
    assert source.video_filter is None
    assert source.portal_source_type == "monitor"
    assert probes == ["probe"]


def test_linux_wayland_gstreamer_probe_failure_propagates(monkeypatch) -> None:
    def boom():
        raise GStreamerNotFoundError("GStreamer was not found on PATH (fake)")

    monkeypatch.setattr("clipersal.wayland_gstreamer.ensure_gstreamer", boom)

    with pytest.raises(GStreamerNotFoundError):
        build_video_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.WAYLAND, framerate=30, monitor_index=0)


def test_linux_wayland_window_mode_maps_to_window_source_type(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.ensure_gstreamer", lambda: "/usr/bin/gst-launch-1.0")

    source = build_window_capture_source("ffmpeg", OS.LINUX, LinuxSessionType.WAYLAND, "Any Title", framerate=30)

    assert source.kind == WAYLAND_PORTAL_KIND
    assert source.portal_source_type == "window"


def test_build_wayland_input_args_exact_argv() -> None:
    assert build_wayland_input_args(1920, 1080, 30) == [
        "-f", "rawvideo",
        "-pix_fmt", "bgra",
        "-video_size", "1920x1080",
        "-framerate", "30",
        "-i", "pipe:0",
    ]
