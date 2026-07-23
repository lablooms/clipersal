"""Diagnostics bundle export (Logs tab -> "Export diagnostics...").

Packs everything a bug report needs into one zip: the app log plus its
rotated siblings, the capture session's own ffmpeg log (when present), a
copy of the persisted config, and a `system.txt` of best-effort machine
facts (OS, session type, Python/app/ffmpeg versions, encoder, monitors).

Never raises -- a diagnostics export that itself crashes would defeat its
entire purpose. Every included artifact is gathered behind its own guard
so one unreadable file just means a smaller zip; `None` is returned only
on total failure (the zip itself couldn't be created).
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

from clipersal import __version__, monitors, platform_detect
from clipersal.subprocess_utils import NO_WINDOW_KWARGS

log = logging.getLogger(__name__)

# RotatingFileHandler backupCount=3 (see cli._configure_logging): the live
# log plus these three rotated siblings are the whole on-disk log history.
_ROTATED_LOG_SUFFIXES = (".1", ".2", ".3")
_FFMPEG_VERSION_TIMEOUT = 10


def _add_file_if_present(bundle: zipfile.ZipFile, path: Path, arcname: str) -> None:
    """One file into the zip, skip-don't-crash: a missing/unreadable source
    (no ffmpeg.log yet on this session, a log mid-rotation) just means the
    bundle ships without it.
    """
    try:
        if path.is_file():
            bundle.write(path, arcname)
    except OSError as exc:
        log.warning("Diagnostics export: could not add %s (%s)", path, exc)


def export_diagnostics_zip(
    target_path: Path,
    log_path: Path,
    config_path: Path,
    buffer_dir: Path | None,
    facts: dict[str, str],
) -> Path | None:
    """Write a diagnostics zip to target_path and return it, or None on
    total failure. Partial bundles (some sources missing) still count as
    success -- see the module docstring.
    """
    try:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            _add_file_if_present(bundle, Path(log_path), Path(log_path).name)
            for suffix in _ROTATED_LOG_SUFFIXES:
                rotated = Path(log_path).with_name(Path(log_path).name + suffix)
                _add_file_if_present(bundle, rotated, rotated.name)
            if buffer_dir is not None:
                _add_file_if_present(bundle, Path(buffer_dir) / "ffmpeg.log", "ffmpeg.log")
            _add_file_if_present(bundle, Path(config_path), "config.json")
            system_text = "".join(f"{key}: {value}\n" for key, value in facts.items())
            bundle.writestr("system.txt", system_text)
    except Exception:  # noqa: BLE001 -- never raise out of a diagnostics export
        log.exception("Failed to export diagnostics to %s", target_path)
        try:
            Path(target_path).unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return target_path


def _ffmpeg_version_line(ffmpeg_path: str) -> str:
    """First line of `ffmpeg -version` ("ffmpeg version 7.1 ...") -- the one
    line that identifies the build. Spreads NO_WINDOW_KWARGS like every
    ffmpeg/ffprobe subprocess in this codebase (console-flash rule).
    """
    result = subprocess.run(
        [ffmpeg_path, "-version"],
        capture_output=True,
        text=True,
        timeout=_FFMPEG_VERSION_TIMEOUT,
        **NO_WINDOW_KWARGS,
    )
    lines = result.stdout.splitlines()
    return lines[0] if lines else "(no output)"


def collect_facts(ffmpeg_path: str | None = None, encoder: str | None = None) -> dict[str, str]:
    """The `system.txt` content, gathered best-effort: every fact is behind
    its own guard, so a failing probe (no ffmpeg, a monitor-enumeration
    hiccup) omits its line instead of failing the whole export. Called by
    cli.py with the live ffmpeg path / encoder so the facts describe the
    running session, not the launch-time one.
    """
    facts: dict[str, str] = {}

    def _safe(key: str, probe: Callable[[], str]) -> None:
        try:
            value = probe()
        except Exception as exc:  # noqa: BLE001 -- omit the fact, keep the rest
            log.debug("Diagnostics fact %r probe failed: %s", key, exc)
            return
        if value:
            facts[key] = value

    _safe("app_version", lambda: __version__)
    _safe("python", lambda: sys.version.split()[0])
    _safe("os", lambda: f"{platform.system()} {platform.release()} ({platform.version()})")

    def _session_type() -> str:
        os_ = platform_detect.get_os()
        if os_ == platform_detect.OS.LINUX:
            return f"linux/{platform_detect.get_linux_session_type().value}"
        return os_.value

    _safe("session_type", _session_type)
    if ffmpeg_path:
        _safe("ffmpeg_path", lambda: ffmpeg_path)
        _safe("ffmpeg_version", lambda: _ffmpeg_version_line(ffmpeg_path))
    if encoder:
        _safe("encoder", lambda: encoder)

    def _monitor_summary() -> str:
        found = monitors.list_monitors(platform_detect.get_os())
        if not found:
            return "none detected"
        return ", ".join(
            f"monitor {mon.index + 1}: {mon.width}x{mon.height}" + (" (primary)" if mon.is_primary else "")
            for mon in found
        )

    _safe("monitors", _monitor_summary)
    return facts
