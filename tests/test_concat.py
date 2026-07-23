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
    enforce_size_cap,
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


def test_render_filename_falls_back_to_clip_for_windows_reserved_device_names() -> None:
    # A template rendering to a reserved device basename would make ffmpeg
    # "succeed" writing to the device instead of a file -- the save reports
    # OK but no clip exists. Case-insensitive, with or without an extension.
    for reserved in ("NUL", "nul", "CON", "con", "PRN", "AUX", "COM1", "com5", "LPT1", "lpt9", "NUL.txt"):
        assert render_filename(reserved) == "clip", reserved


def test_render_filename_keeps_names_that_merely_start_with_a_reserved_word() -> None:
    assert render_filename("nully") == "nully"
    assert render_filename("console") == "console"
    assert render_filename("com10") == "com10"


def test_render_filename_window_placeholder_uses_the_given_title() -> None:
    now = datetime(2026, 7, 17, 1, 13, 51)

    assert render_filename("{window}-{date}-{time}", now=now, window_title="Valorant") == "Valorant-20260717-011351"


def test_render_filename_window_placeholder_falls_back_to_clip_without_a_title() -> None:
    now = datetime(2026, 7, 22, 1, 13, 51)

    # None (Wayland, an unreadable foreground window), empty, and
    # all-whitespace titles all degrade to the pre-{window} default name.
    assert render_filename("{window}-{date}-{time}", now=now) == "clip-20260722-011351"
    assert render_filename("{window}-{date}-{time}", now=now, window_title="") == "clip-20260722-011351"
    assert render_filename("{window}-{date}-{time}", now=now, window_title="   ") == "clip-20260722-011351"


def test_render_filename_window_title_is_sanitized_for_filename_use() -> None:
    # Invalid filename characters become "_" and whitespace runs collapse to
    # single spaces; case is preserved.
    assert render_filename("{window}", window_title='My "App":  Weird/Title*?') == "My _App__ Weird_Title__"


def test_render_filename_window_title_is_capped_at_40_chars() -> None:
    assert render_filename("{window}", window_title="A" * 60) == "A" * 40


def test_render_filename_window_title_cap_leaves_no_trailing_space() -> None:
    # The 40-char cut can land right after a space -- a trailing-space
    # filename is unusable on Windows, so the cut is stripped again.
    title = "x" * 39 + " " + "y" * 20
    assert render_filename("{window}", window_title=title) == "x" * 39


def test_render_filename_window_title_reserved_device_name_falls_back_to_clip() -> None:
    # Same protection as a literal reserved template: "NUL.mp4" would open
    # the null device, not a file -- but a title that merely STARTS with a
    # reserved word is fine.
    assert render_filename("{window}", window_title="NUL") == "clip"
    assert render_filename("{window}", window_title="Console App") == "Console App"


def test_save_clip_names_output_with_the_window_placeholder(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    for name in ("seg-20260101-000100.ts", "seg-20260101-000200.ts"):
        _touch(buffer_dir / name)
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))

    output_path = save_clip("ffmpeg", buffer_dir, clips_dir, filename_template="{window}-{date}", window_title="Valorant")

    assert output_path.name.startswith("Valorant-")
    assert output_path.suffix == ".mp4"


def test_save_clip_without_a_window_title_keeps_the_clip_fallback(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    for name in ("seg-20260101-000100.ts", "seg-20260101-000200.ts"):
        _touch(buffer_dir / name)
    monkeypatch.setattr(concat_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))

    output_path = save_clip("ffmpeg", buffer_dir, clips_dir, filename_template="{window}-{date}")

    assert output_path.name.startswith("clip-")
    assert output_path.suffix == ".mp4"


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


def test_enforce_clip_retention_never_deletes_protected_names(tmp_path: Path) -> None:
    # Favorites (clip_metadata) are passed in as protected so a starred clip
    # survives the sweep no matter how old it is.
    now = time.time()
    old_favorite = tmp_path / "clip-favorite.mp4"
    old_plain = tmp_path / "clip-plain.mp4"
    _touch(old_favorite, mtime=now - 10 * 86400)
    _touch(old_plain, mtime=now - 10 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=5, now=now, protected={"clip-favorite.mp4"})

    assert deleted == [old_plain]
    assert old_favorite.exists()
    assert not old_plain.exists()


def test_enforce_clip_retention_default_protected_is_none_and_behaves_as_before(tmp_path: Path) -> None:
    # protected=None (the default) must be exactly the pre-favorites
    # behavior: every .mp4 older than the cutoff goes.
    now = time.time()
    old_clip = tmp_path / "clip-old.mp4"
    _touch(old_clip, mtime=now - 10 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=5, now=now)

    assert deleted == [old_clip]
    assert not old_clip.exists()


