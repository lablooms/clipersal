"""ffmpeg discovery, encoder selection, and platform capture/audio source args.

Implements the two-step encoder detection, the Wayland capture caveat, and
the audio-loopback caveat -- see ARCHITECTURE.md for the full rationale
behind each.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from clipersal.monitors import list_monitors
from clipersal.platform_detect import OS, LinuxSessionType
from clipersal.subprocess_utils import NO_WINDOW_KWARGS
from clipersal.window_capture import list_windows

log = logging.getLogger(__name__)

_SMOKE_TEST_TIMEOUT = 10  # seconds
_DSHOW_LIST_TIMEOUT = 10
_PACTL_TIMEOUT = 5

# Substrings (matched case-insensitively) of known loopback-capable dshow
# device names, in priority order. None of these ship with a stock Windows
# install -- see the "Audio capture caveat" section in ARCHITECTURE.md.
_WINDOWS_LOOPBACK_DEVICE_HINTS = ("virtual-audio-capturer", "cable output", "stereo mix")

# Encoder priority lists, most-preferred first. libx264 is always the final
# fallback and is appended by pick_encoder regardless of platform.
_ENCODER_PRIORITY = {
    OS.WINDOWS: ["h264_nvenc", "h264_qsv"],
    OS.LINUX: ["h264_nvenc", "h264_vaapi", "h264_qsv"],
    OS.MACOS: [],
    OS.OTHER: [],
}


class FfmpegNotFoundError(RuntimeError):
    pass


class NoWorkingEncoderError(RuntimeError):
    pass


# Marker CaptureSource.kind for the Wayland portal capture path. The real
# ffmpeg input args depend on the stream size the portal handshake returns,
# so resolve_setup can only return a placeholder -- capture.py builds the
# actual args at capture-start via build_wayland_input_args. Opening the
# portal session at probe time is not an option: the first Start shows the
# desktop's share-dialog, and resolve_setup must stay dialog-free.
WAYLAND_PORTAL_KIND = "wayland-portal"


@dataclass
class CaptureSource:
    input_args: list[str]
    video_filter: str | None
    kind: str
    # Only meaningful when kind == WAYLAND_PORTAL_KIND: which source type the
    # portal's share-dialog asks for ("monitor" or "window"). Defaults to
    # "monitor" so every pre-Wayland construction site keeps its behavior.
    portal_source_type: str = "monitor"


@dataclass
class AudioSource:
    input_args: list[str]
    description: str


def find_ffmpeg() -> str:
    """Locate a working ffmpeg binary on PATH, or raise a clear, actionable error."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegNotFoundError(
            "ffmpeg was not found on PATH. Install it and make sure it's on PATH:\n"
            "  Windows: winget install Gyan.FFmpeg   (or choco install ffmpeg)\n"
            "  Linux:   sudo apt install ffmpeg   (or dnf/pacman equivalent)"
        )
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=_SMOKE_TEST_TIMEOUT, **NO_WINDOW_KWARGS
        )
    except OSError as exc:
        raise FfmpegNotFoundError(f"Found ffmpeg at {path} but it failed to run: {exc}") from exc
    if result.returncode != 0:
        raise FfmpegNotFoundError(
            f"Found ffmpeg at {path} but `ffmpeg -version` exited {result.returncode}"
        )
    first_line = result.stdout.splitlines()[0] if result.stdout else "(no version output)"
    log.info("Using ffmpeg at %s (%s)", path, first_line)
    return path


def list_encoders(ffmpeg_path: str) -> set[str]:
    # A hung or unrunnable ffmpeg here must not escape: startup only catches
    # the specific resolve_setup exceptions, and the Settings apply path
    # reaches this through restart_capture from a Qt slot. Returning an empty
    # set instead lets encoder auto-detection fall through to
    # NoWorkingEncoderError, which both call sites already handle.
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=_SMOKE_TEST_TIMEOUT,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list ffmpeg encoders: %s", exc)
        return set()
    names = set()
    for line in result.stdout.splitlines():
        # Encoder lines look like: " V....D h264_nvenc  NVIDIA NVENC H.264 encoder ..."
        match = re.match(r"\s*[VAS.][F.][S.][X.][B.][D.]\s+(\S+)", line)
        if match:
            names.add(match.group(1))
    return names


def list_filters(ffmpeg_path: str) -> set[str]:
    # Same containment as list_encoders above: on failure an empty set just
    # means "no filters detected", which degrades ddagrab to the gdigrab
    # fallback instead of crashing startup or a Settings-triggered restart.
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=_SMOKE_TEST_TIMEOUT,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list ffmpeg filters: %s", exc)
        return set()
    names = set()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*[T.][S.][C.]\s+(\S+)", line)
        if match:
            names.add(match.group(1))
    return names


