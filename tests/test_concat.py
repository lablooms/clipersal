import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from clipersal.concat import (
    ConcatFailedError,
    EmptyBufferError,
    _finalized_segments,
    _unique_output_path,
    enforce_clip_retention,
    render_filename,
    save_clip,
)

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
