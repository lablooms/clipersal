"""Continuous segmented capture + rolling buffer.

A single ffmpeg process runs for the life of the capture session, writing
short MPEG-TS segments into config.buffer_dir. A background thread deletes
segments older than config.buffer_seconds. See ARCHITECTURE.md ("Why segments + a
cleanup thread") for the rationale.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from clipersal import ffmpeg_utils, platform_detect
from clipersal.config import Config
from clipersal.ffmpeg_utils import AudioSource, CaptureSource
from clipersal.platform_detect import OS
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_PROCESS_STOP_TIMEOUT = 5  # seconds to wait for ffmpeg to exit before killing it

# Auto-restart budget: a driver hiccup or transient GPU error is worth retrying,
# but a persistently broken setup (bad encoder args, dead hardware) shouldn't
# spin-restart forever and peg a CPU core on a crash loop. Once this many
# unexpected exits happen within the window, restarting stops and capture
# stays down until the user (or a Settings change) restarts it explicitly.
_MAX_RESTARTS_PER_WINDOW = 5
_RESTART_WINDOW_SECONDS = 60.0

SEGMENT_GLOB = "seg-*.ts"


@dataclass
class ResolvedSetup:
    ffmpeg_path: str
    encoder: str
    video_source: CaptureSource
    audio_source: AudioSource | None
    mic_source: AudioSource | None = None


def resolve_setup(config: Config) -> ResolvedSetup:
    """Run all the startup-time detection once: ffmpeg binary, encoder,
    platform capture source, and best-effort audio source.
    """
    ffmpeg_path = ffmpeg_utils.find_ffmpeg()
    os_ = platform_detect.get_os()
    session_type = platform_detect.get_linux_session_type() if os_ == OS.LINUX else None

    encoder = ffmpeg_utils.pick_encoder(ffmpeg_path, os_, forced=config.encoder_override)
    if config.capture_mode == "window" and config.window_title:
        video_source = ffmpeg_utils.build_window_capture_source(
            ffmpeg_path, os_, session_type, config.window_title, framerate=config.framerate
        )
    else:
        # "desktop" (the default) always behaves as if monitor_index were 0,
        # regardless of whatever monitor_index is currently stored -- that
        # value only takes effect in "monitor" mode.
        monitor_index = config.monitor_index if config.capture_mode == "monitor" else 0
        video_source = ffmpeg_utils.build_video_capture_source(
            ffmpeg_path, os_, session_type, framerate=config.framerate, monitor_index=monitor_index
        )
    log.info("Video capture source: %s", video_source.kind)

    audio_source = ffmpeg_utils.find_audio_source(ffmpeg_path, os_)
    if audio_source is None:
        log.warning(
            "No system-audio loopback/monitor source found; capturing video-only. "
            "See the 'Audio capture caveat' section in ARCHITECTURE.md."
        )
    else:
        log.info("Audio capture source: %s", audio_source.description)

    mic_source = None
    if config.mic_device:
        mic_source = ffmpeg_utils.find_microphone_source(os_, config.mic_device)
        if mic_source is None:
            log.warning("Microphone input is not supported on this platform (%s); skipping it", os_)
        else:
            log.info("Microphone source: %s", mic_source.description)

    return ResolvedSetup(ffmpeg_path, encoder, video_source, audio_source, mic_source)


def list_current_segments(buffer_dir: Path) -> list[Path]:
    """Segments currently retained in the buffer, oldest first.

    Sorting by filename works because segment filenames are strftime-based
    (seg-YYYYmmdd-HHMMSS.ts), which sorts lexicographically in time order.
    """
    return sorted(buffer_dir.glob(SEGMENT_GLOB))


def delete_stale_segments(buffer_dir: Path, buffer_seconds: float, now: float | None = None) -> list[Path]:
    """Delete segment files older than buffer_seconds. Returns the deleted paths.

    Pure-ish and side-effect-isolated on purpose so it's unit-testable without
    a real ffmpeg process (see tests/test_capture.py).
    """
    cutoff = (now if now is not None else time.time()) - buffer_seconds
    deleted = []
    for path in list_current_segments(buffer_dir):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
        except FileNotFoundError:
            pass  # already gone (e.g. a concurrent cleanup pass), fine
        except OSError as exc:
            log.warning("Could not delete stale segment %s: %s", path, exc)
    return deleted


class SegmentedCapture:
    def __init__(self, config: Config, setup: ResolvedSetup):
        self.config = config
        self.setup = setup
        self._process: subprocess.Popen | None = None
        self._ffmpeg_log_file = None
        self._cleanup_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_timestamps: list[float] = []
        self._gave_up = False

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("Capture is already running")

        self._gave_up = False
        self._restart_timestamps = []
        # A fresh start clears any crash log from a previous run; restarts
        # (below) append instead, so the log that explains *why* ffmpeg died
        # is still there right after the auto-restart replaces the process.
        (self.config.buffer_dir / "ffmpeg.log").unlink(missing_ok=True)
        self._start_process()

        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _start_process(self) -> None:
        cmd = self._build_command()
        log.info("Starting capture process: %s", " ".join(cmd))

        # ffmpeg's own stderr is sent to a log file rather than piped: piping
        # without a dedicated reader thread risks a full-pipe deadlock, and
        # stdin is closed rather than inherited so ffmpeg's interactive
        # "press q to stop" handling doesn't steal keystrokes from this
        # process's own Enter-to-save input() loop.
        log_path = self.config.buffer_dir / "ffmpeg.log"
        self._ffmpeg_log_file = open(log_path, "a", encoding="utf-8")

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=self._ffmpeg_log_file,
            stderr=subprocess.STDOUT,
            **NO_WINDOW_KWARGS,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=_PROCESS_STOP_TIMEOUT)
            self._cleanup_thread = None

        if self._process is not None:
            self._stop_process()
            self._process = None

        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def gave_up_restarting(self) -> bool:
        """True once auto-restart has exhausted its budget (see
        _MAX_RESTARTS_PER_WINDOW) and capture is sitting stopped after an
        unexpected ffmpeg exit, rather than mid-restart or intentionally
        stopped via stop().
        """
        return self._gave_up

    def _stop_process(self) -> None:
        # A plain terminate() (SIGTERM / TerminateProcess), not a graceful
        # CTRL_BREAK_EVENT, deliberately: CTRL_BREAK relies on
        # GenerateConsoleCtrlEvent, which needs an attached console and
        # raises OSError ("The handle is invalid") in a --windowed packaged
        # build with no console at all (see packaging/). Segments are
        # MPEG-TS, not MP4, so there's no container trailer/index that
        # needs a graceful shutdown to finalize, and concat.py already
        # excludes the newest (possibly still-being-written) segment from
        # every save regardless -- an abruptly-terminated last segment is
        # never read either way.
        process = self._process
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg did not exit within %ss, killing it", _PROCESS_STOP_TIMEOUT)
            process.kill()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)

    def _build_command(self) -> list[str]:
        cfg = self.config
        setup = self.setup
        # Loopback (system audio) always comes before the mic when both are
        # present, so their input indices are predictable below (1 and 2).
        audio_sources = [src for src in (setup.audio_source, setup.mic_source) if src is not None]

        cmd = [setup.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]
        cmd += ffmpeg_utils.encoder_global_args(setup.encoder)
        cmd += setup.video_source.input_args
        for source in audio_sources:
            cmd += source.input_args

        filter_parts = [
            part
            for part in (setup.video_source.video_filter, ffmpeg_utils.encoder_filter_fragment(setup.encoder))
            if part
        ]
        if filter_parts:
            cmd += ["-vf", ",".join(filter_parts)]

        cmd += ["-map", "0:v:0"]
        if len(audio_sources) == 2:
            # System audio (input 1) and mic (input 2) mixed into a single
            # output track -- duration=first ties the mix's length to the
            # loopback stream, which (like the video) runs for the life of
            # capture either way.
            cmd += ["-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=0[aout]"]
            cmd += ["-map", "[aout]"]
        elif len(audio_sources) == 1:
            cmd += ["-map", "1:a:0"]

        bitrate, speed = ffmpeg_utils.resolve_quality_preset(cfg.quality_preset, setup.encoder, cfg.video_bitrate)
        cmd += ffmpeg_utils.encoder_output_args(setup.encoder, bitrate, speed)
        if audio_sources:
            cmd += ["-c:a", "aac", "-b:a", "160k"]

        # -segment_time is only a minimum: the segment muxer cuts at the next
        # keyframe at or after that point, and an encoder's default GOP length
        # (several seconds) would otherwise make actual segment length wildly
        # exceed segment_seconds. Forcing a keyframe on this exact cadence is
        # what makes segment_seconds an accurate, not approximate, cut point.
        cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{cfg.segment_seconds})"]

        segment_pattern = cfg.buffer_dir / "seg-%Y%m%d-%H%M%S.ts"
        cmd += [
            "-f",
            "segment",
            "-segment_time",
            str(cfg.segment_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(segment_pattern),
        ]
        return cmd

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self.config.cleanup_interval_seconds):
            delete_stale_segments(self.config.buffer_dir, self.config.buffer_seconds)
            self._check_process_health()

    def _check_process_health(self) -> None:
        """Detect ffmpeg dying unexpectedly (driver hiccup, etc.) and restart
        it in place, reusing the same buffer_dir -- segment filenames are
        strftime-timestamped, so a restart never collides with segments
        already written before the crash, and those pre-crash segments are
        still valid and still age out normally.

        Runs on the same cadence as the stale-segment sweep rather than a
        separate timer -- there's no need for a tighter check than that.
        """
        process = self._process
        if process is None or process.poll() is None:
            return  # never started, or still running -- nothing to do
        if self._gave_up:
            return  # already exhausted the restart budget; don't keep trying

        log.warning("ffmpeg exited unexpectedly (code %s); attempting to restart capture", process.poll())

        now = time.time()
        self._restart_timestamps = [t for t in self._restart_timestamps if now - t < _RESTART_WINDOW_SECONDS]
        if len(self._restart_timestamps) >= _MAX_RESTARTS_PER_WINDOW:
            self._gave_up = True
            log.error(
                "ffmpeg crashed %d times in the last %.0fs -- giving up on auto-restart. "
                "Capture is stopped; check %s for details.",
                _MAX_RESTARTS_PER_WINDOW,
                _RESTART_WINDOW_SECONDS,
                self.config.buffer_dir / "ffmpeg.log",
            )
            return
        self._restart_timestamps.append(now)

        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None
        self._start_process()