def encoder_global_args(encoder: str) -> list[str]:
    """ffmpeg args that must appear before any -i for this encoder (e.g. hw device setup)."""
    if encoder == "h264_vaapi":
        # Assumes the primary GPU is the first DRI render node. A later phase
        # should probe /dev/dri/renderD* and/or make this configurable.
        return ["-vaapi_device", "/dev/dri/renderD128"]
    return []


def encoder_filter_fragment(encoder: str) -> str | None:
    """A -vf fragment this encoder needs applied to its input frames, if any."""
    if encoder == "h264_vaapi":
        return "format=nv12,hwupload"
    return None


def encoder_output_args(encoder: str, bitrate: str, speed: str | None = None) -> list[str]:
    """`speed` overrides each encoder's default preset/speed knob -- used by
    the quality-preset system below. None keeps today's hardcoded defaults
    (p4 / veryfast), so callers that don't care about speed (the encoder
    smoke test, "custom" quality preset) see no behavior change.
    """
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", speed or "p4", "-rc", "vbr", "-b:v", bitrate]
    if encoder == "h264_qsv":
        args = ["-c:v", encoder, "-b:v", bitrate]
        if speed:
            args += ["-preset", speed]
        return args
    if encoder == "h264_vaapi":
        # VAAPI's rate control doesn't have an equivalent software-style
        # preset speed knob, so `speed` is accepted but unused here.
        return ["-c:v", encoder, "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", speed or "veryfast", "-b:v", bitrate]


# Named quality presets: collapse the raw bitrate+preset-speed pair
# into three simple choices for the common case, while "custom" (the default,
# for zero behavior change on existing configs) keeps the old raw-bitrate
# slider in charge. Speed is per-encoder since each encoder's preset naming
# is different (NVENC's p1-p7, libx264's named presets, QSV's named presets);
# VAAPI has no entry since encoder_output_args ignores speed for it anyway.
QUALITY_PRESETS: dict[str, dict] = {
    "performance": {
        "bitrate_mbps": 4,
        "speed": {"h264_nvenc": "p1", "h264_qsv": "veryfast", "libx264": "ultrafast"},
    },
    "balanced": {
        "bitrate_mbps": 8,
        "speed": {"h264_nvenc": "p4", "h264_qsv": "medium", "libx264": "veryfast"},
    },
    "quality": {
        "bitrate_mbps": 16,
        "speed": {"h264_nvenc": "p6", "h264_qsv": "slow", "libx264": "fast"},
    },
}


def resolve_quality_preset(preset: str, encoder: str, custom_bitrate: str) -> tuple[str, str | None]:
    """Returns (bitrate_string, speed_or_None) for `_build_command` to pass
    to encoder_output_args. "custom" (or an unrecognized preset name, e.g.
    from a hand-edited config) falls back to custom_bitrate with no speed
    override -- today's exact behavior.
    """
    spec = QUALITY_PRESETS.get(preset)
    if spec is None:
        return custom_bitrate, None
    return f"{spec['bitrate_mbps']}M", spec["speed"].get(encoder)


# Smoke-test content: a modest amount of desktop-resolution-ish frames rather
# than a handful of tiny ones, so a genuinely non-functional or pathologically
# slow encoder (e.g. falling back to a software emulation path with no real
# GPU behind it) gets caught by the timing check below rather than just the
# exit-code check.
_SMOKE_TEST_DURATION_S = 1.0
_SMOKE_TEST_MAX_REALTIME_FACTOR = 2.5  # allow up to 2.5x slower than real time
_SMOKE_TEST_FIXED_OVERHEAD_S = 2.0  # process/driver startup slack


