import shutil
import subprocess
from pathlib import Path

import pytest

from clipersal import thumbnails

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
