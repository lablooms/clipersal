import os
import time
from pathlib import Path

from clipersal.capture import (
    _MAX_RESTARTS_PER_WINDOW,
    ResolvedSetup,
    SegmentedCapture,
    delete_stale_segments,
    list_current_segments,
)
from clipersal.config import Config
from clipersal.ffmpeg_utils import AudioSource, CaptureSource


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
