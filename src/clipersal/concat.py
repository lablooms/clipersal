"""Concat-on-demand: turn currently-retained segments into a saved clip.

This is a stream-copy remux (-c copy), not a re-encode -- the actual encode
already happened once, continuously, while segments were being written. See
ARCHITECTURE.md ("Why concat is a stream copy, not a re-encode").

Also home to trim_clip: cutting a *saved* clip down to a sub-range -- the
same stream-copy trade-off in miniature.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from clipersal import thumbnails
from clipersal.capture import list_current_segments
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_CONCAT_TIMEOUT = 60  # seconds; generous since this is a fast stream copy
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(date|time|datetime|window)\}")
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_WHITESPACE_RUN_RE = re.compile(r"\s+")
# A window title can run to hundreds of characters; capped so the {window}
# placeholder can't blow past filename length limits on its own.
_WINDOW_TITLE_MAX_CHARS = 40
# Windows reserves these device basenames in EVERY folder, case-insensitively
# and ignoring any extension: "NUL.mp4" opens the null device, not a file. A
# template rendering to one of these would make ffmpeg "succeed" while writing
# nothing -- a save that reports OK yet no clip exists anywhere.
_RESERVED_DEVICE_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


class EmptyBufferError(RuntimeError):
    pass


class ConcatFailedError(RuntimeError):
    pass


class TrimRangeError(RuntimeError):
    pass


def _sanitize_window_title(window_title: str | None) -> str:
    """The {window} placeholder's value: the title with invalid filename
    characters replaced by "_", whitespace runs collapsed to single spaces,
    stripped, and capped at _WINDOW_TITLE_MAX_CHARS. Case is preserved
    ("Valorant", not "valorant"). None or empty (Wayland, an unreadable
    foreground window) renders as "clip", so "{window}-{date}-{time}"
    degrades to exactly the pre-{window} default name.
    """
    if not window_title:
        return "clip"
    title = _INVALID_FILENAME_CHARS_RE.sub("_", window_title)
    title = _WHITESPACE_RUN_RE.sub(" ", title).strip()
    title = title[:_WINDOW_TITLE_MAX_CHARS].strip()
    return title or "clip"


def render_filename(template: str, now: datetime | None = None, window_title: str | None = None) -> str:
    """Render a clip filename (without extension) from a template like
    "{window}-{date}-{time}" (the default). Placeholders: {date}, {time},
    {datetime}, and {window} -- the active window's title, sanitized by
    _sanitize_window_title ("Valorant-20260717-011351"); with no title
    (Wayland, an unreadable foreground window) {window} renders as "clip",
    reproducing the original hardcoded "clip-YYYYMMDD-HHMMSS" name. Falls
    back to "clip" for a template that renders empty, to nothing but
    dots/invalid characters, or to a Windows reserved device name, rather
    than producing an unusable filename from a bad hand-edited config.
    """
    now = now or datetime.now()
    values = {
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y%m%d-%H%M%S"),
        "window": _sanitize_window_title(window_title),
    }
    name = _TEMPLATE_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
    name = _INVALID_FILENAME_CHARS_RE.sub("_", name).strip().strip(".")
    # Windows matches reserved device names on the stem alone ("nul",
    # "NUL.txt", "com1" all hit the device -- see _RESERVED_DEVICE_BASENAMES),
    # so compare uppercased with anything from the first dot on stripped off.
    if name.upper().split(".")[0] in _RESERVED_DEVICE_BASENAMES:
        name = ""
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
    window_title: str | None = None,
) -> Path:
    """Concat currently-retained segments into a clip in clips_dir, named per
    filename_template. trim_seconds, if given, saves only the last N seconds
    of the buffer instead of the whole thing. window_title feeds the
    template's {window} placeholder (None = the placeholder's "clip"
    fallback -- exactly the pre-{window} behavior for other callers).

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

    output_path = _unique_output_path(clips_dir, render_filename(filename_template, window_title=window_title))
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
    except subprocess.TimeoutExpired:
        # ffmpeg creates the output file up front (-y), so a failed or
        # timed-out remux leaves a partial .mp4 behind: it shows up in the
        # gallery as a clip that won't play, and it holds the base name, so
        # the next successful save gets a pointless -1 suffix. Delete it on
        # every failure path.
        output_path.unlink(missing_ok=True)
        raise
    finally:
        list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise ConcatFailedError(f"ffmpeg concat failed:\n{result.stderr.strip()[-1000:]}")

    log.info("Saved clip: %s (%d segments)", output_path, len(finalized))
    return output_path


