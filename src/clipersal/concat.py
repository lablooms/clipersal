"""Concat-on-demand: turn currently-retained segments into a saved clip.

This is a stream-copy remux (-c copy), not a re-encode -- the actual encode
already happened once, continuously, while segments were being written. See
ARCHITECTURE.md ("Why concat is a stream copy, not a re-encode").
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from clipersal.capture import list_current_segments
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_CONCAT_TIMEOUT = 60  # seconds; generous since this is a fast stream copy
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(date|time|datetime)\}")
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


class EmptyBufferError(RuntimeError):
    pass


class ConcatFailedError(RuntimeError):
    pass


def render_filename(template: str, now: datetime | None = None) -> str:
    """Render a clip filename (without extension) from a template like
    "clip-{date}-{time}" -- the default reproduces the original hardcoded
    "clip-YYYYMMDD-HHMMSS" name exactly, so existing configs/scripts see no
    behavior change. Falls back to "clip" for a template that renders empty
    or to nothing but dots/invalid characters, rather than producing an
    unusable filename from a bad hand-edited config.
    """
    now = now or datetime.now()
    values = {
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y%m%d-%H%M%S"),
    }
    name = _TEMPLATE_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
    name = _INVALID_FILENAME_CHARS_RE.sub("_", name).strip().strip(".")
    return name or "clip"


def _unique_output_path(clips_dir: Path, base_name: str) -> Path:
    """Append -1, -2, ... if base_name.mp4 already exists -- a template
    without {time} (or a very fast double-trigger) would otherwise silently
    overwrite a previous clip.
    """
    candidate = clips_dir / f"{base_name}.mp4"
    counter = 1
    while candidate.exists():
        candidate = clips_dir / f"{base_name}-{counter}.mp4"
        counter += 1
    return candidate


def _finalized_segments(buffer_dir: Path, trim_seconds: float | None = None) -> list[Path]:
    """All currently-retained segments except the newest one, optionally
    further restricted to just the last trim_seconds (basic trim-before-save
    -- see ipc.py's argument support and cli.py's handle_save).

    ffmpeg's segment muxer keeps writing to the most recent segment file
    until it rolls over to the next one, so that file may still be growing /
    truncated. Excluding it guarantees we only ever concat finalized files.
    """
    segments = list_current_segments(buffer_dir)
    finalized = segments[:-1] if len(segments) > 1 else []
    if trim_seconds is not None and finalized:
        cutoff = time.time() - trim_seconds
        kept = []
        for p in finalized:
            # The cleanup thread can delete a segment between the listing
            # above and this stat() -- treat that as "no longer in the
            # buffer" rather than letting a raw FileNotFoundError escape
            # save_clip as an IPC ERROR (see save_clip's own re-check).
            try:
                if p.stat().st_mtime >= cutoff:
                    kept.append(p)
            except FileNotFoundError:
                pass
        finalized = kept
    return finalized


def save_clip(
    ffmpeg_path: str,
    buffer_dir: Path,
    clips_dir: Path,
    filename_template: str = "clip-{date}-{time}",
    trim_seconds: float | None = None,
) -> Path:
    """Concat currently-retained segments into a clip in clips_dir, named per
    filename_template. trim_seconds, if given, saves only the last N seconds
    of the buffer instead of the whole thing.

    Raises EmptyBufferError if not enough has been captured yet (within the
    trim window, if one was given), or ConcatFailedError if ffmpeg's remux
    fails.
    """
    finalized = _finalized_segments(buffer_dir, trim_seconds=trim_seconds)
    if not finalized:
        raise EmptyBufferError(
            "Not enough has been captured yet to save a clip -- wait a few seconds and try again."
        )

    # The cleanup thread (capture.delete_stale_segments) sweeps segments by
    # mtime on a ~1s cadence, so at steady state the oldest segment of a full
    # buffer has only ~0-3s of slack and can vanish between the listing above
    # and ffmpeg opening the concat list -- which used to fail the whole save
    # ("No such file or directory" -> ConcatFailedError) exactly when the
    # buffer was full, i.e. the common case. Re-check existence immediately
    # before spawning ffmpeg and simply leave a vanished segment out of the
    # clip; if they ALL vanished the buffer is empty for real.
    finalized = [p for p in finalized if p.exists()]
    if not finalized:
        raise EmptyBufferError(
            "Not enough has been captured yet to save a clip -- wait a few seconds and try again."
        )

    output_path = _unique_output_path(clips_dir, render_filename(filename_template))
    list_file = buffer_dir / f".concat-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"

    with open(list_file, "w", encoding="utf-8") as f:
        for segment in finalized:
            # ffmpeg concat-demuxer quoting: single-quote the path, escaping
            # any embedded single quotes as '\''.
            escaped = str(segment.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_CONCAT_TIMEOUT, **NO_WINDOW_KWARGS)
    finally:
        list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise ConcatFailedError(f"ffmpeg concat failed:\n{result.stderr.strip()[-1000:]}")

    log.info("Saved clip: %s (%d segments)", output_path, len(finalized))
    return output_path


def enforce_clip_retention(clips_dir: Path, retention_days: int, now: float | None = None) -> list[Path]:
    """Delete saved clips older than retention_days. retention_days <= 0
    disables this entirely (the default -- clips are kept forever, so a
    fresh install never surprises anyone by deleting something).

    Sweeps every .mp4 in clips_dir by age, not just ones clipersal
    itself wrote -- there's no manifest tracking that, so this assumes
    clips_dir is a dedicated folder for saved clips, the same assumption
    the Settings window's folder picker already makes.
    """
    if retention_days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    deleted = []
    for path in sorted(clips_dir.glob("*.mp4")):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not delete old clip %s: %s", path, exc)
    if deleted:
        log.info("Clip retention: deleted %d clip(s) older than %d day(s)", len(deleted), retention_days)
    return deleted
