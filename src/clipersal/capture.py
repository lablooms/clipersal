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

from clipersal import ffmpeg_utils, platform_detect, wayland_gstreamer
from clipersal.config import Config
from clipersal.ffmpeg_utils import AudioSource, CaptureSource
from clipersal.platform_detect import OS, LinuxSessionType
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

# config.resolution_scale value -> the height the video is downscaled to
# ("native" is deliberately absent: it maps to no scale stage at all, so a
# default/old config produces the byte-identical pre-resolution-scale
# command). A hand-edited config with a bogus value reads as native here too
# -- degrade quietly, never kill capture startup over it.
_SCALE_HEIGHTS = {"1080p": 1080, "720p": 720}


def _format_volume(percent: int) -> str:
    """The ffmpeg `volume` filter value for a percentage slider position.

    `volume` takes a decimal multiplier (1.5 = 150%). Formatting via :g keeps
    the percent->decimal division clean -- 150 -> "1.5", 33 -> "0.33" -- with
    none of str(float)'s noise (0.30000000000000004) and no trailing zeros.
    """
    return f"{percent / 100:g}"


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
    # On Wayland the stored window title plays no role -- the desktop's own
    # share-dialog picks the window (see portal_screencast.py) -- so window
    # mode routes to the portal window source even with no title stored,
    # where X11/Windows treat an empty title as "no window chosen" and fall
    # back to the whole desktop.
    wants_window = config.capture_mode == "window" and (
        bool(config.window_title) or session_type == LinuxSessionType.WAYLAND
    )
    if wants_window:
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


def _default_portal_session_factory(source_type: str):
    # Lazy import on purpose: portal_screencast pulls in jeepney, and Windows
    # must never touch jeepney at runtime -- this only ever runs on the
    # Wayland capture path.
    from clipersal.portal_screencast import open_screencast_session

    return open_screencast_session(source_type)