def trim_clip(
    ffmpeg_path: str,
    clip_path: Path,
    start_seconds: float,
    end_seconds: float,
    clips_dir: Path,
    duration_seconds: float | None = None,
) -> Path:
    """Write the [start_seconds, end_seconds] range of clip_path to a new
    "<stem>-trimmed.mp4" in clips_dir (counter-suffixed on collision, same
    as save_clip) and return its path. The original clip is never modified
    or deleted.

    This is a stream copy (-c copy), not a re-encode -- the same trade-off
    as save_clip in miniature: zero quality loss and effectively instant
    even for a long clip, but the cut points snap to the nearest keyframe.
    Since -force_key_frames pins a keyframe to every segment boundary
    (capture._build_command), that's at most ~segment_seconds (2s by
    default) of slack at each end; frame-exact cuts would need a re-encode,
    which is a documented deferral.

    The range is validated as 0 <= start < end <= duration before ffmpeg is
    ever spawned. duration_seconds comes from the caller when it already
    knows it (the trim dialog probes it up front); when None it's probed
    here via thumbnails' ffprobe helper. Raises TrimRangeError for an
    invalid range or an undeterminable duration, ConcatFailedError if
    ffmpeg itself fails.
    """
    if duration_seconds is None:
        ffprobe_path = thumbnails.find_ffprobe(ffmpeg_path)
        if ffprobe_path is not None:
            duration_seconds = thumbnails.get_duration_seconds(ffprobe_path, clip_path)
        if duration_seconds is None:
            raise TrimRangeError(
                f"Could not determine the duration of {clip_path.name} -- is ffprobe installed next to ffmpeg?"
            )

    if start_seconds < 0:
        raise TrimRangeError(f"Trim start must be >= 0, got {start_seconds:g}s.")
    if end_seconds > duration_seconds:
        raise TrimRangeError(f"Trim end ({end_seconds:g}s) is past the clip's duration ({duration_seconds:g}s).")
    if start_seconds >= end_seconds:
        raise TrimRangeError(f"Trim start ({start_seconds:g}s) must be before trim end ({end_seconds:g}s).")

    output_path = _unique_output_path(clips_dir, f"{clip_path.stem}-trimmed")
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-ss",
        f"{start_seconds:g}",
        "-to",
        f"{end_seconds:g}",
        "-i",
        str(clip_path),
        "-c",
        "copy",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_CONCAT_TIMEOUT, **NO_WINDOW_KWARGS)
    except subprocess.TimeoutExpired:
        # Same partial-output leak as save_clip: ffmpeg has already created
        # the output file by the time it can fail, so a broken "-trimmed.mp4"
        # would otherwise be left in clips_dir looking like a real clip.
        output_path.unlink(missing_ok=True)
        raise
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise ConcatFailedError(f"ffmpeg trim failed:\n{result.stderr.strip()[-1000:]}")

    log.info("Saved trimmed clip: %s (%.3fs-%.3fs of %s)", output_path, start_seconds, end_seconds, clip_path.name)
    return output_path


def enforce_clip_retention(
    clips_dir: Path,
    retention_days: int,
    now: float | None = None,
    protected: set[str] | None = None,
) -> list[Path]:
    """Delete saved clips older than retention_days. retention_days <= 0
    disables this entirely (the default -- clips are kept forever, so a
    fresh install never surprises anyone by deleting something).

    Sweeps every .mp4 in clips_dir by age, not just ones clipersal
    itself wrote -- there's no manifest tracking that, so this assumes
    clips_dir is a dedicated folder for saved clips, the same assumption
    the Settings window's folder picker already makes.

    protected is a set of clip filenames (full names, not stems -- the
    exact keys clip_metadata.py uses) that must survive the sweep no
    matter their age; cli.py passes the current favorites so starring a
    clip means "keep this", never "delete it in N days". None (the
    default) is exactly the pre-favorites behavior.
    """
    if retention_days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    deleted = []
    for path in sorted(clips_dir.glob("*.mp4")):
        if protected and path.name in protected:
            continue
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


def enforce_size_cap(
    clips_dir: Path,
    max_bytes: int,
    protected: set[str] | None = None,
) -> list[Path]:
    """Delete the oldest saved clips until the clips folder's total .mp4
    size fits within max_bytes. max_bytes <= 0 disables this entirely (the
    default -- unlimited, same opt-in philosophy as enforce_clip_retention:
    a fresh install never deletes anything).

    protected is a set of clip filenames (full names, not stems -- the same
    exact-match keys enforce_clip_retention uses) that must survive no
    matter their size; cli.py passes the current favorites plus, on the save
    path, the clip just saved -- a save must never delete the clip it just
    produced. Deletion stops when only protected clips remain, even if the
    folder is still over the cap.

    Same dedicated-folder assumption and vanish-tolerance as the retention
    sweep (a clip deleted externally mid-sweep is skipped, not an error);
    never raises.
    """
    if max_bytes <= 0:
        return []
    protected = protected or set()

    # Stat everything once, up front: the oldest-first deletion order below
    # is by mtime, and a clip vanishing between the glob and its stat (the
    # retention sweep, the user cleaning up by hand) is skipped rather than
    # fatal.
    clips: list[tuple[float, Path, int]] = []
    total_bytes = 0
    try:
        candidates = sorted(clips_dir.glob("*.mp4"))
    except OSError as exc:
        log.warning("Could not list clips folder %s for the size-cap sweep: %s", clips_dir, exc)
        return []
    for path in candidates:
        try:
            stat = path.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not stat clip %s for the size-cap sweep: %s", path, exc)
        else:
            clips.append((stat.st_mtime, path, stat.st_size))
            total_bytes += stat.st_size

    deleted = []
    for _mtime, path, size in sorted(clips):
        if total_bytes <= max_bytes:
            break
        if path.name in protected:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            # Gone already -- its bytes no longer count against the cap even
            # though it isn't ours to report as deleted.
            total_bytes -= size
        except OSError as exc:
            log.warning("Could not delete clip %s for the size cap: %s", path, exc)
        else:
            total_bytes -= size
            deleted.append(path)
    if deleted:
        log.info("Clips size cap: deleted %d clip(s) to fit %.1f GB", len(deleted), max_bytes / (1 << 30))
    return deleted
