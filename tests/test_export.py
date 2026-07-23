import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import clipersal.export as export_module
from clipersal.export import ExportError, compress_clip, export_gif
from clipersal.subprocess_utils import NO_WINDOW_KWARGS


def _fake_run_recording(recorded: list, returncode: int = 0, stderr: str = ""):
    """Stand-in for subprocess.run that captures argv/kwargs and pretends
    ffmpeg wrote its output (always the last argv element) -- including on
    failure, because a real failed ffmpeg still leaves a partial file."""

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        Path(cmd[-1]).write_bytes(b"partial output")
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    return fake_run


def _assert_no_window_kwargs(kwargs: dict) -> None:
    for key, value in NO_WINDOW_KWARGS.items():
        assert kwargs.get(key) == value


# ---- export_gif: happy path / argv shape -------------------------------------


def test_export_gif_two_pass_argv_and_palette_cleanup(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    out = export_gif("ffmpeg", clip, tmp_path)

    assert out == tmp_path / "clip-1.gif"
    assert out.exists()
    assert len(recorded) == 2

    pass1_cmd, pass1_kwargs = recorded[0]
    palette_path = Path(pass1_cmd[-1])
    assert palette_path.suffix == ".png"
    assert "clipersal-palette-" in palette_path.name
    assert pass1_cmd == [
        "ffmpeg",
        "-y",
        "-ss",
        "0",
        "-t",
        "3",
        "-i",
        str(clip),
        "-vf",
        "fps=12,scale=480:-1:flags=lanczos,palettegen",
        str(palette_path),
    ]
    assert pass1_kwargs["timeout"] == 120
    assert pass1_kwargs["capture_output"] is True and pass1_kwargs["text"] is True
    _assert_no_window_kwargs(pass1_kwargs)

    pass2_cmd, pass2_kwargs = recorded[1]
    assert pass2_cmd == [
        "ffmpeg",
        "-y",
        "-ss",
        "0",
        "-t",
        "3",
        "-i",
        str(clip),
        "-i",
        str(palette_path),
        "-lavfi",
        "fps=12,scale=480:-1:flags=lanczos [x]; [x][1:v] paletteuse",
        str(out),
    ]
    assert pass2_kwargs["timeout"] == 120
    _assert_no_window_kwargs(pass2_kwargs)

    # The palette is a temp file: gone once the export is done.
    assert not palette_path.exists()


def test_export_gif_custom_parameters_land_in_both_passes(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    export_gif("ffmpeg", clip, tmp_path, start=1.5, duration=2.5, fps=24, width=720)

    pass1_cmd = recorded[0][0]
    assert pass1_cmd[pass1_cmd.index("-ss") + 1] == "1.5"
    assert pass1_cmd[pass1_cmd.index("-t") + 1] == "2.5"
    assert pass1_cmd[pass1_cmd.index("-vf") + 1] == "fps=24,scale=720:-1:flags=lanczos,palettegen"
    pass2_cmd = recorded[1][0]
    assert pass2_cmd[pass2_cmd.index("-lavfi") + 1] == "fps=24,scale=720:-1:flags=lanczos [x]; [x][1:v] paletteuse"


def test_export_gif_collision_gets_counter_suffix(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake clip")
    (tmp_path / "clip-1.gif").write_bytes(b"existing gif")
    (tmp_path / "clip-1-1.gif").write_bytes(b"existing gif")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    out = export_gif("ffmpeg", clip, tmp_path)

    assert out == tmp_path / "clip-1-2.gif"
    # Both pre-existing GIFs are untouched.
    assert (tmp_path / "clip-1.gif").read_bytes() == b"existing gif"
    assert (tmp_path / "clip-1-1.gif").read_bytes() == b"existing gif"


# ---- export_gif: validation ---------------------------------------------------


def test_export_gif_rejects_out_of_range_parameters(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")

    with pytest.raises(ValueError, match="start"):
        export_gif("ffmpeg", clip, tmp_path, start=-0.1)
    with pytest.raises(ValueError, match="duration"):
        export_gif("ffmpeg", clip, tmp_path, duration=0)
    with pytest.raises(ValueError, match="duration"):
        export_gif("ffmpeg", clip, tmp_path, duration=31)
    with pytest.raises(ValueError, match="fps"):
        export_gif("ffmpeg", clip, tmp_path, fps=3)
    with pytest.raises(ValueError, match="fps"):
        export_gif("ffmpeg", clip, tmp_path, fps=31)
    with pytest.raises(ValueError, match="width"):
        export_gif("ffmpeg", clip, tmp_path, width=199)
    with pytest.raises(ValueError, match="width"):
        export_gif("ffmpeg", clip, tmp_path, width=1921)


# ---- export_gif: failure paths ------------------------------------------------


def test_export_gif_pass1_failure_raises_with_stderr_tail_and_cleans_palette(
    tmp_path: Path, monkeypatch
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(
        export_module.subprocess, "run", _fake_run_recording(recorded, returncode=1, stderr="x" * 2000)
    )

    with pytest.raises(ExportError) as excinfo:
        export_gif("ffmpeg", clip, tmp_path)

    # The error carries the TAIL of ffmpeg's stderr, not the whole thing.
    assert "x" * 1000 in str(excinfo.value)
    assert "x" * 1001 not in str(excinfo.value)
    # Pass 2 never ran, the palette temp is gone, and no .gif was left behind.
    assert len(recorded) == 1
    palette_path = Path(recorded[0][0][-1])
    assert not palette_path.exists()
    assert list(tmp_path.glob("*.gif")) == []


def test_export_gif_pass2_failure_unlinks_partial_output(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        Path(cmd[-1]).write_bytes(b"partial output")
        if calls["n"] == 1:
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="boom: paletteuse exploded")

    monkeypatch.setattr(export_module.subprocess, "run", fake_run)

    with pytest.raises(ExportError, match="paletteuse exploded"):
        export_gif("ffmpeg", clip, tmp_path)

    assert list(tmp_path.glob("*.gif")) == []


def test_export_gif_timeout_unlinks_partial_output_and_raises(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        Path(cmd[-1]).write_bytes(b"partial output")
        if calls["n"] == 1:
            return SimpleNamespace(returncode=0, stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    monkeypatch.setattr(export_module.subprocess, "run", fake_run)

    with pytest.raises(ExportError, match="timed out"):
        export_gif("ffmpeg", clip, tmp_path)

    assert list(tmp_path.glob("*.gif")) == []


# ---- compress_clip: argv per encoder family -----------------------------------


def test_compress_clip_nvenc_argv(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    out = compress_clip("ffmpeg", "h264_nvenc", clip, tmp_path)

    assert out == tmp_path / "clip-1-compressed.mp4"
    cmd, kwargs = recorded[0]
    assert cmd == [
        "ffmpeg",
        "-y",
        "-i",
        str(clip),
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-rc",
        "vbr",
        "-b:v",
        "4M",
        "-c:a",
        "copy",
        str(out),
    ]
    assert kwargs["timeout"] == 300
    _assert_no_window_kwargs(kwargs)


def test_compress_clip_libx264_argv_with_scale(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    out = compress_clip("ffmpeg", "libx264", clip, tmp_path, bitrate="2.5M", scale_height=720)

    cmd = recorded[0][0]
    assert cmd == [
        "ffmpeg",
        "-y",
        "-i",
        str(clip),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "2.5M",
        "-vf",
        "scale=-2:720",
        "-c:a",
        "copy",
        str(out),
    ]


def test_compress_clip_qsv_argv(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    compress_clip("ffmpeg", "h264_qsv", clip, tmp_path)

    cmd = recorded[0][0]
    out = tmp_path / "clip-compressed.mp4"
    assert cmd == [
        "ffmpeg",
        "-y",
        "-i",
        str(clip),
        "-c:v",
        "h264_qsv",
        "-b:v",
        "4M",
        "-c:a",
        "copy",
        str(out),
    ]


def test_compress_clip_vaapi_device_before_input_and_scale_before_hwupload(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    compress_clip("ffmpeg", "h264_vaapi", clip, tmp_path, scale_height=720)

    cmd = recorded[0][0]
    # encoder_global_args' own contract is "before any -i" (it's empty for
    # every other encoder, so only VAAPI shows this).
    assert cmd[:4] == ["ffmpeg", "-y", "-vaapi_device", "/dev/dri/renderD128"]
    assert cmd[4:6] == ["-i", str(clip)]
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "h264_vaapi"
    # Same ordering capture._build_command uses: software scale first, then
    # the upload to VAAPI surfaces.
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:720,format=nv12,hwupload"


def test_compress_clip_collision_gets_counter_suffix(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip-1.mp4"
    clip.write_bytes(b"fake clip")
    (tmp_path / "clip-1-compressed.mp4").write_bytes(b"existing")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    out = compress_clip("ffmpeg", "libx264", clip, tmp_path)

    assert out == tmp_path / "clip-1-compressed-1.mp4"
    assert (tmp_path / "clip-1-compressed.mp4").read_bytes() == b"existing"


# ---- compress_clip: validation / failure --------------------------------------


def test_compress_clip_rejects_bad_bitrate_and_scale(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")

    for bad in ("4", "M", "4G", "4m", "-4M", "abc", "4.5.6M", ""):
        with pytest.raises(ValueError, match="[Bb]itrate"):
            compress_clip("ffmpeg", "libx264", clip, tmp_path, bitrate=bad)
    with pytest.raises(ValueError, match="[Ss]cale"):
        compress_clip("ffmpeg", "libx264", clip, tmp_path, scale_height=360)
    with pytest.raises(ValueError, match="[Ss]cale"):
        compress_clip("ffmpeg", "libx264", clip, tmp_path, scale_height=1440)


def test_compress_clip_accepts_documented_bitrate_shapes(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(export_module.subprocess, "run", _fake_run_recording(recorded))

    for bitrate in ("500k", "4M", "2.5M"):
        out = compress_clip("ffmpeg", "libx264", clip, tmp_path, bitrate=bitrate)
        assert out.exists()


def test_compress_clip_failure_unlinks_partial_and_raises_with_tail(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")
    recorded = []
    monkeypatch.setattr(
        export_module.subprocess, "run", _fake_run_recording(recorded, returncode=1, stderr="y" * 2000)
    )

    with pytest.raises(ExportError) as excinfo:
        compress_clip("ffmpeg", "libx264", clip, tmp_path)

    assert "y" * 1000 in str(excinfo.value)
    assert "y" * 1001 not in str(excinfo.value)
    assert list(tmp_path.glob("*-compressed*.mp4")) == []


def test_compress_clip_timeout_unlinks_partial_and_raises(tmp_path: Path, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake clip")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial output")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

    monkeypatch.setattr(export_module.subprocess, "run", fake_run)

    with pytest.raises(ExportError, match="timed out"):
        compress_clip("ffmpeg", "libx264", clip, tmp_path)

    assert list(tmp_path.glob("*-compressed*.mp4")) == []