class SegmentedCapture:
    def __init__(self, config: Config, setup: ResolvedSetup, portal_session_factory=None):
        self.config = config
        self.setup = setup
        self._process: subprocess.Popen | None = None
        self._ffmpeg_log_file = None
        self._cleanup_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_timestamps: list[float] = []
        self._gave_up = False
        # Wayland portal path state (all None on Windows/X11): the live
        # ScreenCast session, the GStreamer frame pump feeding ffmpeg's
        # stdin, and the cached ensure_gstreamer() result (resolve_setup
        # already probed; re-probing on every crash-restart is waste).
        self._portal_session_factory = portal_session_factory or _default_portal_session_factory
        self._portal_session = None
        self._frame_pump = None
        self._gst_launch: str | None = None
        # Monotonic timestamp of the current ffmpeg process's (re)start --
        # the basis for uptime_seconds(). Monotonic, not wall-clock, so a
        # system-clock adjustment can't produce a negative or jumping uptime.
        self._started_monotonic: float | None = None

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("Capture is already running")

        self._gave_up = False
        self._restart_timestamps = []
        # A previous session can end with its cleanup thread still alive --
        # the give-up path in _check_process_health leaves it sweeping a dead
        # process -- so a fresh start (e.g. IPC RESUME after CRASHED) must
        # wind it down first. Two loops on the same session could both pass
        # the poll() check before either reassigns _process, orphaning an
        # untracked ffmpeg.
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            self._stop_event.set()
            self._cleanup_thread.join(timeout=_PROCESS_STOP_TIMEOUT)
        # Likewise the old log handle can still be open if stop() never ran,
        # and on Windows unlinking a file this process still holds open
        # raises PermissionError [WinError 32] -- which is what used to make
        # RESUME-after-CRASHED always fail. Mirror what stop() does.
        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None
        # A fresh start clears any crash log from a previous run; restarts
        # (below) append instead, so the log that explains *why* ffmpeg died
        # is still there right after the auto-restart replaces the process.
        (self.config.buffer_dir / "ffmpeg.log").unlink(missing_ok=True)
        self._start_process()

        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _uses_wayland_portal(self) -> bool:
        return self.setup.video_source.kind == ffmpeg_utils.WAYLAND_PORTAL_KIND

    def _acquire_portal_session(self):
        """Open the portal ScreenCast session for this capture start.

        Only ever called from _start_process -- never at probe time: the
        first Start shows the desktop's share-dialog, and resolve_setup must
        stay dialog-free. Crash-restarts re-acquire through here too; the
        persisted restore token makes that dialog-free after first approval.
        """
        session = self._portal_session_factory(self.setup.video_source.portal_source_type)
        session.on_closed = self._on_portal_session_closed
        return session

    def _on_portal_session_closed(self) -> None:
        # The user revoked screen sharing from the desktop's own indicator.
        # That is a deliberate user stop, not a crash: mark gave_up so the
        # health check performs NO re-acquire (the user just said no --
        # asking again would be exactly the wrong response) and STATUS
        # reports capture as stopped. The pipeline is left to wind down on
        # its own (the dead PipeWire remote kills gst-launch; ffmpeg stalls
        # on stdin) and is torn down on the next stop()/start().
        log.info("Screen sharing was revoked from the desktop; capture stays stopped until started again")
        self._gave_up = True

    def _teardown_wayland(self) -> None:
        """Stop the frame pump, close the portal session, and close a
        previous ffmpeg's stdin pipe. Idempotent (a no-op on Windows/X11 and
        after a first teardown), so both _stop_process and the crash-restart
        path in _start_process can call it. Order matters: the pump goes
        first so nothing is still writing into the pipe when ffmpeg's stdin
        closes; the stdin EOF then lets ffmpeg finish before terminate().
        """
        pump, self._frame_pump = self._frame_pump, None
        session, self._portal_session = self._portal_session, None
        if pump is not None:
            pump.stop()
            if session is not None:
                # The pump owns the PipeWire fd once constructed (its stop()
                # just closed it) -- close() must not os.close() it a second
                # time, or it could close an unrelated fd that reused the
                # number (see ScreenCastSession.close's docstring).
                session.pipewire_fd = None
        if session is not None:
            session.close()
        process = self._process
        stdin = getattr(process, "stdin", None) if process is not None else None
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass

    def _start_process(self) -> None:
        wayland = self._uses_wayland_portal()
        portal_session = None
        video_input_args = None
        if wayland:
            # Defensive teardown of any previous pipeline half (dead pump /
            # open session / stale stdin from a crashed run) before
            # re-acquiring -- the health check's restart path lands here with
            # the old resources still attached.
            self._teardown_wayland()
            if self._gst_launch is None:
                self._gst_launch = wayland_gstreamer.ensure_gstreamer()
            portal_session = self._acquire_portal_session()
            stream = portal_session.stream
            if not stream.width or not stream.height:
                # The portal may legally omit the stream size (0x0 = "let
                # PipeWire negotiate"), but ffmpeg's rawvideo input needs an
                # explicit -video_size -- there is no negotiation on a pipe.
                portal_session.close()
                from clipersal.portal_screencast import PortalBackendError

                raise PortalBackendError(
                    "The portal approved screen sharing but returned no stream size -- "
                    "cannot build the raw-video input for ffmpeg."
                )
            video_input_args = ffmpeg_utils.build_wayland_input_args(stream.width, stream.height, self.config.framerate)

        cmd = self._build_command(video_input_args=video_input_args)
        log.info("Starting capture process: %s", " ".join(cmd))

        # ffmpeg's own stderr is sent to a log file rather than piped: piping
        # without a dedicated reader thread risks a full-pipe deadlock.
        # stdin is DEVNULL for the direct capture backends (nothing here
        # talks to ffmpeg's interactive "press q to stop" handling -- control
        # goes through the IPC socket), and PIPE on the Wayland path, where
        # the GStreamer frame pump writes raw BGRA frames into it.
        log_path = self.config.buffer_dir / "ffmpeg.log"
        try:
            self._ffmpeg_log_file = open(log_path, "a", encoding="utf-8")
        except Exception:
            # Don't leak the just-acquired portal session if the log can't
            # be opened -- same don't-leak discipline as the Popen path below.
            if portal_session is not None:
                portal_session.close()
            raise

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if wayland else subprocess.DEVNULL,
                stdout=self._ffmpeg_log_file,
                stderr=subprocess.STDOUT,
                **NO_WINDOW_KWARGS,
            )
            # Stamped at process birth, in _start_process rather than
            # start(), so a crash auto-restart (which re-enters here from
            # the health check) re-bases uptime on the NEW process -- the
            # value is "current ffmpeg process age", not "session age".
            self._started_monotonic = time.monotonic()
            if wayland:
                pump = wayland_gstreamer.GStreamerFramePump(
                    self._gst_launch, portal_session.pipewire_fd, portal_session.stream.node_id, self._process.stdin
                )
                pump.start()
                self._frame_pump = pump
                self._portal_session = portal_session
        except Exception:
            # Popen can fail after the log file was opened (ffmpeg binary
            # removed or blocked mid-session), and pump.start() can fail with
            # ffmpeg already spawned -- don't leak the handle, a frameless
            # ffmpeg, or the portal session. The caller (_cleanup_loop) logs
            # the failure and keeps sweeping.
            if portal_session is not None:
                portal_session.close()
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    self._terminate_process(process)
                except Exception:  # noqa: BLE001 -- already failing; don't mask the original error
                    log.warning("Could not terminate ffmpeg after a failed capture start", exc_info=True)
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None
            raise

    def stop(self) -> None:
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=_PROCESS_STOP_TIMEOUT)
            self._cleanup_thread = None

        if self._process is not None:
            self._stop_process()
            self._process = None
        self._started_monotonic = None

        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def uptime_seconds(self) -> float | None:
        """Age of the current ffmpeg process in seconds, or None when no
        process is running (never started, stopped, or crashed-and-gave-up).
        Feeds the IPC STATS payload's uptime field.

        Read from the IPC handler thread while the cleanup thread may be
        mid-restart; a plain attribute is safe enough here because CPython
        reads and writes a single object reference atomically, so the worst
        case is one read of the previous process generation's timestamp --
        harmless for a display-value uptime. is_running() is checked first
        so a dead process with a stale timestamp still reports None.
        """
        if not self.is_running():
            return None
        started = self._started_monotonic
        if started is None:
            return None
        return time.monotonic() - started

    def gave_up_restarting(self) -> bool:
        """True once auto-restart has exhausted its budget (see
        _MAX_RESTARTS_PER_WINDOW) and capture is sitting stopped after an
        unexpected ffmpeg exit, rather than mid-restart or intentionally
        stopped via stop().
        """
        return self._gave_up

    def _stop_process(self) -> None:
        # Wayland teardown first (pump -> session -> stdin EOF; see
        # _teardown_wayland) so ffmpeg stops receiving frames and sees its
        # stdin close before the terminate below lands. A no-op elsewhere.
        self._teardown_wayland()
        process = self._process
        if process is None or process.poll() is not None:
            return
        self._terminate_process(process)

    def _terminate_process(self, process) -> None:
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
        try:
            process.terminate()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg did not exit within %ss, killing it", _PROCESS_STOP_TIMEOUT)
            process.kill()
            process.wait(timeout=_PROCESS_STOP_TIMEOUT)

    def _build_command(self, video_input_args: list[str] | None = None) -> list[str]:
        # video_input_args overrides the source's stored input args -- the
        # Wayland portal path only learns them at capture-start (the stream
        # size comes from the portal handshake), so its marker CaptureSource
        # carries an empty list and _start_process passes the real args in.
        cfg = self.config
        setup = self.setup
        # Loopback (system audio) always comes before the mic when both are
        # present, so their input indices are predictable below (1 and 2).
        audio_sources = [src for src in (setup.audio_source, setup.mic_source) if src is not None]

        cmd = [setup.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]
        cmd += ffmpeg_utils.encoder_global_args(setup.encoder)
        cmd += video_input_args if video_input_args is not None else setup.video_source.input_args
        for source in audio_sources:
            cmd += source.input_args

        filter_parts = []
        if setup.video_source.video_filter:
            filter_parts.append(setup.video_source.video_filter)
        # The downscale stage sits BETWEEN the source's own fragment (e.g.
        # ddagrab's hwdownload/format chain) and the encoder's fragment:
        # VAAPI's "format=nv12,hwupload" must run after the scale (frames
        # are scaled in software, then uploaded). One -vf chain serves the
        # direct capture backends and the Wayland rawvideo path alike --
        # the portal source's marker CaptureSource carries video_filter=None,
        # so the scale is simply the first stage there.
        scale_height = _SCALE_HEIGHTS.get(cfg.resolution_scale)
        if scale_height is not None:
            filter_parts.append(f"scale=-2:{scale_height}")
        encoder_fragment = ffmpeg_utils.encoder_filter_fragment(setup.encoder)
        if encoder_fragment:
            filter_parts.append(encoder_fragment)
        if filter_parts:
            cmd += ["-vf", ",".join(filter_parts)]

        cmd += ["-map", "0:v:0"]
        if len(audio_sources) == 2:
            # System audio (input 1) and mic (input 2) mixed into a single
            # output track -- duration=first ties the mix's length to the
            # loopback stream, which (like the video) runs for the life of
            # capture either way.
            # A per-source volume adjustment rides in front of the mix when
            # that source isn't at 100%; at 100 the stage is omitted entirely,
            # so the default command stays byte-identical to the
            # pre-volume-control one (old config files change nothing -- the
            # Phase 8 rule).
            filter_chain = []
            if cfg.desktop_volume != 100:
                filter_chain.append(f"[1:a]volume={_format_volume(cfg.desktop_volume)}[a1]")
                mix_inputs = "[a1]"
            else:
                mix_inputs = "[1:a]"
            if cfg.mic_volume != 100:
                filter_chain.append(f"[2:a]volume={_format_volume(cfg.mic_volume)}[a2]")
                mix_inputs += "[a2]"
            else:
                mix_inputs += "[2:a]"
            filter_chain.append(f"{mix_inputs}amix=inputs=2:duration=first:dropout_transition=0[aout]")
            cmd += ["-filter_complex", ";".join(filter_chain)]
            cmd += ["-map", "[aout]"]
        elif len(audio_sources) == 1:
            # The lone source is the loopback when one was found, else the mic.
            only_volume = cfg.desktop_volume if setup.audio_source is not None else cfg.mic_volume
            if only_volume != 100:
                cmd += ["-filter_complex", f"[1:a]volume={_format_volume(only_volume)}[aout]"]
                cmd += ["-map", "[aout]"]
            else:
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
            try:
                delete_stale_segments(self.config.buffer_dir, self.config.buffer_seconds)
                self._check_process_health()
            except Exception:
                # A dead loop means stale segments stop aging out (the disk
                # slowly fills) and crash-restarts silently stop, while
                # STATUS still reports the stale _process state -- so log and
                # keep going, same best-effort style as the app's probes.
                log.exception("Cleanup iteration failed; retrying on the next interval")

    def _frame_pump_failed(self) -> bool:
        """True when the Wayland frame pump died (or is dying) -- gst-launch
        exiting on its own, or the pump thread hitting an unexpected EOF.
        Always False on Windows/X11 (no pump exists there).
        """
        pump = self._frame_pump
        if pump is None:
            return False
        return bool(pump.exited_unexpectedly) or not pump.is_running

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
        if process is None:
            return  # never started -- nothing to do
        pump_failed = self._frame_pump_failed()
        if process.poll() is None and not pump_failed:
            return  # still running -- nothing to do
        if self._gave_up:
            return  # already exhausted the restart budget; don't keep trying

        if pump_failed and process.poll() is None:
            # gst-launch died out from under a live ffmpeg (pipewiresrc
            # error, portal stream gone). ffmpeg itself can't notice -- it
            # just sits blocked on a stdin nobody writes to anymore -- so a
            # dead pump with a live ffmpeg is a capture death too, with the
            # same restart semantics; the live half of the pipeline just has
            # to come down first.
            log.warning("GStreamer frame pump stopped unexpectedly; restarting capture")
            self._stop_process()
        else:
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
            # Close the log handle on this path too (only the restart branch
            # below used to): start() unlinks ffmpeg.log on a fresh start,
            # and on Windows unlinking a file this process still holds open
            # raises PermissionError -- so leaving it open here broke the
            # documented RESUME-after-CRASHED recovery.
            if self._ffmpeg_log_file is not None:
                self._ffmpeg_log_file.close()
                self._ffmpeg_log_file = None
            return
        self._restart_timestamps.append(now)

        if self._stop_event.is_set():
            # stop() ran while this health check was in flight (its join
            # timed out); restarting now would orphan an ffmpeg nobody owns.
            return

        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None
        self._start_process()
