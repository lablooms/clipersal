import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipersal import concat, screenshots
from clipersal.screenshots import ScreenshotError, save_screenshot


def _make_segments(buffer_dir: Path, names: list[str]) -> None:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (buffer_dir / name).write_bytes(b"fake segment data")


def _fake_run_factory(calls, returncodes, create_output=False):
    """Stand-in for subprocess.run: records each argv, replays the given
    return codes, and optionally creates the output file (the partial-PNG
    leak a real failed ffmpeg leaves behind with -y).
    """

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(list(cmd))
        if create_output:
            Path(cmd[-1]).write_bytes(b"partial png")
        return SimpleNamespace(returncode=returncodes[len(calls) - 1], stderr=f"error {len(calls)}")

    return fake_run


def test_empty_buffer_raises_empty_buffer_error(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()

    with pytest.raises(concat.EmptyBufferError):
        save_screenshot("ffmpeg", buffer_dir, tmp_path / "clips")


def test_single_still_growing_segment_raises_empty_buffer_error(tmp_path: Path) -> None:
    # The newest segment is still being written by ffmpeg and excluded --
    # with only one segment there is nothing finalized to grab from.
    buffer_dir = tmp_path / "buffer"
    _make_segments(buffer_dir, ["seg-20260101-000000.ts"])

    with pytest.raises(concat.EmptyBufferError):
        save_screenshot("ffmpeg", buffer_dir, tmp_path / "clips")


def test_grabs_last_frame_of_newest_finalized_segment(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts", "seg-20260101-000004.ts"])

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [0], create_output=True))

    result = save_screenshot("ffmpeg", buffer_dir, clips_dir)

    # seg-...-000004 is still being written, so the grab targets seg-...-000002.
    cmd = calls[0]
    assert str(buffer_dir / "seg-20260101-000002.ts") in cmd
    assert cmd[cmd.index("-sseof") : cmd.index("-sseof") + 2] == ["-sseof", "-0.3"]
    assert cmd[cmd.index("-frames:v") : cmd.index("-frames:v") + 2] == ["-frames:v", "1"]
    assert result.parent == clips_dir
    assert re.fullmatch(r"screenshot-\d{8}-\d{6}\.png", result.name)


def test_tail_seek_failure_retries_from_first_frame(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [1, 0], create_output=True))

    result = save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert len(calls) == 2
    assert "-sseof" in calls[0]
    second = calls[1]
    assert "-sseof" not in second
    assert second[second.index("-ss") : second.index("-ss") + 2] == ["-ss", "0"]
    assert result.parent == clips_dir


def test_both_seeks_failing_raises_and_unlinks_partial_output(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [1, 1], create_output=True))

    with pytest.raises(ScreenshotError, match="screenshot grab failed"):
        save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert len(calls) == 2
    # The partial PNG the failed ffmpegs left behind is gone -- a broken
    # screenshot must never sit in the clips folder looking like a real one.
    assert list(clips_dir.glob("*.png")) == []


def test_timeout_unlinks_partial_output_and_propagates(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])

    def timeout_run(cmd, capture_output, text, timeout, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial png")
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(screenshots.subprocess, "run", timeout_run)

    with pytest.raises(subprocess.TimeoutExpired):
        save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert list(clips_dir.glob("*.png")) == []


def test_name_collision_gets_a_counter_suffix(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])
    # Pin the timestamped base name -- otherwise a second rolling over
    # between this render and save_screenshot's own would make the test
    # depend on wall-clock luck.
    monkeypatch.setattr(concat, "render_filename", lambda template: "screenshot-fixed")
    (clips_dir / "screenshot-fixed.png").write_bytes(b"earlier screenshot")

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [0], create_output=True))

    result = save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert result.name == "screenshot-fixed-1.png"


def test_vanished_segments_are_skipped(tmp_path: Path, monkeypatch) -> None:
    # The cleanup thread can sweep a segment between the listing and the
    # existence re-check -- a vanished newest-finalized segment just means
    # the next-older one is grabbed instead.
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts", "seg-20260101-000004.ts"])
    (buffer_dir / "seg-20260101-000002.ts").unlink()

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [0], create_output=True))

    save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert str(buffer_dir / "seg-20260101-000000.ts") in calls[0]


def test_returncode_zero_without_output_file_retries_next_seek(tmp_path: Path, monkeypatch) -> None:
    # Real bug found by the end-to-end smoke: ffmpeg exits 0 but decodes no
    # frame (e.g. -sseof past the last decodable frame on QSV segments) --
    # the ghost path must not be returned; the next seek strategy runs.
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])

    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 2:  # only the -ss 0 retry "produces" a frame
            Path(cmd[-1]).write_bytes(b"real png")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(screenshots.subprocess, "run", fake_run)

    result = save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert len(calls) == 2
    assert "-sseof" in calls[0] and "-ss" in calls[1]
    assert result.exists() and result.stat().st_size > 0


def test_returncode_zero_with_no_output_at_all_raises(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    _make_segments(buffer_dir, ["seg-20260101-000000.ts", "seg-20260101-000002.ts"])

    calls = []
    monkeypatch.setattr(screenshots.subprocess, "run", _fake_run_factory(calls, [0, 0]))  # never creates output

    with pytest.raises(ScreenshotError, match="screenshot grab failed"):
        save_screenshot("ffmpeg", buffer_dir, clips_dir)

    assert len(calls) == 2
    assert list(clips_dir.glob("*.png")) == []
