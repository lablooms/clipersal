"""Small standalone trigger script: sends one command to a running
clipersal instance over its local IPC socket (see ipc.py).

This exists specifically for the Wayland fallback described in ARCHITECTURE.md's
Wayland caveat: clipersal can't register a real cross-desktop global
hotkey there, so instead you bind a compositor/DE-level custom keyboard
shortcut directly to this script, e.g. (GNOME) Settings -> Keyboard ->
Custom Shortcuts -> command `clipersal-trigger save`. It works
identically on Windows/X11 too, for scripting or testing a save without
touching the keyboard at all.

`--trim SECONDS` (only valid with `save`) saves just the last SECONDS of the
buffer instead of the whole thing, e.g. `clipersal-trigger save --trim
30` for a quick "last 30 seconds" clip -- the same trim path the tray's
"Save last 30s" menu item uses.
"""

from __future__ import annotations

import argparse
import sys

from clipersal import __version__
from clipersal.ipc import DEFAULT_PORT
from clipersal.ipc_client import IpcClientError, send_command


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipersal-trigger",
        description=(
            "Send a command to a running clipersal instance over its local IPC socket. "
            "Intended for binding to a desktop-environment keyboard shortcut on Wayland, where "
            "clipersal cannot register a real global hotkey itself -- see the Wayland "
            "caveat in ARCHITECTURE.md."
        ),
    )
    parser.add_argument("--version", action="version", version=f"Clipersal {__version__}")
    parser.add_argument(
        "command",
        choices=["save", "pause", "resume", "status", "show", "settings", "gallery", "logs", "ping", "quit"],
        help="Command to send ('show' opens the main window, 'gallery' opens its Clips tab)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"IPC port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--trim",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Only valid with 'save': save just the last SECONDS of the buffer instead of the whole thing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.trim is not None and args.command != "save":
        print("error: --trim is only valid with the 'save' command", file=sys.stderr)
        return 1
    arg = str(args.trim) if args.trim is not None else None
    try:
        response = send_command(args.command.upper(), arg=arg, port=args.port)
    except IpcClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(response)
    return 0 if response.startswith("OK") else 1


if __name__ == "__main__":
    raise SystemExit(main())
