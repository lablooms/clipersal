"""GStreamer pipewiresrc -> pipe -> ffmpeg bridge for Wayland screen capture.

Why this module exists at all: no released ffmpeg has a PipeWire input device
(the pipewiregrab patch was proposed upstream but never merged), so on Wayland
the xdg-desktop-portal screencast stream cannot be handed to ffmpeg directly.
The established workaround -- used by Kooha, vokoscreenNG, and others -- is to
run a tiny GStreamer pipeline that reads the portal-provided PipeWire stream
via `pipewiresrc` and writes raw BGRA frames to its own stdout via `fdsink`;
ffmpeg then consumes `-f rawvideo` from the other end of that pipe. This
module probes for the required GStreamer pieces (ensure_gstreamer) and runs
that bridge pipeline (GStreamerFramePump).

Linux-only at runtime, but must import cleanly on Windows: nothing
platform-specific happens at import time, and the D-Bus/portal work that
produces the PipeWire fd lives in a sibling module. `pass_fds` is a POSIX-only
Popen kwarg, but this code path is only ever reached on Linux/Wayland.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import BinaryIO

from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_GST_INSPECT_TIMEOUT = 10  # seconds
_TERMINATE_TIMEOUT = 5  # seconds to wait for gst-launch after terminate()
_PUMP_JOIN_TIMEOUT = 5  # seconds to wait for the pump thread in stop()
_CHUNK_SIZE = 256 * 1024  # pump copy granularity: 256 KiB


class GStreamerNotFoundError(RuntimeError):
    pass


class PipewirePluginMissingError(RuntimeError):
    pass


def ensure_gstreamer() -> str:
    """Locate a working gst-launch-1.0 with the pipewiresrc element, or raise
    a clear, actionable error. Unlike the house best-effort probes (audio,
    monitors), this one must raise: Wayland capture simply cannot proceed
    without GStreamer, so "degrade quietly" is not an option here.
    """
    path = shutil.which("gst-launch-1.0")
    if path is None:
        raise GStreamerNotFoundError(
            "GStreamer was not found on PATH. Wayland screen capture needs it to "
            "bridge PipeWire into ffmpeg -- install it:\n"
            "  Debian/Ubuntu: sudo apt install gstreamer1.0-tools gstreamer1.0-pipewire\n"
            "  Fedora:        sudo dnf install gstreamer1-plugins-base-tools gstreamer1-plugin-pipewire"
        )
    inspect = shutil.which("gst-inspect-1.0")
    if inspect is None:
        raise GStreamerNotFoundError(
            f"Found gst-launch-1.0 at {path} but gst-inspect-1.0 is missing -- "
            "the GStreamer installation looks incomplete (gstreamer1.0-tools provides both)."
        )
    try:
        result = subprocess.run(
            [inspect, "pipewiresrc"], capture_output=True, timeout=_GST_INSPECT_TIMEOUT, **NO_WINDOW_KWARGS
        )
    except OSError as exc:
        raise GStreamerNotFoundError(f"Found gst-inspect-1.0 at {inspect} but it failed to run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # The plugin's presence could not be confirmed at all; the actionable
        # advice is the same as for a definitively missing element.
        raise PipewirePluginMissingError(
            f"`gst-inspect-1.0 pipewiresrc` timed out after {_GST_INSPECT_TIMEOUT}s, so the "
            "pipewiresrc element could not be verified. Make sure the PipeWire GStreamer "
            "plugin is installed:\n"
            "  Debian/Ubuntu: sudo apt install gstreamer1.0-pipewire\n"
            "  Fedora:        sudo dnf install gstreamer1-plugin-pipewire"
        ) from exc
    if result.returncode != 0:
        raise PipewirePluginMissingError(
            "gst-launch-1.0 was found, but the 'pipewiresrc' element is missing. "
            "Install the PipeWire GStreamer plugin:\n"
            "  Debian/Ubuntu: sudo apt install gstreamer1.0-pipewire\n"
            "  Fedora:        sudo dnf install gstreamer1-plugin-pipewire"
        )
    log.info("Using gst-launch-1.0 at %s (pipewiresrc present)", path)
    return path


class GStreamerFramePump:
    """Runs the bridge pipeline

        pipewiresrc fd=<portal fd> path=<node id> ! videoconvert !
        video/x-raw,format=BGRA ! fdsink

    and copies the resulting raw BGRA byte stream from gst-launch's stdout
    into `sink` (ffmpeg's stdin pipe) on a background thread.

    Ownership: the portal session hands over ownership of `pipewire_fd` at
    construction -- stop() closes it, exactly once. `sink` is NOT owned:
    ffmpeg's stdin pipe is opened and closed by the capture layer.
    """

    def __init__(self, gst_launch: str, pipewire_fd: int, node_id: int, sink: BinaryIO) -> None:
        self._gst_launch = gst_launch
        self._pipewire_fd: int | None = pipewire_fd
        self._node_id = node_id
        self._sink = sink
        self._process: subprocess.Popen | None = None
        self._pump_thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        # Set by the pump thread when gst-launch's stdout hits EOF without
        # stop() having been requested (pipewiresrc error, stream ended).
        # The capture layer's health check treats this like an ffmpeg death.
        self.exited_unexpectedly = False

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("GStreamerFramePump.start() called twice")
        argv = [
            self._gst_launch,
            "-q",
            "pipewiresrc",
            f"fd={self._pipewire_fd}",
            f"path={self._node_id}",
            "!",  # gst-launch parses each '!' as its own argument
            "videoconvert",
            "!",
            "video/x-raw,format=BGRA",
            "!",
            "fdsink",
        ]
        log.info("Starting PipeWire bridge: %s", " ".join(argv))
        # stderr goes to DEVNULL rather than PIPE: gst-launch chatters on
        # stderr, and a pipe with no dedicated reader risks the same
        # full-pipe deadlock capture.py avoids by logging ffmpeg's stderr to
        # a file instead. pass_fds keeps the portal's PipeWire fd open across
        # the exec -- Popen closes inherited fds by default (close_fds=True),
        # and pipewiresrc's fd= property refers to the fd number in the child.
        self._process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(self._pipewire_fd,),
            **NO_WINDOW_KWARGS,
        )
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="gst-frame-pump", daemon=True
        )
        self._pump_thread.start()

    def _pump_loop(self) -> None:
        # The PipeWire screencast stream is VFR/damage-driven (frames only
        # arrive when something on screen changes); cadence is enforced
        # downstream by ffmpeg's -framerate + vsync, so this loop must stay
        # dumb and fast -- read, write, repeat, no timing logic.
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                chunk = self._process.stdout.read(_CHUNK_SIZE)
                if not chunk:
                    if not self._stop_requested.is_set():
                        log.warning("gst-launch stdout reached EOF unexpectedly (pipewiresrc died?)")
                        self.exited_unexpectedly = True
                    return
                self._sink.write(chunk)
        except (OSError, ValueError):
            # stdout closed from under us during stop(), or ffmpeg died and
            # its stdin pipe broke (BrokenPipeError is an OSError). Either way
            # stop() / the capture health check owns the fallout; this thread
            # just exits quietly.
            return

    def stop(self) -> None:
        """Idempotent: capture stop AND the crash-restart path may both call
        this. Plain terminate() (never CTRL_BREAK_EVENT -- that doesn't work
        from a windowed build), escalate to kill() on timeout, then join the
        pump thread and close the portal fd we own.
        """
        self._stop_requested.set()
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_TERMINATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=_TERMINATE_TIMEOUT)
        thread, self._pump_thread = self._pump_thread, None
        if thread is not None:
            # Bounded join: the thread can be blocked on a write to a full
            # pipe whose reader (ffmpeg) is gone. It's a daemon thread, so a
            # stuck one can't hold the process open.
            thread.join(timeout=_PUMP_JOIN_TIMEOUT)
        if self._pipewire_fd is not None:
            try:
                os.close(self._pipewire_fd)
            except OSError:
                pass
            self._pipewire_fd = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None