def _smoke_test_encoder(ffmpeg_path: str, encoder: str) -> bool:
    """Encode ~1s of 720p test content and confirm the encoder both runs and
    keeps up close to real time. See ARCHITECTURE.md's "Encoder selection" section.
    """
    filter_fragment = encoder_filter_fragment(encoder)
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *encoder_global_args(encoder),
        "-f",
        "lavfi",
        "-i",
        f"color=size=1280x720:rate=30:duration={_SMOKE_TEST_DURATION_S}",
    ]
    if filter_fragment:
        cmd += ["-vf", filter_fragment]
    cmd += [*encoder_output_args(encoder, "4M"), "-f", "null", "-"]

    max_allowed_s = _SMOKE_TEST_DURATION_S * _SMOKE_TEST_MAX_REALTIME_FACTOR + _SMOKE_TEST_FIXED_OVERHEAD_S
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SMOKE_TEST_TIMEOUT, **NO_WINDOW_KWARGS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Encoder %s smoke test raised %s", encoder, exc)
        return False
    elapsed = time.monotonic() - start

    if result.returncode != 0:
        log.debug("Encoder %s smoke test failed: %s", encoder, result.stderr.strip()[-500:])
        return False
    if elapsed > max_allowed_s:
        log.info(
            "Encoder %s ran but took %.1fs to encode %.1fs of 720p test content "
            "(max allowed %.1fs) -- too slow to keep up with real-time capture, skipping",
            encoder,
            elapsed,
            _SMOKE_TEST_DURATION_S,
            max_allowed_s,
        )
        return False
    return True


def pick_encoder(ffmpeg_path: str, os_: OS, forced: str | None = None) -> str:
    """Pick a working H.264 encoder: NVENC > platform HW (VAAPI/QSV) > libx264.

    Each candidate is checked for both compile-time availability and an actual
    working smoke-test encode before being accepted -- an encoder can be
    listed in `ffmpeg -encoders` with no working GPU/driver behind it.
    """
    available = list_encoders(ffmpeg_path)

    if forced is not None:
        if forced not in available:
            raise NoWorkingEncoderError(f"Forced encoder {forced!r} is not available in this ffmpeg build")
        if not _smoke_test_encoder(ffmpeg_path, forced):
            raise NoWorkingEncoderError(f"Forced encoder {forced!r} failed its smoke test")
        return forced

    candidates = [*_ENCODER_PRIORITY.get(os_, []), "libx264"]
    for candidate in candidates:
        if candidate not in available:
            log.debug("Encoder %s not compiled into this ffmpeg build, skipping", candidate)
            continue
        if _smoke_test_encoder(ffmpeg_path, candidate):
            log.info("Selected encoder: %s", candidate)
            return candidate
        log.info("Encoder %s is available but failed its smoke test, skipping", candidate)

    raise NoWorkingEncoderError(
        "No working H.264 encoder found, not even libx264 -- is this ffmpeg build broken?"
    )


def _find_monitor(os_: OS, monitor_index: int):
    for mon in list_monitors(os_):
        if mon.index == monitor_index:
            return mon
    return None


def build_video_capture_source(
    ffmpeg_path: str,
    os_: OS,
    session_type: LinuxSessionType | None,
    framerate: int = 30,
    monitor_index: int = 0,
) -> CaptureSource:
    """monitor_index == 0 (the default) is exactly today's pre-Phase-8
    behavior on every backend -- ddagrab's output_idx already defaulted to
    0, and gdigrab/x11grab already captured the whole desktop/display with
    no cropping. A non-zero index is resolved to a specific monitor's
    geometry via monitors.list_monitors and only then changes the command
    built below, so existing single-monitor setups see no behavior change.
    """
    if os_ == OS.WINDOWS:
        if "ddagrab" in list_filters(ffmpeg_path):
            # ddagrab captures one adapter output per instance (Desktop
            # Duplication API), so output_idx IS the monitor index natively
            # -- no geometry lookup needed here, unlike the gdigrab fallback.
            return CaptureSource(
                input_args=["-f", "lavfi", "-i", f"ddagrab=output_idx={monitor_index}:framerate={framerate}"],
                video_filter="hwdownload,format=bgra,format=nv12",
                kind="ddagrab",
            )
        log.warning("ddagrab filter not available in this ffmpeg build, falling back to gdigrab")
        if monitor_index == 0:
            return CaptureSource(
                input_args=["-f", "gdigrab", "-framerate", str(framerate), "-i", "desktop"],
                video_filter=None,
                kind="gdigrab",
            )
        mon = _find_monitor(os_, monitor_index)
        if mon is None:
            log.warning("Monitor %d not found; capturing the full desktop instead", monitor_index)
            return CaptureSource(
                input_args=["-f", "gdigrab", "-framerate", str(framerate), "-i", "desktop"],
                video_filter=None,
                kind="gdigrab",
            )
        return CaptureSource(
            input_args=[
                "-f", "gdigrab",
                "-framerate", str(framerate),
                "-offset_x", str(mon.x),
                "-offset_y", str(mon.y),
                "-video_size", f"{mon.width}x{mon.height}",
                "-i", "desktop",
            ],
            video_filter=None,
            kind="gdigrab-monitor",
        )

    if os_ == OS.LINUX:
        if session_type == LinuxSessionType.WAYLAND:
            return _build_wayland_portal_source(source_type="monitor")
        if session_type == LinuxSessionType.X11:
            display = os.environ.get("DISPLAY", ":0.0")
            if monitor_index == 0:
                return CaptureSource(
                    input_args=["-f", "x11grab", "-framerate", str(framerate), "-i", display],
                    video_filter=None,
                    kind="x11grab",
                )
            mon = _find_monitor(os_, monitor_index)
            if mon is None:
                log.warning("Monitor %d not found; capturing the full display instead", monitor_index)
                return CaptureSource(
                    input_args=["-f", "x11grab", "-framerate", str(framerate), "-i", display],
                    video_filter=None,
                    kind="x11grab",
                )
            return CaptureSource(
                input_args=[
                    "-f", "x11grab",
                    "-framerate", str(framerate),
                    "-video_size", f"{mon.width}x{mon.height}",
                    "-i", f"{display}+{mon.x},{mon.y}",
                ],
                video_filter=None,
                kind="x11grab-monitor",
            )
        raise RuntimeError(
            "Could not determine whether this is an X11 or Wayland session "
            "(XDG_SESSION_TYPE, WAYLAND_DISPLAY, and DISPLAY are all unset)."
        )

    raise NotImplementedError(f"Video capture is not implemented for {os_} yet")


