"""Clip thumbnail + duration helpers, used by the clip gallery (and the
trim dialog's frame previews).

Thumbnails are generated once per clip via a single ffmpeg frame-grab and
cached in clips_dir/.thumbnails, keyed by the clip's filename + mtime so a
replaced/re-saved clip gets a fresh thumbnail rather than a stale cached one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

_THUMBNAIL_TIMEOUT = 15
_PROBE_TIMEOUT = 10
THUMBNAIL_DIR_NAME = ".thumbnails"
THUMBNAIL_SIZE = 320


def find_ffprobe(ffmpeg_path: str) -> str | None:
    """ffprobe usually ships right next to ffmpeg in the same directory --
    check there first, then fall back to PATH. Returns None (not an error)
    if it's genuinely not available; callers should degrade gracefully
    (e.g. skip showing clip duration) rather than fail the whole gallery.
    """
    ffmpeg_file = Path(ffmpeg_path)
    candidate_name = "ffprobe.exe" if ffmpeg_file.suffix.lower() == ".exe" else "ffprobe"
    candidate = ffmpeg_file.with_name(candidate_name)
    if candidate.exists():
        return str(candidate)
    return shutil.which("ffprobe")


def thumbnail_path_for(clip_path: Path, cache_dir: Path) -> Path | None:
    """Cache path for clip_path's thumbnail, keyed on the clip's mtime.
    Returns None when the clip vanished between the caller's directory
    listing and this stat (the retention sweep runs on the IPC thread, or
    the file was deleted externally) -- the same skip-don't-crash rule as
    the gallery's own stat calls; the thumbnail worker just emits no
    thumbnail for that clip.
    """
    try:
        mtime_ns = clip_path.stat().st_mtime_ns
    except OSError:
        return None
    return cache_dir / f"{clip_path.stem}.{mtime_ns}.jpg"


def grab_frame_at(
    ffmpeg_path: str, clip_path: Path, offset_seconds: float, target_path: Path, size: int = THUMBNAIL_SIZE
) -> Path | None:
    """Single ffmpeg frame-grab at an arbitrary timestamp -- the mechanism
    behind ensure_thumbnail's fixed seeks, exposed for the trim dialog's
    Start/End previews. Unlike ensure_thumbnail there's no cache lookup and
    no fallback seek: the caller picks both the exact offset and the target
    path. Returns target_path on success, None (not an exception) on
    failure -- the same degrade-don't-crash rule as ensure_thumbnail.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # Concurrent writers (the toast fetcher, the gallery worker, the main
    # window's recent-clips worker) can grab the same clip's frame at the
    # same time, and two ffmpegs writing one file interleave their bytes --
    # leaving a corrupt JPEG that target.exists() then treated as a valid
    # cache hit forever. ffmpeg writes to a per-thread temp name in the SAME
    # directory instead, then Path.replace() moves it into place atomically
    # (the codebase's tmp+replace rule, same as config_store's writes), so
    # readers only ever see a complete file and the last writer wins
    # cleanly. A leftover temp from a crashed grab is reclaimed by
    # cleanup_orphaned_thumbnails: its rsplit-stem never matches a clip.
    temp_path = target_path.with_name(f"{target_path.stem}.tmp-{threading.get_ident()}{target_path.suffix}")
    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{offset_seconds:.3f}",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={size}:-1",
        "-update",
        "1",
        str(temp_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_THUMBNAIL_TIMEOUT, **NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Frame grab at %.3fs for %s raised: %s", offset_seconds, clip_path, exc)
        return None
    if result.returncode != 0 or not temp_path.exists():
        return None
    try:
        temp_path.replace(target_path)
    except OSError as exc:
        # e.g. the orphan sweep unlinked the temp mid-grab on POSIX -- lose
        # the thumbnail, not the worker (degrade, don't crash).
        log.warning("Could not move thumbnail %s into place for %s: %s", temp_path, clip_path, exc)
        return None
    return target_path


def ensure_thumbnail(ffmpeg_path: str, clip_path: Path, cache_dir: Path, size: int = THUMBNAIL_SIZE) -> Path | None:
    """Return a cached thumbnail path for clip_path, generating it via a
    single ffmpeg frame-grab if not already cached. Returns None (not an
    exception) on failure -- a corrupt/unreadable clip shouldn't crash the
    gallery, it should just show a placeholder instead.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = thumbnail_path_for(clip_path, cache_dir)
    if target is None:
        return None  # clip vanished between the gallery's listing and this stat
    if target.exists():
        return target

    # Try grabbing a frame half a second in (more representative than frame
    # zero for most clips); fall back to frame zero for very short clips
    # where 0.5s might be past the end.
    for seek in (0.5, 0.0):
        if grab_frame_at(ffmpeg_path, clip_path, seek, target, size=size) is not None:
            return target

    log.warning("Could not generate a thumbnail for %s", clip_path)
    return None


def get_duration_seconds(ffprobe_path: str, clip_path: Path) -> float | None:
    """Best-effort clip duration via ffprobe. Returns None if the probe
    fails -- callers should just omit duration rather than treat it as fatal.
    """
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, **NO_WINDOW_KWARGS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("ffprobe duration check for %s raised: %s", clip_path, exc)
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def cleanup_orphaned_thumbnails(cache_dir: Path, existing_clip_stems: set[str]) -> None:
    """Remove cached thumbnails for clips that no longer exist (deleted or
    renamed) -- called by the gallery on refresh so the cache doesn't grow
    unboundedly across a long-running app session.
    """
    if not cache_dir.exists():
        return
    for thumb in cache_dir.glob("*.jpg"):
        # Thumbnail filenames are "<clip-stem>.<mtime_ns>.jpg"; rsplit from
        # the right so a clip stem that itself contains dots still works.
        stem = thumb.name.rsplit(".", 2)[0]
        if stem not in existing_clip_stems:
            try:
                thumb.unlink()
            except OSError:
                pass
