"""Config file persistence.

Only the settings exposed in the Settings window are persisted: buffer
length, clips folder, hotkey combo, video bitrate, quality preset, encoder
override, filename template, clip retention (days), launch-on-startup,
check-for-updates, and dark mode.
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
    "desktop_volume",
    "mic_volume",
    "filename_template",
    "clip_retention_days",
    "launch_on_startup",
    "check_for_updates",
    "dark_mode",
)

# The JSON type each persisted key must have, mirroring config.py's Config
# fields. A hand-edited config file is user input: a wrong-typed value used to
# crash startup -- {"buffer_seconds": "abc"} reached argparse as a str default
# and SystemExit'd when converted through type=int, {"clips_dir": 123} raised
# TypeError in Path(). load_overrides therefore validates every value against
# this map and drops bad ones with a warning, exactly like it already treats
# corrupt JSON: ignored, with defaults taking over.
_KEY_TYPES: dict[str, type] = {
    "buffer_seconds": int,
    "clips_dir": str,
    "hotkey_combo": str,
    "video_bitrate": str,
    "quality_preset": str,
    "capture_mode": str,
    "monitor_index": int,
    "window_title": str,
    "encoder_override": str,
    "mic_device": str,
    "desktop_volume": int,
    "mic_volume": int,
    "filename_template": str,
    "clip_retention_days": int,
    "launch_on_startup": bool,
    "check_for_updates": bool,
    "dark_mode": bool,
}

# encoder_override / mic_device legitimately persist null ("no override" /
# "no microphone"), so None is a valid value for exactly these two keys.
_NONEABLE_KEYS = frozenset({"encoder_override", "mic_device"})


def _is_valid_type(key: str, value: Any) -> bool:
    if value is None:
        return key in _NONEABLE_KEYS
    expected = _KEY_TYPES[key]
    if expected is bool:
        # Only a real JSON true/false -- 1/0 is a wrong type here, not truthiness.
        return isinstance(value, bool)
    if expected is int:
        # bool IS a subclass of int, but {"buffer_seconds": true} is a typo,
        # not a buffer length -- reject it explicitly.
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


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
    overrides: dict[str, Any] = {}
    for key in PERSISTED_KEYS:
        if key not in data:
            continue
        value = data[key]
        if _is_valid_type(key, value):
            overrides[key] = value
        else:
            log.warning(
                "Ignoring wrong-typed value for %r in config file %s (%r) -- using the default",
                key,
                path,
                value,
            )
    return overrides


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
