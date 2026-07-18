"""PyInstaller entry point for the main Clipersal app.

A separate tiny script, rather than pointing PyInstaller at the installed
package directly, because PyInstaller's Analysis needs an actual script
file as its entry point -- this is the standard pattern for packaging a
src-layout console_scripts package.

The top-level try/except here is a last-resort safety net specific to the
packaged, windowed build: cli.py already shows a dialog for the startup
failures it knows how to anticipate (ffmpeg missing, port in use, ...), but
a windowed PyInstaller build (--windowed, no console) has stdout/stderr
going nowhere -- so a genuinely unexpected exception anywhere else during
startup would otherwise fail completely silently, with no feedback at all.
"""

import sys
import traceback

from clipersal.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- last resort for a console-less build
        traceback.print_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Clipersal - unexpected error", f"{exc}\n\nSee logs for details.")
            root.destroy()
        except Exception:
            pass
        sys.exit(1)
