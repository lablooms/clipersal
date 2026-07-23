import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipersal import thumbnails
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

FFMPEG_PATH = shutil.which("ffmpeg")


def test_thumbnail_path_for_is_stable_for_unchanged_clip(tmp_path: Path) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake")
    cache_dir = tmp_path / ".thumbnails"

    first = thumbnails.thumbnail_path_for(clip, cache_dir)
    second = thumbnails.thumbnail_path_for(clip, cache_dir)

    assert first == second
    assert first.parent == cache_dir
    assert first.suffix == ".jpg"


def test_thumbnail_path_changes_when_clip_mtime_changes(tmp_path: Path) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake")
    cache_dir = tmp_path / ".thumbnails"

    first = thumbnails.thumbnail_path_for(clip, cache_dir)

    import os
    import time

    new_time = time.time() + 10
    os.utime(clip, (new_time, new_time))
    second = thumbnails.thumbnail_path_for(clip, cache_dir)

    assert first != second


def test_thumbnail_path_for_returns_none_for_a_vanished_clip(tmp_path: Path) -> None:
    # A clip deleted between the gallery's glob and the thumbnail worker's
    # stat must not raise FileNotFoundError -- no thumbnail, no crash.
    ghost = tmp_path / "clip-gone.mp4"
    assert thumbnails.thumbnail_path_for(ghost, tmp_path / ".thumbnails") is None


def test_ensure_thumbnail_returns_none_for_a_vanished_clip(tmp_path: Path) -> None:
    # Bails out before ffmpeg is ever spawned, so no real ffmpeg is needed.
    ghost = tmp_path / "clip-gone.mp4"
    assert thumbnails.ensure_thumbnail("ffmpeg", ghost, tmp_path / ".thumbnails") is None


