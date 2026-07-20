import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import clipersal.concat as concat_module
from clipersal.concat import (
    ConcatFailedError,
    EmptyBufferError,
    TrimRangeError,
    _finalized_segments,
    _unique_output_path,
    enforce_clip_retention,
    render_filename,
    save_clip,
    trim_clip,
)
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

FFMPEG_PATH = shutil.which("ffmpeg")


def _touch(path: Path, mtime: float | None = None) -> None:
    path.write_bytes(b"fake segment data")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_finalized_segments_excludes_newest(tmp_path: Path) -> None:
    _touch(tmp_path / "seg-20260101-000100.ts")
    _touch(tmp_path / "seg-20260101-000200.ts")
    _touch(tmp_path / "seg-20260101-000300.ts")

    finalized = _finalized_segments(tmp_path)

    assert [p.name for p in finalized] == [
        "seg-20260101-000100.ts",
        "seg-20260101-000200.ts",
    ]


def test_finalized_segments_empty_when_only_one_segment(tmp_path: Path) -> None:
    _touch(tmp_path / "seg-20260101-000100.ts")

    assert _finalized_segments(tmp_path) == []


def test_save_clip_raises_when_buffer_empty(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()

    with pytest.raises(EmptyBufferError):
        save_clip("ffmpeg", buffer_dir, clips_dir)


def test_save_clip_raises_when_only_one_segment(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    _touch(buffer_dir / "seg-20260101-000100.ts")

    with pytest.raises(EmptyBufferError):
        save_clip("ffmpeg", buffer_dir, clips_dir)


@pytest.mark.skipif(FFMPEG_PATH is None, reason="ffmpeg not available on PATH")
def test_save_clip_produces_playable_output_with_real_ffmpeg(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()

    for i in range(3):
        segment_path = buffer_dir / f"seg-2026010100000{i}.ts"
        subprocess.run(
            [
                FFMPEG_PATH,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:size=64x64:rate=5:duration=1",
                "-c:v",
                "libx264",
                "-f",
                "mpegts",
                str(segment_path),
            ],
            check=True,
            timeout=30,
        )

    output_path = save_clip(FFMPEG_PATH, buffer_dir, clips_dir)

    assert output_path.exists()
    assert output_path.suffix == ".mp4"
    assert output_path.stat().st_size > 0

    probe = subprocess.run(
        [FFMPEG_PATH, "-v", "error", "-i", str(output_path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


def test_finalized_segments_with_trim_filters_by_mtime(tmp_path: Path) -> None:
    now = time.time()
    _touch(tmp_path / "seg-20260101-000100.ts", mtime=now - 120)  # too old, should be trimmed out
    _touch(tmp_path / "seg-20260101-000200.ts", mtime=now - 20)  # within trim window
    _touch(tmp_path / "seg-20260101-000300.ts", mtime=now)  # newest -- always excluded regardless of trim

    finalized = _finalized_segments(tmp_path, trim_seconds=30)

    assert [p.name for p in finalized] == ["seg-20260101-000200.ts"]


def test_finalized_segments_with_trim_returns_empty_when_nothing_recent_enough(tmp_path: Path) -> None:
    now = time.time()
    _touch(tmp_path / "seg-20260101-000100.ts", mtime=now - 120)
    _touch(tmp_path / "seg-20260101-000200.ts", mtime=now)  # newest -- excluded

    finalized = _finalized_segments(tmp_path, trim_seconds=30)

    assert finalized == []


# ---- save vs cleanup-thread race (segments vanishing mid-save) ---------------


def test_finalized_segments_with_trim_skips_segment_that_vanished_mid_listing(tmp_path: Path, monkeypatch) -> None:
    # The cleanup thread can delete a segment between list_current_segments()
    # and the trim filter's stat() -- simulate by returning a listing that
    # includes an already-deleted file.
    now = time.time()
    keep = tmp_path / "seg-20260101-000100.ts"
    gone = tmp_path / "seg-20260101-000200.ts"
    newest = tmp_path / "seg-20260101-000300.ts"
    for path in (keep, gone, newest):
        _touch(path, mtime=now)
    gone.unlink()
    monkeypatch.setattr(concat_module, "list_current_segments", lambda d: [keep, gone, newest])

    finalized = _finalized_segments(tmp_path, trim_seconds=30)

    assert finalized == [keep]


def _fake_run_recording_list_file(recorded: dict):
    """Stand-in for subprocess.run that captures the concat list file's
    contents (the real file is unlinked by save_clip's finally block, so it
    must be read from inside the call)."""

    def fake_run(cmd, **kwargs):
        list_path = Path(cmd[cmd.index("-i") + 1])
        recorded["list_text"] = list_path.read_text(encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    return fake_run


def test_save_clip_skips_segment_that_vanished_before_ffmpeg_runs(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    keep = buffer_dir / "seg-20260101-000100.ts"
    gone = buffer_dir / "seg-20260101-000200.ts"
    newest = buffer_dir / "seg-20260101-000300.ts"
    for path in (keep, gone, newest):
        _touch(path)
    gone.unlink()  # deleted by the cleanup thread after the listing, before the remux
    monkeypatch.setattr(concat_module, "list_current_segments", lambda d: [keep, gone, newest])
    recorded: dict = {}
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording_list_file(recorded))

    output_path = save_clip("ffmpeg", buffer_dir, clips_dir)

    # The vanished segment is simply left out of the clip instead of failing
    # the whole save with ffmpeg's "No such file or directory".
    assert str(keep.resolve()) in recorded["list_text"]
    assert "seg-20260101-000200" not in recorded["list_text"]
    assert output_path.parent == clips_dir


def test_save_clip_raises_empty_buffer_when_every_segment_vanished(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    gone1 = buffer_dir / "seg-20260101-000100.ts"
    gone2 = buffer_dir / "seg-20260101-000200.ts"
    newest = buffer_dir / "seg-20260101-000300.ts"
    for path in (gone1, gone2, newest):
        _touch(path)
    gone1.unlink()
    gone2.unlink()
    monkeypatch.setattr(concat_module, "list_current_segments", lambda d: [gone1, gone2, newest])
    run_calls = []
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: run_calls.append(True))

    with pytest.raises(EmptyBufferError):
        save_clip("ffmpeg", buffer_dir, clips_dir)

    assert run_calls == []  # ffmpeg is never spawned for an empty buffer


def test_render_filename_default_template_matches_original_hardcoded_format() -> None:
    now = datetime(2026, 7, 16, 13, 5, 9)

    assert render_filename("clip-{date}-{time}", now=now) == "clip-20260716-130509"


def test_render_filename_supports_datetime_placeholder() -> None:
    now = datetime(2026, 7, 16, 13, 5, 9)

    assert render_filename("recording_{datetime}", now=now) == "recording_20260716-130509"


def test_render_filename_sanitizes_invalid_filename_characters() -> None:
    now = datetime(2026, 7, 16, 13, 5, 9)

    result = render_filename("clip:{date}/{time}?*", now=now)

    assert result == "clip_20260716_130509__"


def test_render_filename_falls_back_to_clip_when_template_renders_empty() -> None:
    assert render_filename("") == "clip"
    assert render_filename("...") == "clip"


def test_unique_output_path_appends_counter_on_collision(tmp_path: Path) -> None:
    (tmp_path / "clip-test.mp4").write_bytes(b"existing")

    path = _unique_output_path(tmp_path, "clip-test")

    assert path == tmp_path / "clip-test-1.mp4"


def test_unique_output_path_increments_past_multiple_collisions(tmp_path: Path) -> None:
    (tmp_path / "clip-test.mp4").write_bytes(b"existing")
    (tmp_path / "clip-test-1.mp4").write_bytes(b"existing")

    path = _unique_output_path(tmp_path, "clip-test")

    assert path == tmp_path / "clip-test-2.mp4"


def test_unique_output_path_no_collision_returns_base_name(tmp_path: Path) -> None:
    path = _unique_output_path(tmp_path, "clip-test")

    assert path == tmp_path / "clip-test.mp4"


def test_enforce_clip_retention_deletes_only_clips_older_than_cutoff(tmp_path: Path) -> None:
    now = time.time()
    old_clip = tmp_path / "clip-old.mp4"
    new_clip = tmp_path / "clip-new.mp4"
    _touch(old_clip, mtime=now - 10 * 86400)
    _touch(new_clip, mtime=now - 1 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=5, now=now)

    assert deleted == [old_clip]
    assert not old_clip.exists()
    assert new_clip.exists()


def test_enforce_clip_retention_disabled_when_days_is_zero(tmp_path: Path) -> None:
    old_clip = tmp_path / "clip-old.mp4"
    _touch(old_clip, mtime=time.time() - 365 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=0)

    assert deleted == []
    assert old_clip.exists()


def test_enforce_clip_retention_disabled_when_days_is_negative(tmp_path: Path) -> None:
    old_clip = tmp_path / "clip-old.mp4"
    _touch(old_clip, mtime=time.time() - 365 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=-1)

    assert deleted == []
    assert old_clip.exists()


def test_enforce_clip_retention_ignores_non_mp4_files(tmp_path: Path) -> None:
    now = time.time()
    old_thumbnail = tmp_path / "clip-old.jpg"
    _touch(old_thumbnail, mtime=now - 10 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=5, now=now)

    assert deleted == []
    assert old_thumbnail.exists()


# ---- trim_clip ------------------------------------------------------------


def _fake_run_recording(recorded: dict, returncode: int = 0, stderr: str = ""):
    """Stand-in for subprocess.run that just captures argv/kwargs."""

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    return fake_run


def test_trim_clip_builds_exact_ffmpeg_command(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    recorded: dict = {}
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording(recorded))

    output_path = trim_clip("ffmpeg", clip, 12.5, 40.0, clips_dir, duration_seconds=60.0)

    assert recorded["cmd"] == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-ss",
        "12.5",
        "-to",
        "40",
        "-i",
        str(clip),
        "-c",
        "copy",
        str(clips_dir / "clip-trimmed.mp4"),
    ]
    assert output_path == clips_dir / "clip-trimmed.mp4"
    assert recorded["kwargs"]["capture_output"] is True
    assert recorded["kwargs"]["text"] is True
    assert recorded["kwargs"]["timeout"] == concat_module._CONCAT_TIMEOUT
    for key, value in NO_WINDOW_KWARGS.items():
        assert recorded["kwargs"][key] == value
    assert clip.exists()  # the original clip is never deleted


def test_trim_clip_appends_counter_when_trimmed_name_exists(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    (clips_dir / "clip-trimmed.mp4").write_bytes(b"previous trim")
    recorded: dict = {}
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording(recorded))

    first = trim_clip("ffmpeg", clip, 0.0, 10.0, clips_dir, duration_seconds=60.0)
    assert first == clips_dir / "clip-trimmed-1.mp4"

    (clips_dir / "clip-trimmed-1.mp4").write_bytes(b"previous trim")
    second = trim_clip("ffmpeg", clip, 0.0, 10.0, clips_dir, duration_seconds=60.0)
    assert second == clips_dir / "clip-trimmed-2.mp4"


def test_trim_clip_rejects_start_at_or_after_end(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    run_calls = []
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: run_calls.append(True))

    with pytest.raises(TrimRangeError):
        trim_clip("ffmpeg", clip, 10.0, 10.0, tmp_path, duration_seconds=60.0)
    with pytest.raises(TrimRangeError):
        trim_clip("ffmpeg", clip, 11.0, 10.0, tmp_path, duration_seconds=60.0)

    assert run_calls == []  # validation happens before ffmpeg is ever spawned


def test_trim_clip_rejects_end_beyond_duration(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    recorded: dict = {}
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording(recorded))

    with pytest.raises(TrimRangeError):
        trim_clip("ffmpeg", clip, 10.0, 60.5, tmp_path, duration_seconds=60.0)
    assert "cmd" not in recorded

    # ...while an end exactly AT the duration is the allowed boundary.
    trim_clip("ffmpeg", clip, 0.0, 60.0, tmp_path, duration_seconds=60.0)
    assert "cmd" in recorded


def test_trim_clip_rejects_negative_start(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    run_calls = []
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: run_calls.append(True))

    with pytest.raises(TrimRangeError):
        trim_clip("ffmpeg", clip, -0.5, 10.0, tmp_path, duration_seconds=60.0)

    assert run_calls == []


def test_trim_clip_probes_duration_when_not_provided(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    monkeypatch.setattr(concat_module.thumbnails, "find_ffprobe", lambda ffmpeg_path: "/fake/ffprobe")
    monkeypatch.setattr(concat_module.thumbnails, "get_duration_seconds", lambda ffprobe, path: 30.0)
    recorded: dict = {}
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording(recorded))

    # 40s is beyond the probed 30s duration -- validation uses the probe.
    with pytest.raises(TrimRangeError):
        trim_clip("ffmpeg", clip, 10.0, 40.0, tmp_path, duration_seconds=None)

    output_path = trim_clip("ffmpeg", clip, 10.0, 20.0, tmp_path)
    assert output_path == tmp_path / "clip-trimmed.mp4"


def test_trim_clip_raises_when_duration_probe_fails(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    monkeypatch.setattr(concat_module.thumbnails, "find_ffprobe", lambda ffmpeg_path: None)
    run_calls = []
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: run_calls.append(True))

    with pytest.raises(TrimRangeError, match="duration"):
        trim_clip("ffmpeg", clip, 1.0, 2.0, tmp_path)

    assert run_calls == []


def test_trim_clip_raises_concat_failed_with_truncated_stderr(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_recording({}, returncode=1, stderr="x" * 2000))

    with pytest.raises(ConcatFailedError) as excinfo:
        trim_clip("ffmpeg", clip, 1.0, 2.0, tmp_path, duration_seconds=60.0)

    message = str(excinfo.value)
    assert message.startswith("ffmpeg trim failed:")
    assert "x" * 1000 in message
    assert "x" * 1001 not in message
