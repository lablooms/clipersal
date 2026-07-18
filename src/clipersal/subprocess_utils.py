"""Shared subprocess helper: prevents a console window from flashing when
spawning ffmpeg/ffprobe from Clipersal's windowed build.

ffmpeg and ffprobe are console-subsystem executables. Spawning one from a
`--windowed` PyInstaller build still briefly allocates and flashes a new
console window on Windows unless `creationflags=subprocess.CREATE_NO_WINDOW`
is passed -- `console=False` in the packaging spec only controls Clipersal's
*own* window, not its children's. This was the actual cause behind "the
terminal still pops up every now and then": every ffmpeg subprocess call
(capture start, concat save, encoder detection/smoke tests, thumbnail
generation, ffprobe duration queries) was flashing its own console.
"""

from __future__ import annotations

import subprocess
import sys

NO_WINDOW_KWARGS: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
