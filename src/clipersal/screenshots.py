"""Screenshot capture: a still of "right now", taken from the rolling buffer.

Deliberately NOT a second live capture device: on Windows a concurrent
ddagrab/gdigrab instance conflicts with the capture ffmpeg's own device, and
on Wayland a fresh capture is impossible without another xdg-desktop-portal
consent round-trip. Instead the newest *finalized* segment's last frame is
extracted -- at most ~2x segment_seconds old, which matches the
instant-replay mental model (the same "what just happened" a saved clip
starts with).

Screenshots land in clips_dir as PNGs: the gallery enumerates only *.mp4 and
the retention sweep does too, so they never interact with clip bookkeeping.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from clipersal import concat
from clipersal.capture import list_current_segments
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_SCREENSHOT_TIMEOUT = 30  # seconds; a single frame-grab is normally sub-second


class ScreenshotError(RuntimeError):
    pass


def _unique_screenshot_path(clips_dir: Path, base_name: str) -> Path:
    """Append -1, -2, ... if base_name.png already exists -- the same
    convention as concat's (private) _unique_output_path, kept local so
    concat's helper stays un-exported. Two screenshots inside the same
    1-second name resolution would otherwise silently overwrite.
    """
    candidate = clips_dir / f"{base_name}.png"
    counter = 1
    while candidate.exists():
        candidate = clips_dir / f"{base_name}-{counter}.png"
        counter += 1
    return candidate


def save_screenshot(ffmpeg_path: str, buffer_dir: Path, clips_dir: Path) -> Path:
    """Write the last frame of the newest finalized buffer segment to
    clips_dir/screenshot-{date}-{time}.png (unique-suffixed on collision)
    and return its path.

    Raises concat.EmptyBufferError when nothing has been finalized yet (the
    newest segment is still being written, same exclusion as save_clip), or
    ScreenshotError when ffmpeg can't extract a frame. A partial output file
    is unlinked on every failure path, so a failed grab never leaves a
    broken PNG in the clips folder.
    """
    segments = list_current_segments(buffer_dir)
    finalized = segments[:-1] if len(segments) > 1 else []
    # Same vanish-tolerance as save_clip: the cleanup thread can sweep a
    # segment between the listing above and here.
    finalized = [p for p in finalized if p.exists()]
    if not finalized:
        raise concat.EmptyBufferError(
            "Not enough has been captured yet to take a screenshot -- wait a few seconds and try again."
        )
    segment = finalized[-1]

    output_path = _unique_screenshot_path(clips_dir, concat.render_filename("screenshot-{date}-{time}"))

    result = None
    # -sseof seeks relative to the END of the file -- exactly what "the last
    # frame" wants -- but it fails on some segment shapes (very short or
    # missing duration metadata), so a bare "-ss 0" first-frame grab is the
    # fallback rather than an error.
    for seek_args in (["-sseof", "-0.3"], ["-ss", "0"]):
        cmd = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *seek_args,
            "-i",
            str(segment),
            "-frames:v",
            "1",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SCREENSHOT_TIMEOUT, **NO_WINDOW_KWARGS
            )
        except subprocess.TimeoutExpired:
            # ffmpeg creates the output up front (-y), so a timed-out grab
            # leaves a partial PNG behind -- same cleanup rule as concat's.
            output_path.unlink(missing_ok=True)
            raise
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            log.info("Saved screenshot: %s (from %s)", output_path, segment.name)
            return output_path
        # ffmpeg can exit 0 WITHOUT writing a frame -- -sseof past the last
        # decodable frame (seen on QSV-encoded segments) decodes nothing yet
        # reports success. A missing/empty output is a failed grab: fall
        # through to the next seek strategy instead of returning a ghost path.
        output_path.unlink(missing_ok=True)

    output_path.unlink(missing_ok=True)
    raise ScreenshotError(f"ffmpeg screenshot grab failed:\n{result.stderr.strip()[-1000:]}")
