"""Shared platform "open a folder" helper -- the one piece of the original
pystray-based tray icon that survives the PySide6 migration (see tray_qt.py
for the QSystemTrayIcon replacement). Kept under this module name rather
than moved, since toast_qt.py, gallery_window_qt.py, main_window_qt.py, and
tray_qt.py all already import it from here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 -- opening a local folder the user configured, not user input
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)