def test_grab_frame_at_writes_via_temp_then_replaces_into_place(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    target = tmp_path / ".thumbnails" / "clip.123.jpg"
    ran_cmds = []

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        ran_cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpeg-bytes")  # ffmpeg writes its output to the last argv element
        return _Result()

    monkeypatch.setattr(thumbnails.subprocess, "run", fake_run)

    assert thumbnails.grab_frame_at("ffmpeg", clip, 0.5, target) == target

    output_path = Path(ran_cmds[0][-1])
    # ffmpeg must never write the cache path directly: a temp name in the
    # same directory, atomically replace()d into place, so concurrent
    # writers can't interleave into a corrupt-but-cached JPEG.
    assert output_path != target
    assert output_path.parent == target.parent
    assert ".tmp-" in output_path.name
    assert not output_path.exists()  # replaced into place, not left behind
    assert target.read_bytes() == b"jpeg-bytes"


def test_grab_frame_at_failure_leaves_no_target(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    target = tmp_path / ".thumbnails" / "clip.123.jpg"

    class _Result:
        returncode = 1

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")  # a failed grab can still leave bytes on disk
        return _Result()

    monkeypatch.setattr(thumbnails.subprocess, "run", fake_run)

    # The partial temp must never be promoted to the cache path.
    assert thumbnails.grab_frame_at("ffmpeg", clip, 0.5, target) is None
    assert not target.exists()


def test_grab_frame_at_returns_none_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    target = tmp_path / ".thumbnails" / "clip.123.jpg"

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"jpeg-bytes")
        return _Result()

    def boom(self, *args, **kwargs):
        raise OSError("temp vanished mid-grab")

    monkeypatch.setattr(thumbnails.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "replace", boom)

    # A failed atomic move degrades to "no thumbnail", never an exception.
    assert thumbnails.grab_frame_at("ffmpeg", clip, 0.5, target) is None
    assert not target.exists()


def test_cleanup_orphaned_thumbnails_removes_unmatched_files(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".thumbnails"
    cache_dir.mkdir()
    kept = cache_dir / "clip-1.12345.jpg"
    kept.write_bytes(b"x")
    orphan = cache_dir / "clip-deleted.99999.jpg"
    orphan.write_bytes(b"x")

    thumbnails.cleanup_orphaned_thumbnails(cache_dir, existing_clip_stems={"clip-1"})

    assert kept.exists()
    assert not orphan.exists()


def test_cleanup_orphaned_thumbnails_handles_stem_with_dots(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".thumbnails"
    cache_dir.mkdir()
    kept = cache_dir / "2026.07.16-clip.12345.jpg"
    kept.write_bytes(b"x")

    thumbnails.cleanup_orphaned_thumbnails(cache_dir, existing_clip_stems={"2026.07.16-clip"})

    assert kept.exists()


def test_cleanup_orphaned_thumbnails_noop_when_cache_dir_missing(tmp_path: Path) -> None:
    # Should not raise even though the directory was never created.
    thumbnails.cleanup_orphaned_thumbnails(tmp_path / "never-created", existing_clip_stems=set())


def test_find_ffprobe_falls_back_to_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/found/on/path/{name}")

    assert thumbnails.find_ffprobe("/nonexistent/ffmpeg-that-does-not-exist") == "/found/on/path/ffprobe"


def test_find_ffprobe_returns_none_when_nowhere_to_be_found(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert thumbnails.find_ffprobe("/nonexistent/ffmpeg-that-does-not-exist") is None


def test_find_ffprobe_prefers_sibling_of_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    ffmpeg_path = tmp_path / "ffmpeg"
    ffmpeg_path.write_bytes(b"")
    sibling_ffprobe = tmp_path / "ffprobe"
    sibling_ffprobe.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda name: "/should/not/be/used")

    assert thumbnails.find_ffprobe(str(ffmpeg_path)) == str(sibling_ffprobe)


@pytest.mark.skipif(FFMPEG_PATH is None, reason="ffmpeg not available on PATH")
def test_ensure_thumbnail_and_duration_with_real_ffmpeg(tmp_path: Path) -> None:
    clip = tmp_path / "clip-real.mp4"
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
            "color=c=blue:size=320x240:rate=5:duration=2",
            "-c:v",
            "libx264",
            str(clip),
        ],
        check=True,
        timeout=30,
    )

    cache_dir = tmp_path / ".thumbnails"
    thumb = thumbnails.ensure_thumbnail(FFMPEG_PATH, clip, cache_dir)

    assert thumb is not None
    assert thumb.exists()
    assert thumb.stat().st_size > 0

    # Second call should hit the cache and return the same path without
    # regenerating.
    thumb_again = thumbnails.ensure_thumbnail(FFMPEG_PATH, clip, cache_dir)
    assert thumb_again == thumb

    ffprobe = thumbnails.find_ffprobe(FFMPEG_PATH)
    assert ffprobe is not None
    duration = thumbnails.get_duration_seconds(ffprobe, clip)
    assert duration is not None
    assert 1.5 < duration < 2.5


@pytest.mark.skipif(FFMPEG_PATH is None, reason="ffmpeg not available on PATH")
def test_ensure_thumbnail_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    clip = tmp_path / "not-a-real-video.mp4"
    clip.write_bytes(b"this is not a valid video file")

    thumb = thumbnails.ensure_thumbnail(FFMPEG_PATH, clip, tmp_path / ".thumbnails")

    assert thumb is None


# ---- get_video_info ------------------------------------------------------------


def _fake_ffprobe_json(recorded: dict, stdout: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return fake_run


def test_get_video_info_parses_canned_ffprobe_payload(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    recorded = {}
    monkeypatch.setattr(
        thumbnails.subprocess,
        "run",
        _fake_ffprobe_json(
            recorded,
            '{"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "12.345"}}',
        ),
    )

    info = thumbnails.get_video_info("ffprobe", clip)

    assert info == (12.345, 1920, 1080)
    cmd, kwargs = recorded["cmd"], recorded["kwargs"]
    assert cmd == [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(clip),
    ]
    assert kwargs["timeout"] == thumbnails._PROBE_TIMEOUT
    for key, value in NO_WINDOW_KWARGS.items():
        assert kwargs.get(key) == value


def test_get_video_info_individual_fields_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    recorded = {}

    # No video stream (audio-only file): duration survives, dimensions don't.
    monkeypatch.setattr(
        thumbnails.subprocess,
        "run",
        _fake_ffprobe_json(recorded, '{"streams": [], "format": {"duration": "4.5"}}'),
    )
    assert thumbnails.get_video_info("ffprobe", clip) == (4.5, None, None)

    # ffprobe reports an unknown duration as "N/A": dimensions survive.
    monkeypatch.setattr(
        thumbnails.subprocess,
        "run",
        _fake_ffprobe_json(recorded, '{"streams": [{"width": 640, "height": 360}], "format": {"duration": "N/A"}}'),
    )
    assert thumbnails.get_video_info("ffprobe", clip) == (None, 640, 360)


def test_get_video_info_returns_none_on_probe_failure(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    recorded = {}

    # Nonzero exit (corrupt file).
    monkeypatch.setattr(
        thumbnails.subprocess, "run", _fake_ffprobe_json(recorded, "not json at all", returncode=1)
    )
    assert thumbnails.get_video_info("ffprobe", clip) is None

    # Zero exit but unparseable stdout (shouldn't happen with -of json; be safe).
    monkeypatch.setattr(
        thumbnails.subprocess, "run", _fake_ffprobe_json(recorded, "not json at all", returncode=0)
    )
    assert thumbnails.get_video_info("ffprobe", clip) is None


def test_get_video_info_returns_none_on_timeout_and_oserror(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 10))

    monkeypatch.setattr(thumbnails.subprocess, "run", timeout_run)
    assert thumbnails.get_video_info("ffprobe", clip) is None

    def oserror_run(cmd, **kwargs):
        raise OSError("ffprobe not found")

    monkeypatch.setattr(thumbnails.subprocess, "run", oserror_run)
    assert thumbnails.get_video_info("ffprobe", clip) is None
