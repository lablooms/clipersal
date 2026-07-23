"""Qt signal bridge for cross-thread UI updates (PySide6 migration).

Replaces the queue.Queue + root.after(N, poll) pattern the CustomTkinter-era
code used to safely deliver "something happened on another thread" events to
the Tk-owned main thread. Qt's signal/slot mechanism is thread-safe by
construction: a signal emitted from any thread is delivered to a slot owned
by a QObject living on the GUI thread via Qt.QueuedConnection automatically,
as long as that QObject was itself constructed on the GUI thread (true here
-- AppSignals is meant to be built once, early, by the GUI-thread code in
cli.py, before any IPC handler or tray callback thread could emit through it
-- mirroring how the old `show_requests`/`toast_requests`/`save_events`
queues were constructed before cli.py's worker threads started).

This removes several independent polling timers (200ms/300ms/2000ms in the
old code) that existed purely as a Tk-thread-safety workaround, not as a
deliberate design choice -- see ARCHITECTURE.md's "Main window" section for the
pattern this replaces. One poll is deliberately kept as a real QTimer even
after this migration: the STATUS IPC check, since it's a genuine "did some
other trigger (tray/IPC/crash-restart) change capture state" poll with no
natural push notification without adding signal wiring to every one of those
call sites -- and it doubles as a Ctrl+C safety net under QApplication.exec()
(Python's SIGINT handler only runs between bytecode instructions, so a dead
event loop with zero timers can make Ctrl+C appear to hang).

`quit_requested` exists for a related but distinct reason, discovered while
verifying the real running app (not caught by any unit test, since those
never run a real `app.exec()` against a genuinely separate thread): calling
`QApplication.quit()` *directly* from a non-GUI thread reliably hung forever
in this environment, verified with a minimal reproduction outside this
codebase entirely -- a plain QApplication, a background thread sleeping 1s
then calling `app.quit()`, and `app.exec()` on the main thread never
returned. Routing the same request through a queued signal instead (emit
from the worker thread, connect the signal to `app.quit` on the GUI thread)
fixed it immediately in the same reproduction. Despite Qt's own docs
describing `QCoreApplication.quit()`/`exit()` as safe to call from any
thread, don't call it directly from a non-GUI thread in this codebase --
always go through a signal instead, the same as every other cross-thread
request here.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal


class AppSignals(QObject):
    """One instance, constructed on the GUI thread. IPC handlers and tray
    callbacks (running on their own threads, unchanged from before this
    migration) call `.emit(...)` on these directly instead of `queue.put(...)`.
    """

    show_requested = Signal(object)  # str | None -- a tab name, or None for "just show"
    toast_requested = Signal(Path)  # a newly saved clip's path
    screenshot_saved = Signal(object)  # Path -- a newly saved screenshot's path (its toast says "Screenshot saved", not "Clip saved", so it gets its own signal rather than reusing toast_requested)
    save_completed = Signal()  # a save succeeded -- pulse the status dot, refresh recent clips
    save_failed = Signal(str)  # a save attempt failed -- arg is the error detail, shown in the Home tab's status card (success already pops a toast via toast_requested, so only failure needs a client-side signal)
    quit_requested = Signal()  # ask the GUI thread to call QApplication.quit() -- see docstring above
    update_available = Signal(str, str)  # version, url -- a newer GitHub release was found; url opens in the browser via the Home tab's dismissible banner
    theme_changed = Signal()  # theme.apply_theme() already rewrote the tokens -- re-apply the global stylesheet and repaint (emitted from the GUI thread by apply_settings, so queued delivery isn't the point here; keeping it a signal just keeps every UI-affecting event on this one bridge)
