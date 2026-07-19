"""Application configuration: a plain dataclass with sane defaults, layered
with (in increasing priority) hardcoded defaults, the persisted config file
(config_store.py), and CLI flags. The persisted subset -- buffer length,
clips folder, hotkey combo, video bitrate, encoder override, filename
template, and clip retention -- is exactly what the Settings window
(settings_window.py) edits.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipersal import __version__, config_store
from clipersal.hotkey import DEFAULT_COMBO as _DEFAULT_HOTKEY_COMBO
from clipersal.ipc import DEFAULT_PORT as _DEFAULT_IPC_PORT


def _default_clips_dir() -> Path:
    return Path.home() / "Videos" / "Clipersal"


@dataclass
class Config:
    buffer_seconds: int = 60
    segment_seconds: int = 2
    cleanup_interval_seconds: float = 1.0
    framerate: int = 30
    capture_mode: str = "desktop"  # "desktop" | "monitor" | "window"
    monitor_index: int = 0  # 0 = default (primary output / whole desktop) -- see monitors.py; only used when capture_mode == "monitor"
    window_title: str = ""  # only used when capture_mode == "window"
    video_bitrate: str = "8M"
    quality_preset: str = "custom"  # "performance" | "balanced" | "quality" | "custom" (raw video_bitrate)
    encoder_override: str | None = None
    mic_device: str | None = None  # None = no microphone mixed in (default, unchanged behavior)
    clips_dir: Path = field(default_factory=_default_clips_dir)
    # None = not supplied -> a fresh temp dir is created in __post_init__ and
    # buffer_dir_is_temp flips True so cli.py's shutdown can delete it again
    # (otherwise every run leaks up to a full buffer, ~60 MB at defaults, into
    # the system temp dir). After __post_init__ this is always a Path.
    buffer_dir: Path | None = None
    buffer_dir_is_temp: bool = field(init=False, default=False)
    ipc_port: int = _DEFAULT_IPC_PORT
    hotkey_combo: str = _DEFAULT_HOTKEY_COMBO
    hotkey_enabled: bool = True
    tray_enabled: bool = True
    filename_template: str = "clip-{date}-{time}"
    clip_retention_days: int = 0  # 0 = keep saved clips forever
    launch_on_startup: bool = False
    check_for_updates: bool = True

    def __post_init__(self) -> None:
        self.clips_dir = Path(self.clips_dir).expanduser()
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        if self.buffer_dir is None:
            self.buffer_dir = Path(tempfile.mkdtemp(prefix="clipersal-buffer-"))
            self.buffer_dir_is_temp = True
        else:
            # Note: dataclasses.replace() passes the resolved path back in, so
            # a replaced Config always reads as non-temp -- the safe direction
            # (shutdown never deletes a dir it didn't create itself).
            self.buffer_dir = Path(self.buffer_dir).expanduser()
        self.buffer_dir.mkdir(parents=True, exist_ok=True)


def build_arg_parser(persisted: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    """Build the CLI parser. `persisted` (defaults to whatever's in the config
    file) seeds argparse defaults, giving the precedence CLI flag > config
    file > hardcoded default -- a flag explicitly passed on the command line
    always wins, but otherwise a saved Settings-window value is used instead
    of the hardcoded fallback.
    """
    if persisted is None:
        persisted = config_store.load_overrides()

    default_clips_dir = Path(persisted["clips_dir"]) if "clips_dir" in persisted else _default_clips_dir()

    parser = argparse.ArgumentParser(
        prog="clipersal",
        description="Clipersal (by Lablooms) -- continuous rolling screen-capture buffer with save-on-demand.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Clipersal {__version__}",
    )
    parser.add_argument(
        "--buffer-seconds",
        type=int,
        default=persisted.get("buffer_seconds", 60),
        help="How much capture history to retain (default: 60)",
    )
    parser.add_argument(
        "--clips-dir", type=Path, default=default_clips_dir, help="Folder saved clips are written to"
    )
    parser.add_argument(
        "--buffer-dir", type=Path, default=None, help="Folder segment files are written to (default: a temp dir)"
    )
    parser.add_argument(
        "--bitrate",
        type=str,
        default=persisted.get("video_bitrate", "8M"),
        help="Target video bitrate, e.g. 8M (default: 8M)",
    )
    parser.add_argument(
        "--framerate", type=int, default=30, help="Capture framerate (default: 30)"
    )
    parser.add_argument(
        "--capture-mode",
        type=str,
        choices=["desktop", "monitor", "window"],
        default=persisted.get("capture_mode", "desktop"),
        help="What to capture: the whole desktop, one monitor, or one window (default: desktop)",
    )
    parser.add_argument(
        "--monitor-index",
        type=int,
        default=persisted.get("monitor_index", 0),
        help="Which monitor to capture when --capture-mode=monitor (0 = primary output)",
    )
    parser.add_argument(
        "--window-title",
        type=str,
        default=persisted.get("window_title", ""),
        help="Exact window title to capture when --capture-mode=window",
    )
    parser.add_argument(
        "--quality-preset",
        type=str,
        choices=["performance", "balanced", "quality", "custom"],
        default=persisted.get("quality_preset", "custom"),
        help="Named bitrate/speed preset, or 'custom' to use --bitrate directly (default: custom)",
    )
    parser.add_argument(
        "--encoder",
        type=str,
        default=persisted.get("encoder_override"),
        help="Force a specific encoder instead of auto-detecting",
    )
    parser.add_argument(
        "--mic-device",
        type=str,
        default=persisted.get("mic_device"),
        help="Microphone device name to mix in alongside system audio (default: none)",
    )
    parser.add_argument(
        "--ipc-port",
        type=int,
        default=_DEFAULT_IPC_PORT,
        help=f"Local IPC port used to trigger saves (default: {_DEFAULT_IPC_PORT})",
    )
    parser.add_argument(
        "--hotkey",
        type=str,
        default=persisted.get("hotkey_combo", _DEFAULT_HOTKEY_COMBO),
        help=f"Global hotkey combo to save, pynput format (default: {_DEFAULT_HOTKEY_COMBO})",
    )
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help="Don't bind a global hotkey; trigger saves only via the IPC socket / clipersal-trigger",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Don't show a system tray icon; control clipersal via the hotkey / IPC socket only",
    )
    parser.add_argument(
        "--filename-template",
        type=str,
        default=persisted.get("filename_template", "clip-{date}-{time}"),
        help="Saved clip filename pattern -- {date}, {time}, {datetime} placeholders (default: clip-{date}-{time})",
    )
    parser.add_argument(
        "--clip-retention-days",
        type=int,
        default=persisted.get("clip_retention_days", 0),
        help="Delete saved clips older than N days; 0 = keep forever (default: 0)",
    )
    parser.add_argument(
        "--launch-on-startup",
        action=argparse.BooleanOptionalAction,
        default=persisted.get("launch_on_startup", False),
        help="Register clipersal to launch automatically at login (default: %(default)s)",
    )
    parser.add_argument(
        "--check-for-updates",
        action=argparse.BooleanOptionalAction,
        default=persisted.get("check_for_updates", True),
        help="Check GitHub Releases for a newer version at startup and show a notice if found (default: %(default)s)",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    kwargs = dict(
        buffer_seconds=args.buffer_seconds,
        clips_dir=args.clips_dir,
        video_bitrate=args.bitrate,
        quality_preset=args.quality_preset,
        framerate=args.framerate,
        capture_mode=args.capture_mode,
        monitor_index=args.monitor_index,
        window_title=args.window_title,
        encoder_override=args.encoder,
        mic_device=args.mic_device,
        ipc_port=args.ipc_port,
        hotkey_combo=args.hotkey,
        hotkey_enabled=not args.no_hotkey,
        tray_enabled=not args.no_tray,
        filename_template=args.filename_template,
        clip_retention_days=args.clip_retention_days,
        launch_on_startup=args.launch_on_startup,
        check_for_updates=args.check_for_updates,
    )
    if args.buffer_dir is not None:
        kwargs["buffer_dir"] = args.buffer_dir
    return Config(**kwargs)
