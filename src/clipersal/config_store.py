"""Config file persistence.

Only the settings exposed in the Settings window are persisted: buffer
length, clips folder, hotkey combo, video bitrate, quality preset, encoder
override, filename template, clip retention (days), launch-on-startup, and
check-for-updates.
`config.py`
loads this once at import time to seed argparse defaults (so precedence is
CLI flag > config file > hardcoded default), and the Settings window writes
it back out whenever it applies a change.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PERSISTED_KEYS = (
    "buffer_seconds",
    "clips_dir",
    "hotkey_combo",
    "video_bitrate",
    "quality_preset",
    "capture_mode",
    "monitor_index",
    "window_title",
    "encoder_override",
    "mic_device",
    "filename_template",
    "clip_retention_days",
    "launch_on_startup",
    "check_for_updates",
)


def default_config_path() -> Path:
    """Nested under Lablooms/Clipersal -- Clipersal is one app in the
    Lablooms studio's lineup of open-source "flower" apps, and every
    Lablooms app is expected to follow this same Lablooms/<AppName>/
    convention so they don't collide or scatter loosely across
    %APPDATA%/~/.config.
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Lablooms" / "Clipersal" / "config.json"


def default_log_path() -> Path:
    """Next to the config file -- a --windowed packaged build has no console
    for stdout/stderr to go to, so this is the only diagnostic trail a user
    (or whoever's helping them) has for a startup problem that isn't one of
    the two specific failures cli.py already shows a dialog for.
    """
    return default_config_path().parent / "clipersal.log"


def load_overrides(path: Path | None = None) -> dict[str, Any]:
    path = path or default_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read config file %s: %s -- using defaults", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Config file %s did not contain a JSON object -- using defaults", path)
        return {}
    return {key: data[key] for key in PERSISTED_KEYS if key in data}


def save_overrides(values: dict[str, Any], path: Path | None = None) -> None:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: values[key] for key in PERSISTED_KEYS if key in values}

    # Write to a temp file and rename over the real one -- a crash mid-write
    # must never leave a half-written, unparseable config file behind.
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp_path.replace(path)
    log.info("Saved settings to %s", path)