def test_enforce_clip_retention_protected_matches_full_filename_not_stem(tmp_path: Path) -> None:
    # clip_metadata keys are full filenames, so a bare stem must NOT
    # protect the file -- matching is exact, never fuzzy.
    now = time.time()
    old_clip = tmp_path / "clip-old.mp4"
    _touch(old_clip, mtime=now - 10 * 86400)

    deleted = enforce_clip_retention(tmp_path, retention_days=5, now=now, protected={"clip-old"})

    assert deleted == [old_clip]
    assert not old_clip.exists()


def test_enforce_clip_retention_unknown_protected_names_are_harmless(tmp_path: Path) -> None:
    now = time.time()
    old_clip = tmp_path / "clip-old.mp4"
    _touch(old_clip, mtime=now - 10 * 86400)

    deleted = enforce_clip_retention(
        tmp_path, retention_days=5, now=now, protected={"clip-deleted-long-ago.mp4", "clip-old.mp4"}
    )

    assert deleted == []
    assert old_clip.exists()


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


# ---- partial-output cleanup on failure --------------------------------------


def _fake_run_creating_partial_output(returncode: int = 0, stderr: str = "", hang: bool = False):
    """Stand-in for subprocess.run mimicking ffmpeg's real failure shape:
    with -y it creates the output file up front, so a failed or timed-out
    remux leaves a partial .mp4 behind unless the caller deletes it."""

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial ffmpeg output")
        if hang:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    return fake_run


def _save_dirs_with_two_finalized_segments(tmp_path: Path) -> tuple[Path, Path]:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    buffer_dir.mkdir()
    clips_dir.mkdir()
    _touch(buffer_dir / "seg-20260101-000100.ts")
    _touch(buffer_dir / "seg-20260101-000200.ts")
    _touch(buffer_dir / "seg-20260101-000300.ts")  # newest -- excluded from the save
    return buffer_dir, clips_dir


def test_save_clip_deletes_partial_output_when_ffmpeg_fails(tmp_path: Path, monkeypatch) -> None:
    buffer_dir, clips_dir = _save_dirs_with_two_finalized_segments(tmp_path)
    monkeypatch.setattr(
        concat_module.subprocess, "run", _fake_run_creating_partial_output(returncode=1, stderr="boom")
    )

    with pytest.raises(ConcatFailedError):
        save_clip("ffmpeg", buffer_dir, clips_dir)

    assert list(clips_dir.iterdir()) == []


def test_save_clip_deletes_partial_output_when_ffmpeg_times_out(tmp_path: Path, monkeypatch) -> None:
    buffer_dir, clips_dir = _save_dirs_with_two_finalized_segments(tmp_path)
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_creating_partial_output(hang=True))

    with pytest.raises(subprocess.TimeoutExpired):
        save_clip("ffmpeg", buffer_dir, clips_dir)

    assert list(clips_dir.iterdir()) == []


def test_save_clip_keeps_output_on_success(tmp_path: Path, monkeypatch) -> None:
    buffer_dir, clips_dir = _save_dirs_with_two_finalized_segments(tmp_path)
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_creating_partial_output())

    output_path = save_clip("ffmpeg", buffer_dir, clips_dir)

    assert output_path.exists()
    assert output_path.read_bytes() == b"partial ffmpeg output"


