"""User-initiated exports of already-saved clips: GIF and re-compression.

This is NOT the capture path -- capture/concat never touch this module. These
are deliberate, one-off transforms the user asks for from the clip gallery on
a clip that already exists on disk.

Quality notes (why these are exports, not replacements):

- A GIF is lossy by nature: 256 colors per frame via a generated palette,
  dropped frames (fps default 12), no audio. It's a shareable preview, not an
  archive format.
- compress_clip re-encodes, so generation loss is unavoidable -- unlike
  save_clip/trim_clip, which are stream copies. The original clip is ALWAYS
  kept (the output is a new "<stem>-compressed.mp4" next to it), matching
  trim_clip's never-modify-the-original philosophy.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from clipersal import ffmpeg_utils
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_GIF_PASS_TIMEOUT = 120  # seconds per palette pass
_COMPRESS_TIMEOUT = 300  # seconds; a re-encode is real work, unlike a remux
_VALID_SCALE_HEIGHTS = (480, 720, 1080)
_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kM]$")


class ExportError(RuntimeError):
    pass


def _unique_output_path(out_dir: Path, base_name: str, extension: str) -> Path:
    """Same -1, -2, ... collision convention as concat's clip naming (kept
    local rather than imported because concat's helper is private and
    hardcodes .mp4; exports need .gif too).
    """
    candidate = out_dir / f"{base_name}{extension}"
    counter = 1
    while candidate.exists():
        candidate = out_dir / f"{base_name}-{counter}{extension}"
        counter += 1
    return candidate


def _stderr_tail(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip()[-1000:]


def export_gif(
    ffmpeg_path: str,
    clip_path: Path,
    out_dir: Path,
    start: float = 0.0,
    duration: float = 3.0,
    fps: int = 12,
    width: int = 480,
) -> Path:
    """Export the [start, start + duration] window of clip_path as an
    animated GIF written to "<stem>.gif" in out_dir (counter-suffixed on
    collision, same as saved clips).

    The two-pass palette workflow: pass 1 analyzes the frames and writes an
    optimal 256-color palette to a temp PNG, pass 2 encodes the GIF using
    that palette (paletteuse). A per-clip generated palette is the difference
    between a watchable GIF and the default web-safe palette's banding.

    Raises ValueError for out-of-range parameters, ExportError (with the
    ffmpeg stderr tail) when ffmpeg fails. A failed run never leaves a
    partial .gif or the temp palette behind.
    """
    if start < 0:
        raise ValueError(f"GIF start must be >= 0, got {start:g}s.")
    if not 0 < duration <= 30:
        raise ValueError(f"GIF duration must be in (0, 30] seconds, got {duration:g}s.")
    if not 4 <= fps <= 30:
        raise ValueError(f"GIF fps must be in [4, 30], got {fps}.")
    if not 200 <= width <= 1920:
        raise ValueError(f"GIF width must be in [200, 1920] px, got {width}.")

    output_path = _unique_output_path(out_dir, clip_path.stem, ".gif")
    scale_filter = f"fps={fps},scale={width}:-1:flags=lanczos"

    # mkstemp (not NamedTemporaryFile): ffmpeg must be able to open the path
    # for writing itself, which a still-open file would block on Windows.
    # The fd is closed immediately; the file is deleted in the finally below
    # no matter which pass fails.
    palette_fd, palette_name = tempfile.mkstemp(prefix="clipersal-palette-", suffix=".png")
    os.close(palette_fd)
    palette_path = Path(palette_name)

    try:
        pass1_cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{start:g}",
            "-t",
            f"{duration:g}",
            "-i",
            str(clip_path),
            "-vf",
            f"{scale_filter},palettegen",
            str(palette_path),
        ]
        try:
            pass1 = subprocess.run(
                pass1_cmd, capture_output=True, text=True, timeout=_GIF_PASS_TIMEOUT, **NO_WINDOW_KWARGS
            )
        except subprocess.TimeoutExpired as exc:
            raise ExportError(f"ffmpeg palette pass timed out after {_GIF_PASS_TIMEOUT}s") from exc
        if pass1.returncode != 0:
            raise ExportError(f"ffmpeg palette pass failed:\n{_stderr_tail(pass1)}")

        pass2_cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{start:g}",
            "-t",
            f"{duration:g}",
            "-i",
            str(clip_path),
            "-i",
            str(palette_path),
            "-lavfi",
            f"{scale_filter} [x]; [x][1:v] paletteuse",
            str(output_path),
        ]
        try:
            pass2 = subprocess.run(
                pass2_cmd, capture_output=True, text=True, timeout=_GIF_PASS_TIMEOUT, **NO_WINDOW_KWARGS
            )
        except subprocess.TimeoutExpired as exc:
            # ffmpeg creates the output up front (-y), so a timed-out or
            # failed encode leaves a partial .gif that would sit next to the
            # clip looking like a finished export -- same partial-output
            # leak as save_clip, same fix: delete it on every failure path.
            output_path.unlink(missing_ok=True)
            raise ExportError(f"ffmpeg GIF encode timed out after {_GIF_PASS_TIMEOUT}s") from exc
        if pass2.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise ExportError(f"ffmpeg GIF encode failed:\n{_stderr_tail(pass2)}")
    finally:
        palette_path.unlink(missing_ok=True)

    log.info("Exported GIF: %s (%.3fs-%.3fs of %s)", output_path, start, start + duration, clip_path.name)
    return output_path


def compress_clip(
    ffmpeg_path: str,
    encoder: str,
    clip_path: Path,
    out_dir: Path,
    bitrate: str = "4M",
    scale_height: int | None = None,
) -> Path:
    """Re-encode clip_path to "<stem>-compressed.mp4" in out_dir
    (counter-suffixed on collision), keeping the original untouched. Audio
    is stream-copied (-c:a copy) -- re-encoding it would cost time and
    quality for no size win at these bitrates.

    Reuses ffmpeg_utils' per-encoder arg builders: their shape fits a file
    transcode as-is (a -c:v/preset/bitrate tail is the same whether the
    input is a live capture device or a file), with two documented
    adjustments:

    - encoder_global_args goes BEFORE -i, per its own contract ("must appear
      before any -i") -- that helper is empty for every encoder but VAAPI,
      so this changes nothing for nvenc/qsv/libx264.
    - VAAPI additionally needs its filter fragment (format=nv12,hwupload)
      merged into the -vf chain, with the scale BEFORE the upload -- the
      same ordering capture._build_command uses for resolution_scale.

    Raises ValueError for a malformed bitrate or unsupported scale height,
    ExportError (with the ffmpeg stderr tail) when ffmpeg fails; a failed
    run never leaves a partial .mp4 behind.
    """
    if not _BITRATE_RE.match(bitrate):
        raise ValueError(f"Bitrate must look like 500k / 4M / 2.5M, got {bitrate!r}.")
    if scale_height is not None and scale_height not in _VALID_SCALE_HEIGHTS:
        raise ValueError(f"Scale height must be one of {_VALID_SCALE_HEIGHTS} (or None), got {scale_height}.")

    output_path = _unique_output_path(out_dir, f"{clip_path.stem}-compressed", ".mp4")

    filter_parts = []
    if scale_height is not None:
        filter_parts.append(f"scale=-2:{scale_height}")
    encoder_fragment = ffmpeg_utils.encoder_filter_fragment(encoder)
    if encoder_fragment:
        filter_parts.append(encoder_fragment)

    cmd = [
        ffmpeg_path,
        "-y",
        *ffmpeg_utils.encoder_global_args(encoder),
        "-i",
        str(clip_path),
        *ffmpeg_utils.encoder_output_args(encoder, bitrate, speed=None),
    ]
    if filter_parts:
        cmd += ["-vf", ",".join(filter_parts)]
    cmd += ["-c:a", "copy", str(output_path)]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_COMPRESS_TIMEOUT, **NO_WINDOW_KWARGS
        )
    except subprocess.TimeoutExpired as exc:
        # Same partial-output leak as export_gif: the file exists from the
        # moment ffmpeg starts, so a failed/timed-out re-encode would leave
        # a broken clip in the gallery.
        output_path.unlink(missing_ok=True)
        raise ExportError(f"ffmpeg compress timed out after {_COMPRESS_TIMEOUT}s") from exc
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise ExportError(f"ffmpeg compress failed:\n{_stderr_tail(result)}")

    log.info("Compressed clip: %s (encoder=%s, bitrate=%s)", output_path, encoder, bitrate)
    return output_path
