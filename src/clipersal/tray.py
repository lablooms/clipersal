"""Shared platform "open a folder / file" helpers -- the one piece of the
original pystray-based tray icon that survives the PySide6 migration (see
tray_qt.py for the QSystemTrayIcon replacement). Kept under this module name
rather than moved, since toast_qt.py, gallery_window_qt.py, main_window_qt.py,
and tray_qt.py all already import it from here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 -- opening a local folder the user configured, not user input
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def open_file(path: Path) -> None:
    """Open a file with the OS's default application (a clip in the default
    video player, a screenshot in the image viewer). Log-and-continue on
    failure -- this backs the toast's "Open" button, and a cosmetic action
    must never take the toast (or the save it celebrates) down with it.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 -- a local file the app itself just saved, not user input
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:  # noqa: BLE001 -- best-effort, see the docstring
        log.exception("Failed to open %s with the default application", path)