def test_trim_clip_deletes_partial_output_when_ffmpeg_fails(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    monkeypatch.setattr(
        concat_module.subprocess, "run", _fake_run_creating_partial_output(returncode=1, stderr="boom")
    )

    with pytest.raises(ConcatFailedError):
        trim_clip("ffmpeg", clip, 1.0, 2.0, clips_dir, duration_seconds=60.0)

    assert list(clips_dir.iterdir()) == []
    assert clip.exists()  # the original clip is never touched


def test_trim_clip_deletes_partial_output_when_ffmpeg_times_out(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_creating_partial_output(hang=True))

    with pytest.raises(subprocess.TimeoutExpired):
        trim_clip("ffmpeg", clip, 1.0, 2.0, clips_dir, duration_seconds=60.0)

    assert list(clips_dir.iterdir()) == []


def test_trim_clip_keeps_output_on_success(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake mp4 data")
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    monkeypatch.setattr(concat_module.subprocess, "run", _fake_run_creating_partial_output())

    output_path = trim_clip("ffmpeg", clip, 1.0, 2.0, clips_dir, duration_seconds=60.0)

    assert output_path.exists()
    assert output_path.read_bytes() == b"partial ffmpeg output"


# ---- enforce_size_cap ----------------------------------------------------------


def _touch_sized(path: Path, size: int, mtime: float) -> None:
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


def test_enforce_size_cap_deletes_oldest_first_until_under_cap(tmp_path: Path) -> None:
    now = time.time()
    oldest = tmp_path / "clip-oldest.mp4"
    middle = tmp_path / "clip-middle.mp4"
    newest = tmp_path / "clip-newest.mp4"
    _touch_sized(oldest, 100, mtime=now - 300)
    _touch_sized(middle, 100, mtime=now - 200)
    _touch_sized(newest, 100, mtime=now - 100)

    deleted = enforce_size_cap(tmp_path, max_bytes=150)

    # 300 bytes total: two oldest must go to fit 150, the newest survives.
    assert deleted == [oldest, middle]
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()


def test_enforce_size_cap_noop_when_already_under_cap(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _touch_sized(clip, 100, mtime=time.time())

    assert enforce_size_cap(tmp_path, max_bytes=1000) == []
    assert clip.exists()


def test_enforce_size_cap_zero_and_negative_disable_it(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _touch_sized(clip, 100, mtime=time.time())

    assert enforce_size_cap(tmp_path, max_bytes=0) == []
    assert enforce_size_cap(tmp_path, max_bytes=-1) == []
    assert clip.exists()


def test_enforce_size_cap_skips_protected_and_deletes_next_oldest(tmp_path: Path) -> None:
    now = time.time()
    favorite = tmp_path / "clip-favorite.mp4"
    plain = tmp_path / "clip-plain.mp4"
    newest = tmp_path / "clip-newest.mp4"
    _touch_sized(favorite, 100, mtime=now - 300)  # oldest, but protected
    _touch_sized(plain, 100, mtime=now - 200)
    _touch_sized(newest, 100, mtime=now - 100)

    deleted = enforce_size_cap(tmp_path, max_bytes=200, protected={"clip-favorite.mp4"})

    # 300 bytes total, cap 200: the protected favorite is skipped and the
    # next-oldest goes instead, bringing the folder exactly to the cap.
    assert deleted == [plain]
    assert favorite.exists()
    assert not plain.exists()
    assert newest.exists()


def test_enforce_size_cap_stops_when_only_protected_clips_remain(tmp_path: Path) -> None:
    now = time.time()
    plain = tmp_path / "clip-plain.mp4"
    fav1 = tmp_path / "clip-fav-1.mp4"
    fav2 = tmp_path / "clip-fav-2.mp4"
    _touch_sized(plain, 100, mtime=now - 300)
    _touch_sized(fav1, 100, mtime=now - 200)
    _touch_sized(fav2, 100, mtime=now - 100)

    # Even after deleting the one unprotected clip the folder is still over
    # the cap -- the sweep must stop there, not eat the favorites.
    deleted = enforce_size_cap(tmp_path, max_bytes=50, protected={"clip-fav-1.mp4", "clip-fav-2.mp4"})

    assert deleted == [plain]
    assert not plain.exists()
    assert fav1.exists()
    assert fav2.exists()


def test_enforce_size_cap_ignores_non_mp4_files(tmp_path: Path) -> None:
    now = time.time()
    screenshot = tmp_path / "screenshot-1.png"
    _touch_sized(screenshot, 5000, mtime=now - 300)
    clip = tmp_path / "clip.mp4"
    _touch_sized(clip, 50, mtime=now - 100)

    deleted = enforce_size_cap(tmp_path, max_bytes=100)

    # The PNG counts nothing toward the cap and is never a deletion candidate.
    assert deleted == []
    assert screenshot.exists()
    assert clip.exists()


def test_enforce_size_cap_tolerates_a_clip_vanishing_mid_sweep(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    ghost = tmp_path / "clip-ghost.mp4"
    real = tmp_path / "clip-real.mp4"
    _touch_sized(ghost, 100, mtime=now - 200)
    _touch_sized(real, 100, mtime=now - 100)

    real_stat = Path.stat

    def selective_stat(self, *args, **kwargs):
        if self == ghost:
            raise FileNotFoundError("swept by someone else")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_stat)

    deleted = enforce_size_cap(tmp_path, max_bytes=50)

    # The ghost is skipped entirely; only the real clip's 100 bytes count,
    # so it must go to fit the cap.
    assert deleted == [real]


def test_enforce_size_cap_tolerates_undeletable_clips(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    stuck = tmp_path / "clip-stuck.mp4"
    deletable = tmp_path / "clip-deletable.mp4"
    _touch_sized(stuck, 100, mtime=now - 200)
    _touch_sized(deletable, 100, mtime=now - 100)

    real_unlink = Path.unlink

    def selective_unlink(self, *args, **kwargs):
        if self == stuck:
            raise OSError("file locked by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    deleted = enforce_size_cap(tmp_path, max_bytes=50)

    # The locked clip is logged and skipped; the sweep moves on to the next
    # oldest instead of dying on the first failure.
    assert deleted == [deletable]
    assert stuck.exists()
    assert not deletable.exists()
