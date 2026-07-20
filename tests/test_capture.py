import io
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipersal.capture import (
    _MAX_RESTARTS_PER_WINDOW,
    ResolvedSetup,
    SegmentedCapture,
    _format_volume,
    delete_stale_segments,
    list_current_segments,
)
from clipersal.config import Config
from clipersal.ffmpeg_utils import AudioSource, CaptureSource, build_wayland_input_args
from clipersal.portal_screencast import PortalCancelledError


class _FakeProcess:
    """Stands in for subprocess.Popen -- just enough surface for
    SegmentedCapture's health-check/restart logic to exercise without a real
    ffmpeg binary.
    """

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None) -> None:
        pass


def _make_session(tmp_path: Path) -> SegmentedCapture:
    config = Config(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    setup = ResolvedSetup(
        ffmpeg_path="ffmpeg",
        encoder="libx264",
        video_source=CaptureSource(input_args=["-f", "lavfi", "-i", "color"], video_filter=None, kind="test"),
        audio_source=None,
    )
    return SegmentedCapture(config, setup)


def _touch(path: Path, mtime: float) -> None:
    path.write_bytes(b"fake segment data")
    os.utime(path, (mtime, mtime))


def test_list_current_segments_sorted_oldest_first(tmp_path: Path) -> None:
    _touch(tmp_path / "seg-20260101-000200.ts", mtime=200)
    _touch(tmp_path / "seg-20260101-000100.ts", mtime=100)
    _touch(tmp_path / "seg-20260101-000300.ts", mtime=300)

    segments = list_current_segments(tmp_path)

    assert [p.name for p in segments] == [
        "seg-20260101-000100.ts",
        "seg-20260101-000200.ts",
        "seg-20260101-000300.ts",
    ]


def test_list_current_segments_ignores_other_files(tmp_path: Path) -> None:
    _touch(tmp_path / "seg-20260101-000100.ts", mtime=100)
    (tmp_path / "ffmpeg.log").write_text("not a segment")
    (tmp_path / ".concat-abc.txt").write_text("not a segment")

    segments = list_current_segments(tmp_path)

    assert [p.name for p in segments] == ["seg-20260101-000100.ts"]


def test_delete_stale_segments_removes_only_old_ones(tmp_path: Path) -> None:
    now = 1_000_000.0
    old_path = tmp_path / "seg-old.ts"
    fresh_path = tmp_path / "seg-fresh.ts"
    _touch(old_path, mtime=now - 120)  # 120s old
    _touch(fresh_path, mtime=now - 5)  # 5s old

    deleted = delete_stale_segments(tmp_path, buffer_seconds=60, now=now)

    assert deleted == [old_path]
    assert not old_path.exists()
    assert fresh_path.exists()


def test_delete_stale_segments_race_with_concurrent_deletion(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    path = tmp_path / "seg-old.ts"
    _touch(path, mtime=now - 120)

    real_unlink = Path.unlink

    def unlink_then_vanish(self: Path, *args, **kwargs):
        # Simulate another actor (e.g. a concurrent save) removing the file
        # first; delete_stale_segments should treat this as a non-error.
        real_unlink(self, *args, **kwargs)
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "unlink", unlink_then_vanish)

    deleted = delete_stale_segments(tmp_path, buffer_seconds=60, now=now)

    assert deleted == []
    assert not path.exists()


def test_check_process_health_does_nothing_while_running(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session._process = _FakeProcess(returncode=None)

    def boom(*args, **kwargs):
        raise AssertionError("should not restart a still-running process")

    monkeypatch.setattr("clipersal.capture.subprocess.Popen", boom)

    session._check_process_health()

    assert session.is_running()
    assert session._restart_timestamps == []


def test_check_process_health_does_nothing_before_first_start(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session._check_process_health()  # _process is still None -- never started
    assert session._restart_timestamps == []
    assert not session.gave_up_restarting()


def test_check_process_health_restarts_on_unexpected_exit(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    session._process = _FakeProcess(returncode=1)  # simulate a crash
    session._ffmpeg_log_file = open(session.config.buffer_dir / "ffmpeg.log", "w", encoding="utf-8")

    restarted = _FakeProcess(returncode=None)
    monkeypatch.setattr("clipersal.capture.subprocess.Popen", lambda *a, **k: restarted)

    session._check_process_health()

    assert session.is_running()
    assert session._process is restarted
    assert len(session._restart_timestamps) == 1
    assert not session.gave_up_restarting()


def test_check_process_health_gives_up_after_max_restarts(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    session._process = _FakeProcess(returncode=1)
    session._ffmpeg_log_file = open(session.config.buffer_dir / "ffmpeg.log", "w", encoding="utf-8")

    # Every restart immediately "crashes" again too, to exercise the give-up path.
    monkeypatch.setattr("clipersal.capture.subprocess.Popen", lambda *a, **k: _FakeProcess(returncode=1))

    for _ in range(_MAX_RESTARTS_PER_WINDOW + 3):
        session._check_process_health()

    assert session.gave_up_restarting()
    assert len(session._restart_timestamps) == _MAX_RESTARTS_PER_WINDOW
    assert not session.is_running()


def test_check_process_health_give_up_closes_the_log_handle(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    session._process = _FakeProcess(returncode=1)
    log_handle = open(session.config.buffer_dir / "ffmpeg.log", "w", encoding="utf-8")
    session._ffmpeg_log_file = log_handle

    monkeypatch.setattr("clipersal.capture.subprocess.Popen", lambda *a, **k: _FakeProcess(returncode=1))

    for _ in range(_MAX_RESTARTS_PER_WINDOW + 3):
        session._check_process_health()

    assert session.gave_up_restarting()
    # The give-up branch must release ffmpeg.log -- start() unlinks it on a
    # fresh start, which is a PermissionError on Windows while held open.
    assert log_handle.closed
    assert session._ffmpeg_log_file is None


def test_start_after_give_up_closes_stale_log_handle_before_unlinking(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    # Simulate the post-give-up state: dead process, stop() never called, so
    # the old log handle is still open when RESUME triggers a fresh start().
    session._process = _FakeProcess(returncode=1)
    session._gave_up = True
    stale_handle = open(session.config.buffer_dir / "ffmpeg.log", "w", encoding="utf-8")
    session._ffmpeg_log_file = stale_handle

    monkeypatch.setattr("clipersal.capture.subprocess.Popen", lambda *a, **k: _FakeProcess(returncode=None))

    session.start()  # would raise PermissionError here on Windows if unclosed
    try:
        assert stale_handle.closed
        assert session._ffmpeg_log_file is not None
        assert not session._ffmpeg_log_file.closed
        assert session.is_running()
    finally:
        session.stop()


def test_start_with_live_cleanup_thread_winds_it_down_first(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("clipersal.capture.subprocess.Popen", lambda *a, **k: _FakeProcess(returncode=None))

    session.start()
    first_thread = session._cleanup_thread
    assert first_thread.is_alive()

    # Process dies while the cleanup thread keeps sweeping (the post-give-up
    # state); a fresh start() must stop that loop, not stack a second one.
    session._process.returncode = 1
    session._gave_up = True

    session.start()
    try:
        assert not first_thread.is_alive()
        assert session._cleanup_thread is not first_thread
        assert session._cleanup_thread.is_alive()
    finally:
        session.stop()


def test_cleanup_loop_survives_failing_restart_and_keeps_sweeping(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    session.config.cleanup_interval_seconds = 0.05
    session._process = _FakeProcess(returncode=1)  # crashed; every restart attempt will fail

    def boom(*args, **kwargs):
        raise OSError("ffmpeg binary vanished mid-session")

    monkeypatch.setattr("clipersal.capture.subprocess.Popen", boom)

    thread = threading.Thread(target=session._cleanup_loop, daemon=True)
    thread.start()
    try:
        now = time.time()
        first = session.config.buffer_dir / "seg-20260101-000100.ts"
        _touch(first, mtime=now - 120)  # older than the 60s default buffer

        deadline = time.time() + 5
        while time.time() < deadline and first.exists():
            time.sleep(0.01)
        assert not first.exists()  # first sweep ran (then the OSError hit)

        # Only a still-living loop can delete this one: it is planted after
        # the first OSError, which used to kill the thread silently.
        second = session.config.buffer_dir / "seg-20260101-000200.ts"
        _touch(second, mtime=time.time() - 120)

        deadline = time.time() + 5
        while time.time() < deadline and second.exists():
            time.sleep(0.01)

        assert not second.exists()
        assert thread.is_alive()
    finally:
        session._stop_event.set()
        thread.join(timeout=5)
    # The handle opened just before each failed Popen must not leak. Checked
    # after the join so no in-flight iteration can be holding it transiently.
    assert session._ffmpeg_log_file is None


def test_check_process_health_skips_restart_once_stop_event_set(tmp_path: Path, monkeypatch) -> None:
    session = _make_session(tmp_path)
    session.config.buffer_dir.mkdir(parents=True, exist_ok=True)
    session._process = _FakeProcess(returncode=1)
    # stop()'s join timed out while this health check was in flight.
    session._stop_event.set()

    def boom(*args, **kwargs):
        raise AssertionError("must not restart once stop() has been requested")

    monkeypatch.setattr("clipersal.capture.subprocess.Popen", boom)

    session._check_process_health()

    assert session._process.returncode == 1  # left alone, no orphan ffmpeg


def test_build_command_no_audio_sources_is_video_only(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    cmd = session._build_command()

    assert "-map" in cmd
    assert cmd[cmd.index("-map") : cmd.index("-map") + 2] == ["-map", "0:v:0"]
    assert "-filter_complex" not in cmd
    assert "-c:a" not in cmd


def test_build_command_loopback_only_maps_directly(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")

    cmd = session._build_command()

    assert "loop.monitor" in cmd
    assert "-filter_complex" not in cmd
    assert cmd.count("-map") == 2
    assert "0:v:0" in cmd
    assert "1:a:0" in cmd
    assert "-c:a" in cmd


def test_build_command_mic_only_maps_directly_as_the_sole_audio_input(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    assert "-filter_complex" not in cmd
    assert "1:a:0" in cmd
    assert "-c:a" in cmd


def test_build_command_loopback_and_mic_are_mixed_via_amix(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    assert "-filter_complex" in cmd
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    assert "[aout]" in cmd
    assert "-c:a" in cmd
    # loopback's input args should precede the mic's in the command
    assert cmd.index("loop.monitor") < cmd.index("mic0")


# ---- capture volume controls -------------------------------------------------


def test_build_command_default_volumes_leave_the_command_byte_identical(tmp_path: Path) -> None:
    # The Phase 8 rule, restated for the volume controls: both volumes at the
    # 100 default (what every old config file deserializes to) must produce
    # EXACTLY the pre-volume-control command -- not one arg more.
    session = _make_session(tmp_path)
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    assert cmd == [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i", "color",
        "-f", "pulse", "-i", "loop.monitor",
        "-f", "pulse", "-i", "mic0",
        "-map", "0:v:0",
        "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "8M",
        "-c:a", "aac", "-b:a", "160k",
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-f", "segment", "-segment_time", "2", "-reset_timestamps", "1", "-strftime", "1",
        str(tmp_path / "buffer" / "seg-%Y%m%d-%H%M%S.ts"),
    ]


def test_build_command_two_sources_with_volumes_insert_volume_stages_before_amix(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.config.desktop_volume = 150
    session.config.mic_volume = 50
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == (
        "[1:a]volume=1.5[a1];[2:a]volume=0.5[a2];[a1][a2]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )
    assert "[aout]" in cmd


def test_build_command_two_sources_desktop_at_100_omits_the_desktop_volume_stage(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.config.desktop_volume = 100
    session.config.mic_volume = 150
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == "[2:a]volume=1.5[a2];[1:a][a2]amix=inputs=2:duration=first:dropout_transition=0[aout]"


def test_build_command_single_source_with_volume_uses_volume_filter_and_aout(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.config.desktop_volume = 150
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")

    cmd = session._build_command()

    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == "[1:a]volume=1.5[aout]"
    assert "[aout]" in cmd
    assert "1:a:0" not in cmd


def test_build_command_mic_only_volume_comes_from_mic_volume_not_desktop_volume(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.config.desktop_volume = 150  # irrelevant here: no loopback source
    session.config.mic_volume = 50
    session.setup.mic_source = AudioSource(input_args=["-f", "pulse", "-i", "mic0"], description="mic")

    cmd = session._build_command()

    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == "[1:a]volume=0.5[aout]"


def test_build_command_single_source_at_100_keeps_the_direct_map(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session.config.desktop_volume = 100
    session.config.mic_volume = 150  # no mic source, so this must not leak in either
    session.setup.audio_source = AudioSource(input_args=["-f", "pulse", "-i", "loop.monitor"], description="loop")

    cmd = session._build_command()

    assert "-filter_complex" not in cmd
    assert "1:a:0" in cmd
    assert not any("volume" in arg for arg in cmd)


def test_format_volume_renders_clean_decimals_without_float_noise() -> None:
    # :g formatting -- the exact strings the ffmpeg `volume` filter receives.
    assert _format_volume(100) == "1"
    assert _format_volume(150) == "1.5"
    assert _format_volume(50) == "0.5"
    assert _format_volume(33) == "0.33"  # not 0.30000000000000004-style noise


# ---- Wayland portal capture path --------------------------------------------
#
# Everything external is faked: the portal session factory (no D-Bus), the
# GStreamer probe + frame pump (no gst-launch), and subprocess.Popen (no
# ffmpeg). The fake pump/session/process record into one shared `events`
# list so teardown ORDERING can be asserted directly.


def _make_wayland_session(tmp_path: Path, factory, portal_source_type: str = "monitor") -> SegmentedCapture:
    config = Config(buffer_dir=tmp_path / "buffer", clips_dir=tmp_path / "clips")
    config.buffer_dir.mkdir(parents=True, exist_ok=True)
    # The cleanup thread must never fire on its own mid-test -- the tests
    # drive _check_process_health() directly and assert exact call counts.
    config.cleanup_interval_seconds = 3600
    setup = ResolvedSetup(
        ffmpeg_path="ffmpeg",
        encoder="libx264",
        video_source=CaptureSource(
            input_args=[], video_filter=None, kind="wayland-portal", portal_source_type=portal_source_type
        ),
        audio_source=None,
    )
    return SegmentedCapture(config, setup, portal_session_factory=factory)


def _install_wayland_fakes(monkeypatch, width=1920, height=1080):
    rec = SimpleNamespace(events=[], sessions=[], pumps=[], popen_calls=[], source_types=[])

    class FakeSession:
        """Stands in for ScreenCastSession: a stream, a PipeWire fd, a
        settable on_closed callback, and an idempotent close()."""

        def __init__(self):
            self.stream = SimpleNamespace(node_id=7, width=width, height=height)
            self.pipewire_fd = 42
            self.on_closed = None
            self.closed = False
            rec.sessions.append(self)

        def close(self):
            self.closed = True
            rec.events.append("session_close")

    class FakePump:
        """Stands in for GStreamerFramePump."""

        def __init__(self, gst_launch, pipewire_fd, node_id, sink):
            self.init_args = (gst_launch, pipewire_fd, node_id, sink)
            self.started = False
            self.exited_unexpectedly = False
            self._running = False
            rec.pumps.append(self)

        def start(self):
            self.started = True
            self._running = True

        def stop(self):
            self._running = False
            rec.events.append("pump_stop")

        @property
        def is_running(self):
            return self._running

    class FakeWaylandProcess:
        def __init__(self):
            self.returncode = None
            self.stdin = io.BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            rec.events.append("terminate")

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            pass

    def session_factory(source_type):
        rec.source_types.append(source_type)
        return FakeSession()

    def fake_popen(cmd, **kwargs):
        proc = FakeWaylandProcess()
        rec.popen_calls.append(SimpleNamespace(cmd=cmd, kwargs=kwargs, proc=proc))
        return proc

    rec.session_factory = session_factory
    monkeypatch.setattr("clipersal.wayland_gstreamer.ensure_gstreamer", lambda: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr("clipersal.wayland_gstreamer.GStreamerFramePump", FakePump)
    monkeypatch.setattr("clipersal.capture.subprocess.Popen", fake_popen)
    return rec


def test_wayland_start_wires_session_pump_and_stdin_pipe(tmp_path: Path, monkeypatch) -> None:
    rec = _install_wayland_fakes(monkeypatch)
    session = _make_wayland_session(tmp_path, rec.session_factory)

    session.start()
    try:
        # The portal session is acquired at capture-start (never at probe
        # time), with the mode plumbed through and the revoke callback wired.
        assert rec.source_types == ["monitor"]
        portal_session = rec.sessions[0]
        assert portal_session.on_closed is not None

        # ffmpeg reads rawvideo from a stdin pipe at the portal stream's size.
        popen = rec.popen_calls[0]
        assert popen.kwargs["stdin"] == subprocess.PIPE
        expected_input = build_wayland_input_args(1920, 1080, session.config.framerate)
        i = popen.cmd.index("rawvideo")
        assert popen.cmd[i - 1 : i - 1 + len(expected_input)] == expected_input

        # The pump bridges the session's fd/node into ffmpeg's stdin.
        pump = rec.pumps[0]
        gst_launch, fd, node_id, sink = pump.init_args
        assert gst_launch == "/usr/bin/gst-launch-1.0"  # probed once, cached
        assert (fd, node_id) == (portal_session.pipewire_fd, portal_session.stream.node_id)
        assert sink is popen.proc.stdin
        assert pump.started and pump.is_running
        assert session.is_running()
    finally:
        session.stop()


def test_wayland_start_passes_window_source_type_to_factory(tmp_path: Path, monkeypatch) -> None:
    rec = _install_wayland_fakes(monkeypatch)
    session = _make_wayland_session(tmp_path, rec.session_factory, portal_source_type="window")

    session.start()
    try:
        assert rec.source_types == ["window"]
    finally:
        session.stop()


def test_wayland_stop_tears_down_pump_then_session_then_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    rec = _install_wayland_fakes(monkeypatch)
    session = _make_wayland_session(tmp_path, rec.session_factory)

    session.start()
    session.stop()

    # pump.stop() (frames stop flowing) -> session.close() (portal revoked
    # locally) -> terminate ffmpeg -- the stdin EOF lands before the SIGTERM.
    assert rec.events == ["pump_stop", "session_close", "terminate"]
    assert not session.is_running()


def test_wayland_pump_death_restarts_and_reacquires_session(tmp_path: Path, monkeypatch) -> None:
    rec = _install_wayland_fakes(monkeypatch)
    session = _make_wayland_session(tmp_path, rec.session_factory)
    session.start()
    try:
        first_pump = rec.pumps[0]
        first_session = rec.sessions[0]
        first_process = rec.popen_calls[0].proc
        # gst-launch dies out from under a still-running ffmpeg.
        first_pump.exited_unexpectedly = True
        first_pump._running = False

        session._check_process_health()

        # Same restart semantics as an ffmpeg crash: within budget, and the
        # portal session is re-acquired through the factory (the stored
        # restore token makes that dialog-free in reality).
        assert rec.source_types == ["monitor", "monitor"]
        assert len(rec.sessions) == 2
        assert first_session.closed
        assert first_process.returncode == 0  # the live half was terminated, not orphaned
        assert len(rec.popen_calls) == 2
        assert len(rec.pumps) == 2 and rec.pumps[1].started and rec.pumps[1].is_running
        assert len(session._restart_timestamps) == 1
        assert not session.gave_up_restarting()
        assert session.is_running()
    finally:
        session.stop()


def test_wayland_portal_revoke_sets_gave_up_and_blocks_reacquire(tmp_path: Path, monkeypatch) -> None:
    rec = _install_wayland_fakes(monkeypatch)
    session = _make_wayland_session(tmp_path, rec.session_factory)
    session.start()
    try:
        rec.sessions[0].on_closed()  # user revoked sharing from the desktop's indicator
        assert session.gave_up_restarting()

        # Everything downstream of the revoked session then dies -- the
        # health check must NOT re-acquire: the user just said no.
        rec.pumps[0].exited_unexpectedly = True
        rec.pumps[0]._running = False
        rec.popen_calls[0].proc.returncode = 1
        session._check_process_health()

        assert len(rec.sessions) == 1
        assert len(rec.popen_calls) == 1
        assert session.gave_up_restarting()
    finally:
        session.stop()


def test_wayland_factory_failure_propagates_without_leaking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.ensure_gstreamer", lambda: "/usr/bin/gst-launch-1.0")

    def cancelling_factory(source_type):
        raise PortalCancelledError("cancelled in the system dialog (fake)")

    session = _make_wayland_session(tmp_path, cancelling_factory)

    with pytest.raises(PortalCancelledError):
        session._start_process()

    # Nothing half-built is left behind: no process, pump, session, or log handle.
    assert session._process is None
    assert session._frame_pump is None
    assert session._portal_session is None
    assert session._ffmpeg_log_file is None


def test_wayland_factory_failure_is_contained_by_cleanup_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.ensure_gstreamer", lambda: "/usr/bin/gst-launch-1.0")

    def cancelling_factory(source_type):
        raise PortalCancelledError("cancelled in the system dialog (fake)")

    session = _make_wayland_session(tmp_path, cancelling_factory)
    session.config.cleanup_interval_seconds = 0.05
    session._process = _FakeProcess(returncode=1)  # crashed; every restart's re-acquire fails

    thread = threading.Thread(target=session._cleanup_loop, daemon=True)
    thread.start()
    try:
        # Only a still-living loop deletes this: the A1 exception containment
        # must hold across the portal re-acquire failure too.
        stale = session.config.buffer_dir / "seg-20260101-000100.ts"
        _touch(stale, mtime=time.time() - 120)  # older than the 60s default buffer

        deadline = time.time() + 5
        while time.time() < deadline and stale.exists():
            time.sleep(0.01)

        assert not stale.exists()
        assert thread.is_alive()
    finally:
        session._stop_event.set()
        thread.join(timeout=5)
    # The failing re-acquire happens before the log file is opened, so no
    # handle can leak either.
    assert session._ffmpeg_log_file is None