def _find_window(os_: OS, window_title: str):
    for win in list_windows(os_):
        if win.title == window_title:
            return win
    return None


def build_window_capture_source(
    ffmpeg_path: str,
    os_: OS,
    session_type: LinuxSessionType | None,
    window_title: str,
    framerate: int = 30,
) -> CaptureSource:
    """Capture a single application window instead of a monitor/the whole
    desktop. Separate from build_video_capture_source (rather than a mode
    flag on it) since the two pick genuinely different ffmpeg mechanisms --
    gdigrab's own by-title window mode on Windows, geometry-cropped x11grab
    on Linux -- and window capture always forces gdigrab on Windows even
    when ddagrab is otherwise available/preferred, since ddagrab (Desktop
    Duplication API) captures a whole adapter output, never a single window.
    """
    if os_ == OS.WINDOWS:
        return CaptureSource(
            input_args=["-f", "gdigrab", "-framerate", str(framerate), "-i", f"title={window_title}"],
            video_filter=None,
            kind="gdigrab-window",
        )

    if os_ == OS.LINUX:
        if session_type == LinuxSessionType.WAYLAND:
            return _build_wayland_portal_source(source_type="window")
        if session_type == LinuxSessionType.X11:
            display = os.environ.get("DISPLAY", ":0.0")
            win = _find_window(os_, window_title)
            if win is None:
                log.warning("Window %r not found; capturing the full display instead", window_title)
                return CaptureSource(
                    input_args=["-f", "x11grab", "-framerate", str(framerate), "-i", display],
                    video_filter=None,
                    kind="x11grab",
                )
            # wmctrl can report a negative x/y for a window on another
            # viewport or hanging off the left/top screen edge, and
            # XParseGeometry reads "display+-2552,96" as an offset relative
            # to the RIGHT/bottom edge -- passing it through would capture
            # the wrong region entirely, so clamp to the screen edge.
            x, y = max(0, win.x), max(0, win.y)
            return CaptureSource(
                input_args=[
                    "-f", "x11grab",
                    "-framerate", str(framerate),
                    "-video_size", f"{win.width}x{win.height}",
                    "-i", f"{display}+{x},{y}",
                ],
                video_filter=None,
                kind="x11grab-window",
            )
        raise RuntimeError(
            "Could not determine whether this is an X11 or Wayland session "
            "(XDG_SESSION_TYPE, WAYLAND_DISPLAY, and DISPLAY are all unset)."
        )

    raise NotImplementedError(f"Video capture is not implemented for {os_} yet")


def _build_wayland_portal_source(source_type: str) -> CaptureSource:
    """The Wayland capture source: a placeholder, not real ffmpeg args.

    Wayland capture goes through the xdg-desktop-portal ScreenCast API +
    PipeWire (portal_screencast.py), with a GStreamer bridge feeding raw
    frames into ffmpeg's stdin (wayland_gstreamer.py) -- no released ffmpeg
    has a PipeWire input device. The actual input args depend on the stream
    size the portal handshake returns, so they can't be built here; and the
    portal session can't be opened here either, since the first Start shows
    the desktop's share-dialog and resolve_setup must stay dialog-free.
    capture.py acquires the session and builds the real args at
    capture-start.

    GStreamer is probed NOW, though: a missing gst-launch-1.0/pipewiresrc
    fails fast at startup with the actionable install message (typed errors
    cli.py surfaces cleanly) instead of at the first capture start.
    """
    from clipersal import wayland_gstreamer

    wayland_gstreamer.ensure_gstreamer()
    return CaptureSource(
        input_args=[], video_filter=None, kind=WAYLAND_PORTAL_KIND, portal_source_type=source_type
    )


