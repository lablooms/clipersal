"""Clipersal entry point: continuous capture + rolling buffer, controlled via
the local IPC socket (ipc.py) -- from a real global hotkey on
Windows/Linux-X11, `clipersal-trigger` (trigger.py, the Wayland/DE
keybinding fallback), or the system tray icon (tray_qt.py), all funneling
into one persistent main window (main_window_qt.py). See ARCHITECTURE.md's
"Main window" section.

Exactly one QApplication is constructed here, as the very first thing in
main(), and reused for the first-run wizard, any startup-error dialogs, the
main window, and the tray icon. Cross-thread UI updates (a save completing,
a show/tab-switch request) are real Qt signals (signals.AppSignals,
connected below) rather than a polling loop -- see signals.py's docstring
for why. The tray icon doesn't need its own thread either: QSystemTrayIcon
integrates directly with QApplication's event loop.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipersal import (
    __version__,
    autostart,
    capture,
    clip_metadata,
    concat,
    config_store,
    diagnostics,
    hotkey as hotkey_module,
    ipc,
    ipc_client,
    platform_detect,
    screenshots,
    theme,
    thumbnails,
    update_check,
    window_capture,
)
from clipersal.config import build_arg_parser, config_from_args
from clipersal.ffmpeg_utils import WAYLAND_PORTAL_KIND, FfmpegNotFoundError, NoWorkingEncoderError
from clipersal.platform_detect import OS
from clipersal.portal_screencast import PortalError
from clipersal.wayland_gstreamer import GStreamerNotFoundError, PipewirePluginMissingError

try:
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    from clipersal.signals import AppSignals
except ImportError:
    QApplication = None
    QMessageBox = None
    QSystemTrayIcon = None
    AppSignals = None

log = logging.getLogger(__name__)

# resolve_setup failures that surface as a clean startup/apply error (the
# actionable message, no traceback): missing ffmpeg, no working encoder, and
# the Wayland preflight probes (no GStreamer / no pipewiresrc / no portal
# backend). portal_screencast pulls in jeepney -- pure Python, safe to import
# on Windows, and importing it here guarantees the packaged build bundles it.
_SETUP_ERRORS = (
    FfmpegNotFoundError,
    NoWorkingEncoderError,
    GStreamerNotFoundError,
    PipewirePluginMissingError,
    PortalError,
)


def _configure_logging() -> Path:
    """Rotating file handler next to the config file, plus a console handler
    when one actually exists. A --windowed packaged build has stdout/stderr
    set to None (PyInstaller's own doing, not a bug) -- logging.StreamHandler
    would crash the first time it tried to emit a record against that, so the
    console handler is only added when there's somewhere for it to write.
    """
    log_path = config_store.default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [file_handler]

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return log_path


def _configure_app_identity(app) -> None:
    """Window icon + (Windows) AppUserModelID for the shared QApplication.

    The icon comes from assets/icon.png (sys._MEIPASS under a frozen build --
    brand.app_icon owns that lookup) and is inherited by every top-level
    window/dialog. The explicit AppUserModelID makes Windows group the
    taskbar button with the installed/pinned exe's icon instead of deriving
    one from the python.exe process. Both halves are best-effort, exactly
    like the WheelGuard install below -- an identity hiccup must never block
    startup.
    """
    try:
        from clipersal import brand

        app.setWindowIcon(brand.app_icon())
    except Exception:  # noqa: BLE001 -- an icon must never be a startup failure
        log.exception("Could not set the application window icon")
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lablooms.Clipersal")
        except Exception:  # noqa: BLE001
            log.debug("Could not set the AppUserModelID", exc_info=True)


def _ensure_qapplication():
    """Returns the shared QApplication instance, constructing it (and
    applying the app's stylesheet, built from theme.py's CURRENT tokens --
    main() applies the configured mode before the first call here) the
    first time this is called. None if PySide6 isn't installed.
    """
    if QApplication is None:
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        # BODY is the app's default reading size (theme.py's typography
        # rules): every plain button/label/input inherits it, and only
        # titles/hints/mono readouts set an explicit font on top.
        app.setFont(theme.qfont(size=theme.FONT_BODY, weight="normal"))
        app.setStyleSheet(theme.build_stylesheet())
        # At construction time, so the first-run wizard / startup-error
        # dialogs that may follow already carry the icon.
        _configure_app_identity(app)
    if getattr(app, "_wheel_guard", None) is None:
        # Scroll-wheel edits on unfocused combos/spinboxes/sliders are a
        # silent-settings-corruption footgun -- the filter lives on the app
        # object so Settings AND every dialog are covered (see WheelGuard).
        # Parented to the app so it isn't garbage-collected.
        from clipersal.qt_widgets import WheelGuard

        guard = WheelGuard(app)
        app.installEventFilter(guard)
        app._wheel_guard = guard
    return app


def _show_startup_error(message: str) -> None:
    """Report a fatal startup error both to stderr and, best-effort, as a
    native dialog.

    A packaged, windowed build (see packaging/) has no console at all --
    PyInstaller's --windowed mode discards stdout/stderr entirely, so a
    plain `print(..., file=sys.stderr)` would leave the user looking at an
    app that silently failed to start with zero feedback. The dialog is the
    real fix for that; stderr stays as a fallback for anyone running from a
    terminal.
    """
    print(f"error: {message}", file=sys.stderr)
    try:
        _ensure_qapplication()
        if QMessageBox is not None:
            QMessageBox.critical(None, "Clipersal", message)
    except Exception:  # noqa: BLE001 -- stderr above is the guaranteed fallback
        pass


def _another_instance_running(port: int) -> bool:
    """Best-effort single-instance check: PING the configured IPC port before
    doing any of the slow/expensive startup work (encoder detection, starting
    ffmpeg). If something is already listening and answers, that's another
    running instance -- checked up front so a second launch gets a fast,
    friendly message instead of a bare bind-failure error. Only best-effort,
    though: under load, or when two launches race inside the startup window,
    the other end may not be listening (or answering) yet. The real backstop
    is the IpcServer bind itself right after this check, kept ahead of the
    expensive work so a lost race exits before spawning a duplicate capture
    session.
    """
    try:
        response = ipc_client.send_command("PING", port=port, timeout=0.5)
    except ipc_client.IpcClientError:
        return False
    return response.startswith("OK")


def _show_already_running_message(port: int) -> None:
    message = (
        f"Clipersal is already running (listening on 127.0.0.1:{port}).\n\n"
        "Use the tray icon, your hotkey, or clipersal-trigger to control it."
    )
    print(message, file=sys.stderr)
    try:
        _ensure_qapplication()
        if QMessageBox is not None:
            from clipersal.qt_widgets import quiet_message

            quiet_message(None, "Clipersal", message)
    except Exception:  # noqa: BLE001 -- stderr above is the guaranteed fallback
        pass


def _effective_dark(config, os_: OS) -> bool:
    """Resolve the three-way theme_mode to the bool theme.apply_theme wants:
    "dark" is always dark, "light" always light, and "system" defers to the
    OS's own dark-mode setting (platform_detect.system_dark_preferred -- a
    best-effort hint that reads as light wherever there is no such setting).
    """
    return config.theme_mode == "dark" or (
        config.theme_mode == "system" and platform_detect.system_dark_preferred(os_)
    )


class _AppState:
    """Mutable handles nested IPC/tray/settings callbacks need to read or
    swap out -- e.g. Settings changing the encoder means a new
    SegmentedCapture (and its resolved setup) replaces the old one in place.
    """

    def __init__(self, session: capture.SegmentedCapture, setup: capture.ResolvedSetup):
        self.session = session
        self.setup = setup
        self.hotkey_listener: hotkey_module.HotkeyListener | None = None
        self.paused = False


def main(argv: list[str] | None = None) -> int:
    log_path = _configure_logging()

    args = build_arg_parser().parse_args(argv)
    config = config_from_args(args)

    # Platform detection is pure stdlib probing (no Qt), so it happens up
    # here: the "system" theme mode needs os_ to resolve, before the palette
    # is applied below.
    os_ = platform_detect.get_os()
    session_type = platform_detect.get_linux_session_type() if os_ == OS.LINUX else None

    # The palette must be settled BEFORE the shared QApplication exists:
    # _ensure_qapplication builds the global stylesheet from theme.py's
    # current tokens the first time it constructs the app, so applying the
    # configured mode up here means a dark-mode launch never flashes light.
    # theme.py imports no Qt at runtime, so this is safe headless too.
    theme.apply_theme(_effective_dark(config, os_))
    app = _ensure_qapplication()

    if _another_instance_running(config.ipc_port):
        _show_already_running_message(config.ipc_port)
        return 0

    # The IPC server is bound up front, BEFORE the slow/expensive startup
    # work below (resolve_setup's encoder smoke-encodes, session.start
    # spawning ffmpeg). The PING check above is best-effort only -- it can
    # miss an instance that is itself still starting and hasn't bound yet --
    # so this bind is the real single-instance backstop, and the loser of
    # that race must exit here, not after spinning up a duplicate capture
    # session. PING is registered before start() for the same reason: a
    # concurrent launch's _another_instance_running probe gets an answer the
    # moment the socket exists and takes the friendly exit above instead of
    # this bind failure.
    server = ipc.IpcServer(port=config.ipc_port)
    server.register("PING", lambda arg=None: "PONG")
    try:
        server.start()
    except ipc.IpcServerBindError as exc:
        _show_startup_error(str(exc))
        return 1

    if app is not None and not config_store.default_config_path().exists():
        try:
            from clipersal import first_run_qt

            first_run_qt.show_first_run_wizard(config)
        except Exception:  # noqa: BLE001 -- a wizard hiccup must never block a first launch
            log.exception("First-run wizard failed; continuing with current defaults")

    try:
        setup = capture.resolve_setup(config)
    except _SETUP_ERRORS as exc:
        server.stop()  # release the port before the error dialog blocks on the user
        _show_startup_error(str(exc))
        return 1

    if setup.video_source.kind == WAYLAND_PORTAL_KIND:
        # session.start() below blocks on the desktop's own share-dialog the
        # first time around -- say so up front so that wait doesn't look like
        # a hang. The persisted restore token makes re-launches (and crash
        # restarts) dialog-free. See portal_screencast.py.
        log.info(
            "Wayland capture: on first launch your desktop will ask which screen to share; "
            "the choice is remembered, so re-launches reuse it silently."
        )

    session = capture.SegmentedCapture(config, setup)
    try:
        session.start()
    except _SETUP_ERRORS as exc:
        # On Wayland the portal handshake happens HERE, not in resolve_setup --
        # the first start blocks on the desktop's share-dialog, which the user
        # can cancel (PortalCancelledError), and the backend/fd handoff can
        # fail too. Same clean early-exit shape as the resolve_setup failure.
        server.stop()
        _show_startup_error(str(exc))
        return 1
    state = _AppState(session, setup)

    stop_event = threading.Event()
    pause_lock = threading.Lock()
    save_lock = threading.Lock()

    # Cross-thread UI notifications -- IPC handlers and the tray callback
    # (both off the GUI thread) emit these; connected to actual slots further
    # down, once main_window exists. Real Qt signals replace the old
    # queue.Queue + root.after() poll entirely -- see signals.py.
    app_signals = AppSignals() if app is not None else None
    if app_signals is not None:
        app_signals.quit_requested.connect(app.quit)

        def _on_theme_changed() -> None:
            # Rebuild the global stylesheet from the tokens apply_theme()
            # just rewrote: Qt re-polishes every widget on setStyleSheet(),
            # so all objectName/property selectors pick up the new palette
            # in place. The explicit update() sweep covers the
            # custom-painted widgets (ToggleSwitch, BrandMark, SprigAccent,
            # StatusDot) whose paintEvents read theme tokens directly --
            # with no QSS rules of their own there is nothing to re-polish,
            # so schedule their repaint by hand.
            app.setStyleSheet(theme.build_stylesheet())
            for top_level in app.topLevelWidgets():
                top_level.update()

        app_signals.theme_changed.connect(_on_theme_changed)

    def handle_save(arg: str | None = None) -> str:
        # arg, if given, is a "save just the last N seconds" trim request
        # (see clipersal-trigger's --trim and tray_qt's "Save last 30s").
        trim_seconds = float(arg) if arg else None
        # IpcServer is a ThreadingTCPServer, so two SAVEs landing together
        # (a hotkey double-press, hotkey + tray click, two trigger calls) run
        # this handler concurrently -- and concat._unique_output_path is
        # check-then-act, so both would see the same {window}-{date}-{time}
        # name (1-second template resolution) as free and remux into one
        # path with `ffmpeg -y`: interleaved corruption on Linux, a
        # file-lock failure on Windows. A plain lock -- not a try-lock; the second save should
        # wait its turn, not be dropped -- serializes them, and the later one
        # then gets a "-1" suffixed name.
        with save_lock:
            # Resolve the {window} placeholder's title once per save: in
            # window-capture mode the captured window IS the subject, so its
            # configured title names the clip; otherwise ask the OS for the
            # foreground window (None on Wayland or any probe failure --
            # render_filename then falls back to "clip").
            window_title = (
                config.window_title
                if config.capture_mode == "window"
                else window_capture.active_window_title(os_, session_type)
            )
            output_path = concat.save_clip(
                state.setup.ffmpeg_path,
                config.buffer_dir,
                config.clips_dir,
                filename_template=config.filename_template,
                trim_seconds=trim_seconds,
                window_title=window_title,
            )
            # Favorited clips are protected from the sweep (clip_metadata
            # sidecar) -- a star must mean "keep this", never "delete in N
            # days". favorites() never raises, so a corrupt sidecar just
            # means an unprotected sweep, like before favorites existed.
            concat.enforce_clip_retention(
                config.clips_dir,
                config.clip_retention_days,
                protected=clip_metadata.favorites(config.clips_dir),
            )
            # The size-cap sweep runs after retention so already-expired
            # clips don't count against the cap. The just-saved clip is
            # protected alongside the favorites: a save must never delete
            # the very clip it produced (it would also be the NEWEST file,
            # but with the cap deleting oldest-first an explicit protection
            # is the honest guarantee, not an ordering assumption).
            if config.clips_max_gb > 0:
                concat.enforce_size_cap(
                    config.clips_dir,
                    config.clips_max_gb * (1 << 30),
                    protected=clip_metadata.favorites(config.clips_dir) | {output_path.name},
                )
            # Only signaled on success -- the main window's status badge and the
            # save toast both use this to know a save actually happened, and a
            # failed save shouldn't look like one.
            if app_signals is not None:
                app_signals.save_completed.emit()
                if main_window is not None:
                    app_signals.toast_requested.emit(output_path)
            return str(output_path)

    def handle_pause(arg: str | None = None) -> str:
        with pause_lock:
            if state.paused:
                return "already paused"
            state.session.stop()
            state.paused = True
        return "paused"

    def handle_resume(arg: str | None = None) -> str:
        with pause_lock:
            # Not just "if not state.paused: no-op" -- that would also skip a
            # restart when capture is down because ffmpeg crashed and
            # auto-restart gave up (state.paused is False there too, since it
            # was never a deliberate pause). session.start() resets the
            # restart budget, so RESUME doubles as the manual recovery action
            # for that case.
            if not state.paused and state.session.is_running():
                return "already recording"
            state.session.start()
            state.paused = False
        return "resumed"

    def handle_status(arg: str | None = None) -> str:
        if state.session.gave_up_restarting():
            return "CRASHED"
        return "PAUSED" if state.paused else "RECORDING"

    def handle_stats(arg: str | None = None) -> str:
        """One-line pipe-separated key=value payload (parsed client-side by
        ipc_client.parse_stats_payload), e.g.:
        state=RECORDING|uptime=123.4|segments=27|buffer_bytes=12345678|encoder=h264_nvenc|buffer_seconds=60|clips_free_bytes=123456789|clips_count=5

        Every field is computed independently behind its own guard: a probe
        failing (a segment vanishing mid-glob, the clips folder missing)
        degrades that field to an empty string rather than failing the whole
        command -- the main window polls this on a timer, so it must be as
        never-crash as STATUS is.
        """
        try:
            state_value = "CRASHED" if state.session.gave_up_restarting() else ("PAUSED" if state.paused else "RECORDING")
        except Exception:  # noqa: BLE001 -- degrade the field, not the command
            state_value = ""

        uptime_value = ""
        try:
            uptime = state.session.uptime_seconds()
            if uptime is not None:
                uptime_value = f"{uptime:.1f}"
        except Exception:  # noqa: BLE001
            pass

        segments_value = ""
        buffer_bytes_value = ""
        try:
            count = 0
            total = 0
            for segment in config.buffer_dir.glob(capture.SEGMENT_GLOB):
                try:
                    total += segment.stat().st_size
                    count += 1
                except OSError:
                    pass  # swept by the cleanup thread mid-glob -- skip it
            segments_value = str(count)
            buffer_bytes_value = str(total)
        except OSError:
            pass

        try:
            encoder_value = state.setup.encoder or ""
        except Exception:  # noqa: BLE001
            encoder_value = ""

        try:
            clips_free_value = str(shutil.disk_usage(config.clips_dir).free)
        except OSError:
            clips_free_value = ""

        try:
            clips_count_value = str(sum(1 for _ in config.clips_dir.glob("*.mp4")))
        except OSError:
            clips_count_value = ""

        fields = [
            ("state", state_value),
            ("uptime", uptime_value),
            ("segments", segments_value),
            ("buffer_bytes", buffer_bytes_value),
            ("encoder", encoder_value),
            ("buffer_seconds", str(config.buffer_seconds)),
            ("clips_free_bytes", clips_free_value),
            ("clips_count", clips_count_value),
        ]
        return "|".join(f"{key}={value}" for key, value in fields)

    def handle_screenshot(arg: str | None = None) -> str:
        # Serialized under the same lock as SAVE: two screenshots landing
        # together would race the 1-second-resolution output name exactly
        # like two saves would (see handle_save's comment).
        with save_lock:
            output_path = screenshots.save_screenshot(
                state.setup.ffmpeg_path,
                config.buffer_dir,
                config.clips_dir,
            )
            # Its own signal, not toast_requested: the screenshot toast's
            # title reads "Screenshot saved", and the GUI side shows the PNG
            # itself rather than an ffmpeg-grabbed thumbnail.
            if app_signals is not None and main_window is not None:
                app_signals.screenshot_saved.emit(output_path)
            return str(output_path)

    def handle_quit(arg: str | None = None) -> str:
        stop_event.set()
        if app_signals is not None:
            # NOT app.quit() directly -- calling it straight from this IPC
            # handler thread (not the GUI thread) hung forever in real
            # testing, despite Qt's docs describing quit()/exit() as
            # thread-safe. Routing through a queued signal instead (see
            # signals.py's docstring) fixed it immediately.
            app_signals.quit_requested.emit()
        return "bye"

    def _handle_show_tab(tab: str | None) -> str:
        if main_window is None:
            raise RuntimeError(f"main window unavailable -- edit {config_store.default_config_path()} directly")
        app_signals.show_requested.emit(tab)
        return "showing main window" if tab is None else f"showing {tab}"

    def handle_show(arg: str | None = None) -> str:
        return _handle_show_tab(None)

    def handle_settings(arg: str | None = None) -> str:
        return _handle_show_tab("settings")

    def handle_gallery(arg: str | None = None) -> str:
        return _handle_show_tab("clips")

    def handle_logs(arg: str | None = None) -> str:
        return _handle_show_tab("logs")

    # The server itself was created and started up front, before
    # resolve_setup -- see the comment there for why. Registering the rest
    # of the handlers is deferred to here because they close over `state`,
    # which only exists once the capture session does; registering after
    # start() is fine since IpcServer shares its handler dict with the
    # socketserver.
    # `main_window` must be BOUND before the first handler can arrive: the
    # handlers read it as a late-bound closure cell, and it was previously
    # only assigned at the construction site below -- a SAVE/SHOW landing in
    # between (the hotkey listener starts in that window) crashed the
    # handler with NameError instead of answering.
    main_window = None
    server.register("SAVE", handle_save)
    server.register("PAUSE", handle_pause)
    server.register("RESUME", handle_resume)
    server.register("STATUS", handle_status)
    server.register("STATS", handle_stats)
    server.register("SCREENSHOT", handle_screenshot)
    server.register("SHOW", handle_show)
    server.register("SETTINGS", handle_settings)
    server.register("GALLERY", handle_gallery)
    server.register("LOGS", handle_logs)
    server.register("QUIT", handle_quit)

    def rebind_hotkey() -> None:
        if not config.hotkey_enabled or not hotkey_module.is_supported(os_, session_type):
            if state.hotkey_listener is not None:
                state.hotkey_listener.stop()
                state.hotkey_listener = None
            return
        # One GlobalHotKeys map for every configured combo: the main save,
        # the two quick-saves (last-N-seconds), and the screenshot. Empty
        # combos mean "disabled" and never reach pynput (apply_settings also
        # rejects duplicates, so keys can't collapse here in normal use).
        mapping = {
            config.hotkey_combo: lambda: _trigger_command_via_ipc("SAVE", config.ipc_port),
        }
        if config.quick_save_hotkey_1:
            mapping[config.quick_save_hotkey_1] = (
                lambda: _trigger_command_via_ipc(f"SAVE {config.quick_save_seconds_1}", config.ipc_port)
            )
        if config.quick_save_hotkey_2:
            mapping[config.quick_save_hotkey_2] = (
                lambda: _trigger_command_via_ipc(f"SAVE {config.quick_save_seconds_2}", config.ipc_port)
            )
        if config.screenshot_hotkey:
            mapping[config.screenshot_hotkey] = lambda: _trigger_command_via_ipc("SCREENSHOT", config.ipc_port)
        # Construct AND start the new binding before dropping the old one:
        # if the new combo can't be bound, the old listener stays alive
        # instead of leaving no hotkey at all. (apply_settings validates the
        # combo before it can get this far, so a start() failure here means
        # something environmental, e.g. the X connection dropping.)
        listener = hotkey_module.HotkeyListener.from_mapping(mapping)
        try:
            listener.start()
        except hotkey_module.HotkeyUnsupportedError as exc:
            log.warning("Global hotkey unavailable (%s); use clipersal-trigger instead", exc)
            return
        if state.hotkey_listener is not None:
            state.hotkey_listener.stop()
        state.hotkey_listener = listener

    rebind_hotkey()
    if config.hotkey_enabled and state.hotkey_listener is None and not hotkey_module.is_supported(os_, session_type):
        log.info(
            "No global hotkey on this session (Wayland has no cross-DE hotkey API). "
            "Bind `clipersal-trigger save` to a desktop keybinding instead -- see ARCHITECTURE.md."
        )

    def restart_capture(new_setup: capture.ResolvedSetup) -> None:
        """Encoder/bitrate/capture-target/mic/volume changes are baked into
        the ffmpeg command line, so applying them means swapping in a fresh
        SegmentedCapture -- preserving whatever paused/running state was
        already in effect. See ARCHITECTURE.md's "Settings persistence"
        section.

        `new_setup` must already be resolved (against the new settings) by
        the caller: resolving is the failure-prone part (it smoke-encodes
        candidate encoders), and doing it BEFORE anything is stopped means a
        resolution failure leaves the old capture running untouched instead
        of dead with nothing to replace it.

        Capture is (re)started whenever it isn't deliberately paused --
        including when it had CRASHED (auto-restart gave up): an apply that
        swaps capture settings must bring capture back with them, not swap
        in a session that never starts while STATUS claims RECORDING. If the
        new session fails to start (the Wayland share dialog can be
        cancelled here), the old -- stopped but intact -- handles are put
        back and the exception re-raised, so apply_settings' rollback path
        has a coherent session to return to.
        """
        old_session = state.session
        old_setup = state.setup
        should_run = not state.paused
        if should_run:
            # Covers the running case AND the crashed-gave-up case (a dead
            # process, a still-open ffmpeg.log handle the new session's
            # start would otherwise trip over on Windows). stop() is a
            # no-op on an already-stopped session.
            old_session.stop()
        state.setup = new_setup
        state.session = capture.SegmentedCapture(config, new_setup)
        if not should_run:
            return
        try:
            state.session.start()
        except Exception:
            state.setup = old_setup
            state.session = old_session
            raise

    def apply_settings(new_values: dict) -> str | None:
        try:
            new_clips_dir = Path(new_values["clips_dir"]).expanduser()
            new_clips_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Could not use clips folder: {exc}"

        # The hotkey field is free-typed text, so validate the combo before
        # touching anything -- persisting an unparseable combo would kill the
        # hotkey on every future launch too, since the persisted value is
        # rebound at startup.
        if not hotkey_module.is_valid_combo(new_values["hotkey_combo"]):
            return (
                f"Invalid hotkey combo: {new_values['hotkey_combo']!r} -- "
                "use pynput format, e.g. <ctrl>+<alt>+r"
            )

        # New-in-0.1.3 fields are read with .get(..., current) so the
        # 17-key payload from a pre-quick-save Settings UI keeps applying
        # cleanly; once the Settings tab sends them, the provided values win.
        new_framerate = new_values.get("framerate", config.framerate)
        new_resolution_scale = new_values.get("resolution_scale", config.resolution_scale)
        new_quick_save_hotkey_1 = new_values.get("quick_save_hotkey_1", config.quick_save_hotkey_1)
        new_quick_save_seconds_1 = new_values.get("quick_save_seconds_1", config.quick_save_seconds_1)
        new_quick_save_hotkey_2 = new_values.get("quick_save_hotkey_2", config.quick_save_hotkey_2)
        new_quick_save_seconds_2 = new_values.get("quick_save_seconds_2", config.quick_save_seconds_2)
        new_screenshot_hotkey = new_values.get("screenshot_hotkey", config.screenshot_hotkey)
        new_clips_max_gb = new_values.get("clips_max_gb", config.clips_max_gb)
        new_save_sound_enabled = new_values.get("save_sound_enabled", config.save_sound_enabled)
        new_theme_mode = new_values.get("theme_mode", config.theme_mode)

        # The only free-typed-ish theme value (a hand-edited payload could
        # hold anything -- the Settings segmented control can't) must be one
        # of the three modes, or effective_dark would silently read it as
        # light and persist the junk.
        if new_theme_mode not in ("system", "light", "dark"):
            return f"Invalid theme mode: {new_theme_mode!r} -- expected 'system', 'light', or 'dark'"

        # Same free-typed-combo gate as the main hotkey, for every non-empty
        # extra binding (empty = disabled, never validated or bound).
        for label, combo in (
            ("quick-save hotkey 1", new_quick_save_hotkey_1),
            ("quick-save hotkey 2", new_quick_save_hotkey_2),
            ("screenshot hotkey", new_screenshot_hotkey),
        ):
            if combo and not hotkey_module.is_valid_combo(combo):
                return f"Invalid {label}: {combo!r} -- use pynput format, e.g. <ctrl>+<alt>+r"

        # No two actions may share a combo: pynput's GlobalHotKeys is a plain
        # dict, so a duplicate would silently shadow one of the bindings.
        seen_combos: dict[str, str] = {}
        for label, combo in (
            ("save hotkey", new_values["hotkey_combo"]),
            ("quick-save hotkey 1", new_quick_save_hotkey_1),
            ("quick-save hotkey 2", new_quick_save_hotkey_2),
            ("screenshot hotkey", new_screenshot_hotkey),
        ):
            if not combo:
                continue
            normalized = combo.strip().lower()
            if normalized in seen_combos:
                return f"Hotkey combo {combo!r} is assigned to both {seen_combos[normalized]} and {label}."
            seen_combos[normalized] = label

        # Out-of-range quick-save windows (only reachable via a hand-edited
        # config -- the Settings spinboxes are bounded) are clamped into the
        # documented 5-300 s range rather than failing the whole apply.
        new_quick_save_seconds_1 = max(5, min(300, new_quick_save_seconds_1))
        new_quick_save_seconds_2 = max(5, min(300, new_quick_save_seconds_2))

        # A negative size cap (hand-edited config) reads as "unlimited" --
        # 0 -- rather than failing the whole apply.
        new_clips_max_gb = max(0, new_clips_max_gb)

        # Launch-on-startup registration is a pre-flight gate, like the
        # combo validations above: it runs BEFORE anything is mutated,
        # resolved, or persisted. It used to run at the very end and report
        # the failure as an error after every other field had applied and
        # persisted -- but the Settings tab treats any error string as "the
        # apply failed" and rolls every control back to the last-known-good
        # payload, which silently reverted those actually-applied settings
        # on the next save. Here, an error keeps the function's
        # all-or-nothing contract: nothing applied, nothing persisted, and
        # the rollback the tab performs tells the truth.
        if new_values["launch_on_startup"] != config.launch_on_startup and autostart.is_supported(os_):
            try:
                if new_values["launch_on_startup"]:
                    autostart.enable(os_)
                else:
                    autostart.disable(os_)
            except OSError as exc:
                log.warning("Could not update launch-on-startup registration: %s", exc)
                return f"Could not update launch-on-startup registration: {exc}"

        needs_capture_restart = (
            new_values["video_bitrate"] != config.video_bitrate
            or new_values["quality_preset"] != config.quality_preset
            or new_values["encoder_override"] != config.encoder_override
            or new_values["monitor_index"] != config.monitor_index
            or new_values["mic_device"] != config.mic_device
            or new_values["desktop_volume"] != config.desktop_volume
            or new_values["mic_volume"] != config.mic_volume
            or new_values["capture_mode"] != config.capture_mode
            or new_values["window_title"] != config.window_title
            or new_framerate != config.framerate
            or new_resolution_scale != config.resolution_scale
        )
        needs_hotkey_rebind = (
            new_values["hotkey_combo"] != config.hotkey_combo
            or new_quick_save_hotkey_1 != config.quick_save_hotkey_1
            or new_quick_save_seconds_1 != config.quick_save_seconds_1
            or new_quick_save_hotkey_2 != config.quick_save_hotkey_2
            or new_quick_save_seconds_2 != config.quick_save_seconds_2
            or new_screenshot_hotkey != config.screenshot_hotkey
        )
        needs_theme_change = new_theme_mode != config.theme_mode

        # Resolve the new capture setup BEFORE mutating config or stopping
        # the running session. Resolution is where a bad capture setting
        # fails (e.g. a forced encoder that doesn't actually work), and
        # failing here leaves the old capture running and every stored value
        # untouched -- so a second Save with the same fields diffs against
        # the real config and reports the error again, instead of a hollow
        # "saved" over a silently dead capture.
        new_setup = None
        if needs_capture_restart:
            candidate = dataclasses.replace(
                config,
                video_bitrate=new_values["video_bitrate"],
                quality_preset=new_values["quality_preset"],
                encoder_override=new_values["encoder_override"],
                monitor_index=new_values["monitor_index"],
                mic_device=new_values["mic_device"],
                desktop_volume=new_values["desktop_volume"],
                mic_volume=new_values["mic_volume"],
                capture_mode=new_values["capture_mode"],
                window_title=new_values["window_title"],
                framerate=new_framerate,
                resolution_scale=new_resolution_scale,
            )
            try:
                new_setup = capture.resolve_setup(candidate)
            except _SETUP_ERRORS as exc:
                return f"Could not apply capture settings: {exc}"

        # Snapshot of the pre-mutation config for the restart-rollback path
        # below (dataclasses.replace copies every field; its __post_init__
        # mkdirs already exist). Only needed when a restart is pending: if
        # it fails, the live config must go back to exactly what was in
        # effect -- the Settings tab rolls its controls back on any returned
        # error, and the config file was never written, so the in-memory
        # values must match both.
        if needs_capture_restart:
            previous_config = dataclasses.replace(config)

        config.buffer_seconds = new_values["buffer_seconds"]
        config.clips_dir = new_clips_dir
        config.video_bitrate = new_values["video_bitrate"]
        config.quality_preset = new_values["quality_preset"]
        config.encoder_override = new_values["encoder_override"]
        config.monitor_index = new_values["monitor_index"]
        config.mic_device = new_values["mic_device"]
        config.desktop_volume = new_values["desktop_volume"]
        config.mic_volume = new_values["mic_volume"]
        config.capture_mode = new_values["capture_mode"]
        config.window_title = new_values["window_title"]
        config.hotkey_combo = new_values["hotkey_combo"]
        config.filename_template = new_values["filename_template"]
        config.clip_retention_days = new_values["clip_retention_days"]
        config.launch_on_startup = new_values["launch_on_startup"]
        config.check_for_updates = new_values["check_for_updates"]
        config.theme_mode = new_theme_mode
        config.framerate = new_framerate
        config.resolution_scale = new_resolution_scale
        config.quick_save_hotkey_1 = new_quick_save_hotkey_1
        config.quick_save_seconds_1 = new_quick_save_seconds_1
        config.quick_save_hotkey_2 = new_quick_save_hotkey_2
        config.quick_save_seconds_2 = new_quick_save_seconds_2
        config.screenshot_hotkey = new_screenshot_hotkey
        # Both are live-mutate class (like buffer_seconds): the size cap is
        # consulted by the save/apply sweeps below and the save sound is
        # played on the toast path -- neither touches the ffmpeg command
        # line, so no capture restart.
        config.clips_max_gb = new_clips_max_gb
        config.save_sound_enabled = new_save_sound_enabled

        if needs_capture_restart:
            # new_setup was resolved above, before config was mutated --
            # resolution can't fail here. The restart itself still can (the
            # Wayland portal handshake happens at session.start(), and its
            # share dialog can be cancelled there): roll every mutation back
            # and bring the OLD capture session back, so a returned error
            # leaves config, Settings controls, disk, and capture all
            # telling the same story.
            was_running = state.session.is_running()
            try:
                with pause_lock:
                    restart_capture(new_setup)
            except Exception as exc:  # noqa: BLE001 -- the Settings contract is error strings, never exceptions
                log.exception("Capture restart failed during apply_settings; rolling back")
                for field_info in dataclasses.fields(config):
                    # Only init fields: the mutation block never touches
                    # init=False ones (buffer_dir_is_temp), and the
                    # replace()-derived snapshot can't reproduce them
                    # faithfully anyway -- see config.py's note that a
                    # replaced Config always reads as non-temp.
                    if not field_info.init:
                        continue
                    setattr(config, field_info.name, getattr(previous_config, field_info.name))
                # state.session is the old session again (restart_capture
                # restored it). Only revive one that was actually running:
                # a crashed-gave-up session stays down -- STATUS reports
                # CRASHED and the banner's own Restart button is the way
                # back, not an immediate retry of what just failed.
                if was_running:
                    try:
                        state.session.start()
                    except Exception:  # noqa: BLE001 -- doubly unlucky; say so honestly via CRASHED
                        log.exception("Could not bring the previous capture session back either")
                        state.session._gave_up = True  # honest STATUS over encapsulation, see gave_up_restarting
                return f"Could not apply capture settings: {exc}"

        if needs_hotkey_rebind:
            rebind_hotkey()

        if needs_theme_change:
            # A pure-GUI setting -- nothing in the ffmpeg command line -- so
            # it takes the live-mutate path (like buffer_seconds), never a
            # capture restart. apply_theme() rewrites theme.py's token
            # constants in place; the _on_theme_changed slot connected in
            # main() then rebuilds the global stylesheet and repaints, so
            # the whole app switches without a relaunch. A "system" pick
            # re-reads the OS's dark-mode setting right here.
            theme.apply_theme(_effective_dark(config, os_))
            if app_signals is not None:
                app_signals.theme_changed.emit()

        # Same favorite-protection as handle_save's sweep (see above).
        concat.enforce_clip_retention(
            config.clips_dir,
            config.clip_retention_days,
            protected=clip_metadata.favorites(config.clips_dir),
        )
        # Lowering the size cap takes effect immediately, not on the next
        # save -- same "Settings apply runs the sweep" pattern as retention.
        if config.clips_max_gb > 0:
            concat.enforce_size_cap(
                config.clips_dir,
                config.clips_max_gb * (1 << 30),
                protected=clip_metadata.favorites(config.clips_dir),
            )

        try:
            config_store.save_overrides(
                {
                    "buffer_seconds": config.buffer_seconds,
                    "clips_dir": str(config.clips_dir),
                    "hotkey_combo": config.hotkey_combo,
                    "video_bitrate": config.video_bitrate,
                    "quality_preset": config.quality_preset,
                    "encoder_override": config.encoder_override,
                    "monitor_index": config.monitor_index,
                    "mic_device": config.mic_device,
                    "desktop_volume": config.desktop_volume,
                    "mic_volume": config.mic_volume,
                    "capture_mode": config.capture_mode,
                    "window_title": config.window_title,
                    "filename_template": config.filename_template,
                    "clip_retention_days": config.clip_retention_days,
                    "launch_on_startup": config.launch_on_startup,
                    "check_for_updates": config.check_for_updates,
                    "theme_mode": config.theme_mode,
                    "framerate": config.framerate,
                    "resolution_scale": config.resolution_scale,
                    "quick_save_hotkey_1": config.quick_save_hotkey_1,
                    "quick_save_seconds_1": config.quick_save_seconds_1,
                    "quick_save_hotkey_2": config.quick_save_hotkey_2,
                    "quick_save_seconds_2": config.quick_save_seconds_2,
                    "screenshot_hotkey": config.screenshot_hotkey,
                    "clips_max_gb": config.clips_max_gb,
                    "save_sound_enabled": config.save_sound_enabled,
                }
            )
        except OSError as exc:
            # The in-memory apply already happened (a read-only config dir,
            # a full disk) -- but the Settings contract is "None or an error
            # string", and a disk-write failure must never escape as an
            # uncaught exception out of the autosave slot.
            log.exception("Could not persist settings")
            return f"Could not save settings to {config_store.default_config_path()}: {exc}"
        return None

    # The main window is built once, eagerly, right here -- Clipersal is a
    # real, always-present app window (like OBS), so its whole UI
    # (Home/Clips/Settings/Logs) is built up front. --no-tray doesn't affect
    # this -- the window still needs somewhere to live even with no tray
    # icon; only whether closing it hides-to-tray vs. quits depends on
    # config.tray_enabled (see MainWindow's docstring). (`main_window` itself
    # was already bound above, before the IPC handlers could first run.)
    if app is not None:
        try:
            from clipersal.main_window_qt import MainWindow

            main_window = MainWindow(
                config=config,
                ipc_port=server.port,
                save_events=None,
                current_encoder=state.setup.encoder,
                on_apply=apply_settings,
                ffmpeg_path=state.setup.ffmpeg_path,
                # A live provider, not a frozen Path -- apply_settings
                # mutates config.clips_dir in place, and the window (Home
                # recent-clips, status meta, Clips tab) must follow it
                # without an app restart.
                clips_dir_provider=lambda: config.clips_dir,
                log_path=log_path,
                tray_enabled=config.tray_enabled,
                on_quit=stop_event.set,
                app_signals=app_signals,
                # Live facts for the diagnostics zip (the Settings→Logs
                # sub-tab's export and the crash-report prompt): the encoder
                # (and potentially the ffmpeg path) changes when Settings
                # restarts capture, so collect them at export time from the
                # current state, not from launch-time values.
                diagnostics_facts_provider=lambda: diagnostics.collect_facts(
                    state.setup.ffmpeg_path, state.setup.encoder
                ),
                # Live encoder for the Clips tab's Compress dialog -- the
                # same apply_settings-can-swap-it reasoning as the facts
                # provider above.
                encoder_provider=lambda: state.setup.encoder,
            )
        except Exception as exc:  # noqa: BLE001 -- never let a GUI hiccup block startup
            log.warning(
                "Could not build the main window (%s); continuing headless -- edit %s directly",
                exc,
                config_store.default_config_path(),
            )
            main_window = None
    else:
        log.warning(
            "PySide6 is not installed; main window disabled -- edit %s directly",
            config_store.default_config_path(),
        )

    def _on_show_requested(tab: str | None) -> None:
        main_window.show()
        if tab is not None:
            main_window.select_tab(tab)

    def _maybe_play_save_sound() -> None:
        # QApplication.beep(): the zero-dependency, cross-platform save
        # chime. Guarded like every other best-effort notification in this
        # file -- a sound failure must never read as a save failure.
        if not config.save_sound_enabled or QApplication is None:
            return
        try:
            QApplication.beep()
        except Exception:  # noqa: BLE001
            log.exception("Failed to play the save sound")

    def _on_toast_requested(clip_path: Path) -> None:
        try:
            from clipersal import toast_qt

            toast_qt.show_save_toast(
                main_window, state.setup.ffmpeg_path, clip_path, config.clips_dir / thumbnails.THUMBNAIL_DIR_NAME
            )
        except Exception:  # noqa: BLE001 -- a toast failure must never break a save
            log.exception("Failed to show save toast")
        _maybe_play_save_sound()

    def _on_screenshot_saved(screenshot_path: Path) -> None:
        try:
            from clipersal import toast_qt

            toast_qt.show_save_toast(
                main_window,
                state.setup.ffmpeg_path,
                screenshot_path,
                config.clips_dir / thumbnails.THUMBNAIL_DIR_NAME,
                title="Screenshot saved",
            )
        except Exception:  # noqa: BLE001 -- a toast failure must never break a save
            log.exception("Failed to show screenshot toast")
        _maybe_play_save_sound()

    if app_signals is not None and main_window is not None:
        app_signals.show_requested.connect(_on_show_requested)
        app_signals.save_completed.connect(main_window.on_save_completed)
        app_signals.save_failed.connect(main_window.on_save_failed)
        app_signals.toast_requested.connect(_on_toast_requested)
        app_signals.screenshot_saved.connect(_on_screenshot_saved)
        app_signals.update_available.connect(main_window.show_update_banner)
        main_window.show()  # visible on launch, like OBS

        if config.check_for_updates:

            def _run_update_check() -> None:
                # check_for_update_once already guarantees it never raises --
                # no try/except needed here, same as any other best-effort
                # background probe in this codebase.
                result = update_check.check_for_update_once(repo=update_check.GITHUB_REPO, current_version=__version__)
                if result is not None:
                    version, url = result
                    app_signals.update_available.emit(version, url)

            threading.Thread(target=_run_update_check, daemon=True).start()

    tray_icon = None
    if config.tray_enabled:
        try:
            if QSystemTrayIcon is not None and QSystemTrayIcon.isSystemTrayAvailable():
                from clipersal import tray_qt

                # Same live clips-dir provider as the main window above.
                tray_icon = tray_qt.TrayIcon(server.port, lambda: config.clips_dir, log_path=log_path)
                tray_icon.show()
            else:
                log.warning(
                    "No system tray available on this machine. Continuing without it -- "
                    "hotkey and IPC/clipersal-trigger still work."
                )
        except Exception as exc:  # noqa: BLE001 -- tray is a nice-to-have, never fatal
            log.warning(
                "System tray icon unavailable (%s). Continuing without it -- "
                "hotkey and IPC/clipersal-trigger still work.",
                exc,
            )
            tray_icon = None

    print(f"Clipersal {__version__} -- catch the moment you bloomed.")
    print(f"  buffer:    {config.buffer_seconds}s  ->  {config.buffer_dir}")
    print(f"  clips dir: {config.clips_dir}")
    print(f"  encoder:   {setup.encoder}  (video source: {setup.video_source.kind})")
    print(f"  audio:     {setup.audio_source.description if setup.audio_source else 'none (video-only)'}")
    print(
        f"  ipc:       127.0.0.1:{server.port}  "
        "(commands: SAVE, PAUSE, RESUME, STATUS, STATS, SCREENSHOT, SHOW, SETTINGS, GALLERY, LOGS, PING, QUIT)"
    )
    if state.hotkey_listener is not None:
        print(f"  hotkey:    {config.hotkey_combo}  (save)")
    else:
        print(f"  hotkey:    none -- run `clipersal-trigger save --port {server.port}` instead")
    print(f"  tray:      {'enabled' if tray_icon is not None else 'disabled (--no-tray)'}")
    print(f"  settings:  {config_store.default_config_path()}")
    print(f"  logs:      {log_path}")
    print()
    print("Press Ctrl+C to quit.")

    try:
        if app is not None:
            app.exec()
        else:
            while not stop_event.is_set():
                stop_event.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        print("Stopping...")
        if tray_icon is not None:
            tray_icon.hide()
        if state.hotkey_listener is not None:
            state.hotkey_listener.stop()
        server.stop()
        # Under pause_lock for the same reason handle_pause/resume and
        # restart_capture hold it: apply_settings swaps state.session under
        # that lock, so stopping the session WITHOUT it could read the old
        # handle while the GUI thread is mid-swap -- and the freshly started
        # replacement ffmpeg would never be terminated.
        with pause_lock:
            state.session.stop()
        _cleanup_temp_buffer(config)
        if main_window is not None:
            main_window.deleteLater()
        print("Stopped.")

    return 0


def _cleanup_temp_buffer(config) -> None:
    """Delete the auto-created temp buffer dir on shutdown -- only when we
    created it ourselves (config.buffer_dir_is_temp), never a user-supplied
    --buffer-dir. Whatever segments remain at exit (up to a full rolling
    buffer, ~60 MB at the 60 s / 8 Mbit defaults) would otherwise stay in the
    system temp dir forever, accumulating across runs.
    """
    if not config.buffer_dir_is_temp:
        return
    log.info("Removing temp buffer dir %s", config.buffer_dir)
    # ignore_errors: a lingering segment still held by a just-terminated
    # ffmpeg must never turn shutdown into a crash.
    shutil.rmtree(config.buffer_dir, ignore_errors=True)


def _trigger_command_via_ipc(command: str, port: int) -> None:
    """The hotkey callbacks deliberately go back out through the IPC client
    rather than calling save_clip/save_screenshot directly, even though
    hotkey and server live in the same process today -- this is the exact
    boundary that lets the hotkey listener move into a separate sidecar
    process later without any change here. See ARCHITECTURE.md's
    "IPC / hotkey boundary" section. `command` is a full IPC command line,
    e.g. "SAVE" or "SAVE 30" for a quick-save binding.
    """
    try:
        # SAVE/SCREENSHOT get the same long leash the main window and tray
        # give them: the server-side remux can legitimately run tens of
        # seconds (see ipc_client.SAVE_TIMEOUT), and the 5s default reported
        # a slow-but-successful save as a failure in the log.
        command_word = command.split(maxsplit=1)[0]
        timeout = ipc_client.SAVE_TIMEOUT if command_word in ("SAVE", "SCREENSHOT") else 5.0
        response = ipc_client.send_command(command, port=port, timeout=timeout)
        log.info("Hotkey %s: %s", command, response)
    except ipc_client.IpcClientError as exc:
        log.warning("Hotkey %s failed: %s", command, exc)


if __name__ == "__main__":
    raise SystemExit(main())
