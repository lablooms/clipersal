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
    concat,
    config_store,
    hotkey as hotkey_module,
    ipc,
    ipc_client,
    platform_detect,
    thumbnails,
    update_check,
)
from clipersal.config import build_arg_parser, config_from_args
from clipersal.ffmpeg_utils import FfmpegNotFoundError, NoWorkingEncoderError, WaylandCaptureNotImplementedError
from clipersal.platform_detect import OS

try:
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    from clipersal.signals import AppSignals
except ImportError:
    QApplication = None
    QMessageBox = None
    QSystemTrayIcon = None
    AppSignals = None

log = logging.getLogger(__name__)


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


def _ensure_qapplication():
    """Returns the shared QApplication instance, constructing it (and
    applying the app's stylesheet) the first time this is called. None if
    PySide6 isn't installed.
    """
    if QApplication is None:
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        from clipersal import theme

        app.setStyleSheet(theme.build_stylesheet())
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
            QMessageBox.information(None, "Clipersal", message)
    except Exception:  # noqa: BLE001 -- stderr above is the guaranteed fallback
        pass


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
    app = _ensure_qapplication()

    args = build_arg_parser().parse_args(argv)
    config = config_from_args(args)

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
    except (FfmpegNotFoundError, WaylandCaptureNotImplementedError, NoWorkingEncoderError) as exc:
        server.stop()  # release the port before the error dialog blocks on the user
        _show_startup_error(str(exc))
        return 1

    session = capture.SegmentedCapture(config, setup)
    session.start()
    state = _AppState(session, setup)

    stop_event = threading.Event()
    pause_lock = threading.Lock()
    save_lock = threading.Lock()

    os_ = platform_detect.get_os()
    session_type = platform_detect.get_linux_session_type() if os_ == OS.LINUX else None

    # Cross-thread UI notifications -- IPC handlers and the tray callback
    # (both off the GUI thread) emit these; connected to actual slots further
    # down, once main_window exists. Real Qt signals replace the old
    # queue.Queue + root.after() poll entirely -- see signals.py.
    app_signals = AppSignals() if app is not None else None
    if app_signals is not None:
        app_signals.quit_requested.connect(app.quit)

    def handle_save(arg: str | None = None) -> str:
        # arg, if given, is a "save just the last N seconds" trim request
        # (see clipersal-trigger's --trim and tray_qt's "Save last 30s").
        trim_seconds = float(arg) if arg else None
        # IpcServer is a ThreadingTCPServer, so two SAVEs landing together
        # (a hotkey double-press, hotkey + tray click, two trigger calls) run
        # this handler concurrently -- and concat._unique_output_path is
        # check-then-act, so both would see the same clip-{date}-{time} name
        # (1-second template resolution) as free and remux into one path with
        # `ffmpeg -y`: interleaved corruption on Linux, a file-lock failure
        # on Windows. A plain lock -- not a try-lock; the second save should
        # wait its turn, not be dropped -- serializes them, and the later one
        # then gets a "-1" suffixed name.
        with save_lock:
            output_path = concat.save_clip(
                state.setup.ffmpeg_path,
                config.buffer_dir,
                config.clips_dir,
                filename_template=config.filename_template,
                trim_seconds=trim_seconds,
            )
            concat.enforce_clip_retention(config.clips_dir, config.clip_retention_days)
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
    server.register("SAVE", handle_save)
    server.register("PAUSE", handle_pause)
    server.register("RESUME", handle_resume)
    server.register("STATUS", handle_status)
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
        # Construct AND start the new binding before dropping the old one:
        # if the new combo can't be bound, the old listener stays alive
        # instead of leaving no hotkey at all. (apply_settings validates the
        # combo before it can get this far, so a start() failure here means
        # something environmental, e.g. the X connection dropping.)
        listener = hotkey_module.HotkeyListener(
            config.hotkey_combo, callback=lambda: _trigger_save_via_ipc(config.ipc_port)
        )
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
        """Encoder/bitrate/capture-target/mic changes are baked into the
        ffmpeg command line, so applying them means swapping in a fresh
        SegmentedCapture -- preserving whatever paused/running state was
        already in effect. See ARCHITECTURE.md's "Settings persistence"
        section.

        `new_setup` must already be resolved (against the new settings) by
        the caller: resolving is the failure-prone part (it smoke-encodes
        candidate encoders), and doing it BEFORE anything is stopped means a
        resolution failure leaves the old capture running untouched instead
        of dead with nothing to replace it.
        """
        was_running = state.session.is_running()
        if was_running:
            state.session.stop()
        state.setup = new_setup
        state.session = capture.SegmentedCapture(config, new_setup)
        if was_running:
            state.session.start()

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

        needs_capture_restart = (
            new_values["video_bitrate"] != config.video_bitrate
            or new_values["quality_preset"] != config.quality_preset
            or new_values["encoder_override"] != config.encoder_override
            or new_values["monitor_index"] != config.monitor_index
            or new_values["mic_device"] != config.mic_device
            or new_values["capture_mode"] != config.capture_mode
            or new_values["window_title"] != config.window_title
        )
        needs_hotkey_rebind = new_values["hotkey_combo"] != config.hotkey_combo
        needs_autostart_change = new_values["launch_on_startup"] != config.launch_on_startup

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
                capture_mode=new_values["capture_mode"],
                window_title=new_values["window_title"],
            )
            try:
                new_setup = capture.resolve_setup(candidate)
            except (FfmpegNotFoundError, WaylandCaptureNotImplementedError, NoWorkingEncoderError) as exc:
                return f"Could not apply encoder/bitrate change: {exc}"

        config.buffer_seconds = new_values["buffer_seconds"]
        config.clips_dir = new_clips_dir
        config.video_bitrate = new_values["video_bitrate"]
        config.quality_preset = new_values["quality_preset"]
        config.encoder_override = new_values["encoder_override"]
        config.monitor_index = new_values["monitor_index"]
        config.mic_device = new_values["mic_device"]
        config.capture_mode = new_values["capture_mode"]
        config.window_title = new_values["window_title"]
        config.hotkey_combo = new_values["hotkey_combo"]
        config.filename_template = new_values["filename_template"]
        config.clip_retention_days = new_values["clip_retention_days"]
        config.launch_on_startup = new_values["launch_on_startup"]
        config.check_for_updates = new_values["check_for_updates"]

        if needs_autostart_change and autostart.is_supported(os_):
            try:
                if config.launch_on_startup:
                    autostart.enable(os_)
                else:
                    autostart.disable(os_)
            except OSError as exc:
                log.warning("Could not update launch-on-startup registration: %s", exc)

        if needs_capture_restart:
            # new_setup was resolved above, before config was mutated -- it
            # can't fail here, so the old session is only stopped once its
            # replacement is known-good.
            with pause_lock:
                restart_capture(new_setup)

        if needs_hotkey_rebind:
            rebind_hotkey()

        concat.enforce_clip_retention(config.clips_dir, config.clip_retention_days)

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
                "capture_mode": config.capture_mode,
                "window_title": config.window_title,
                "filename_template": config.filename_template,
                "clip_retention_days": config.clip_retention_days,
                "launch_on_startup": config.launch_on_startup,
                "check_for_updates": config.check_for_updates,
            }
        )
        return None

    # The main window is built once, eagerly, right here -- Clipersal is a
    # real, always-present app window (like OBS), so its whole UI
    # (Home/Clips/Settings/Logs) is built up front. --no-tray doesn't affect
    # this -- the window still needs somewhere to live even with no tray
    # icon; only whether closing it hides-to-tray vs. quits depends on
    # config.tray_enabled (see MainWindow's docstring).
    main_window = None
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

    def _on_toast_requested(clip_path: Path) -> None:
        try:
            from clipersal import toast_qt

            toast_qt.show_save_toast(
                main_window, state.setup.ffmpeg_path, clip_path, config.clips_dir / thumbnails.THUMBNAIL_DIR_NAME
            )
        except Exception:  # noqa: BLE001 -- a toast failure must never break a save
            log.exception("Failed to show save toast")

    if app_signals is not None and main_window is not None:
        app_signals.show_requested.connect(_on_show_requested)
        app_signals.save_completed.connect(main_window.on_save_completed)
        app_signals.save_failed.connect(main_window.on_save_failed)
        app_signals.toast_requested.connect(_on_toast_requested)
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
        "(commands: SAVE, PAUSE, RESUME, STATUS, SHOW, SETTINGS, GALLERY, LOGS, PING, QUIT)"
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


def _trigger_save_via_ipc(port: int) -> None:
    """The hotkey callback deliberately goes back out through the IPC client
    rather than calling save_clip directly, even though hotkey and server
    live in the same process today -- this is the exact boundary that lets
    the hotkey listener move into a separate sidecar process later without
    any change here. See ARCHITECTURE.md's "IPC / hotkey boundary" section.
    """
    try:
        response = ipc_client.send_command("SAVE", port=port)
        log.info("Hotkey save: %s", response)
    except ipc_client.IpcClientError as exc:
        log.warning("Hotkey save failed: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