def build_wayland_input_args(width: int, height: int, framerate: int) -> list[str]:
    """ffmpeg input args for the Wayland portal path: the GStreamer frame
    pump writes raw BGRA frames into ffmpeg's stdin, so ffmpeg reads rawvideo
    from pipe:0 at the portal-reported stream size. Kept separate (and pure)
    so the exact argv shape is unit-testable without a portal session.
    """
    return [
        "-f", "rawvideo",
        "-pix_fmt", "bgra",
        "-video_size", f"{width}x{height}",
        "-framerate", str(framerate),
        "-i", "pipe:0",
    ]


def find_audio_source(ffmpeg_path: str, os_: OS) -> AudioSource | None:
    """Best-effort loopback/monitor audio source discovery.

    Returns None (not an error) when no usable source is found -- callers
    should log a warning and proceed video-only. See the "Audio capture
    caveat" section in ARCHITECTURE.md for why this can't just always work.
    """
    if os_ == OS.WINDOWS:
        return _find_windows_audio_source(ffmpeg_path)
    if os_ == OS.LINUX:
        return _find_linux_audio_source()
    return None


def _list_windows_dshow_audio_devices(ffmpeg_path: str) -> list[str]:
    """Shared by both loopback discovery and microphone enumeration below --
    both just want the raw dshow audio device name list, they differ only in
    which names they keep.
    """
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=_DSHOW_LIST_TIMEOUT,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list dshow devices: %s", exc)
        return []
    return re.findall(r'"([^"]+)"\s+\(audio\)', result.stderr)


def _find_windows_audio_source(ffmpeg_path: str) -> AudioSource | None:
    device_names = _list_windows_dshow_audio_devices(ffmpeg_path)
    for hint in _WINDOWS_LOOPBACK_DEVICE_HINTS:
        for name in device_names:
            if hint in name.lower():
                return AudioSource(input_args=["-f", "dshow", "-i", f"audio={name}"], description=name)
    return None


def _list_windows_microphones(ffmpeg_path: str) -> list[str]:
    device_names = _list_windows_dshow_audio_devices(ffmpeg_path)
    return [name for name in device_names if not any(hint in name.lower() for hint in _WINDOWS_LOOPBACK_DEVICE_HINTS)]


def _pactl_source_names() -> list[str] | None:
    """Shared by both loopback discovery and microphone enumeration below.
    Returns None (not []) specifically on failure, so callers can tell
    "pactl isn't available" apart from "pactl ran but found nothing".
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=_PACTL_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list PulseAudio/PipeWire sources via pactl: %s", exc)
        return None
    return [columns[1] for line in result.stdout.splitlines() if len(columns := line.split()) >= 2]


def _find_linux_audio_source() -> AudioSource | None:
    names = _pactl_source_names() or []
    for name in names:
        if name.endswith(".monitor"):
            return AudioSource(input_args=["-f", "pulse", "-i", name], description=name)
    return None


def _list_linux_microphones() -> list[str]:
    names = _pactl_source_names() or []
    return [name for name in names if not name.endswith(".monitor")]


def list_microphones(ffmpeg_path: str, os_: OS) -> list[str]:
    """Real (non-loopback) audio input device names, for the Settings
    microphone picker. Best-effort like find_audio_source -- an empty list
    just means the picker has nothing to offer, not an error.
    """
    if os_ == OS.WINDOWS:
        return _list_windows_microphones(ffmpeg_path)
    if os_ == OS.LINUX:
        return _list_linux_microphones()
    return []


def find_microphone_source(os_: OS, device_name: str) -> AudioSource | None:
    """Builds the ffmpeg input args for a specific user-selected microphone
    device name (as opposed to find_audio_source's auto-detected loopback
    device) -- None only for a platform with no microphone support wired up.
    """
    if os_ == OS.WINDOWS:
        return AudioSource(input_args=["-f", "dshow", "-i", f"audio={device_name}"], description=device_name)
    if os_ == OS.LINUX:
        return AudioSource(input_args=["-f", "pulse", "-i", device_name], description=device_name)
    return None
