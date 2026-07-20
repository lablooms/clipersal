import threading
import time
from pathlib import Path
from types import SimpleNamespace

import clipersal.cli as cli
from clipersal import ipc
from clipersal.cli import _another_instance_running
from clipersal.config import Config
from clipersal.ipc import IpcServer
from clipersal.portal_screencast import PortalCancelledError
from clipersal.portal_screencast import PortalBackendError, PortalCancelledError, PortalUnavailableError
from clipersal.wayland_gstreamer import GStreamerNotFoundError, PipewirePluginMissingError


def test_another_instance_running_true_when_something_answers_ping() -> None:
    server = IpcServer(port=0)
    server.register("PING", lambda arg=None: "PONG")
    server.start()
    try:
        assert _another_instance_running(server.port) is True
    finally:
        server.stop()


def test_another_instance_running_false_when_port_unreachable() -> None:
    assert _another_instance_running(1) is False


def test_another_instance_running_false_when_ping_errors() -> None:
    server = IpcServer(port=0)

    def boom(arg=None):
        raise RuntimeError("nope")

    server.register("PING", boom)
    server.start()
    try:
        assert _another_instance_running(server.port) is False
    finally:
        server.stop()


def _install_headless_startup_fakes(monkeypatch, tmp_path):
    """Stand-ins for everything main() touches on its startup path, so the
    single-instance logic can be driven without a QApplication, an ffmpeg, or
    a real IPC socket. `calls` records the order main() actually did things
    in -- the ordering assertions are the point of the tests below.
    """
    calls = []
    startup_errors = []
    fakes = SimpleNamespace(server=None, fail_on_start=False, session_fail_with=None, calls=calls, startup_errors=startup_errors)

    class FakeIpcServer:
        def __init__(self, host="127.0.0.1", port=51525):
            self.port = port
            self.handlers = {}
            fakes.server = self

        def register(self, command, handler):
            self.handlers[command] = handler

        def start(self):
            calls.append("ipc_start")
            self.handlers_at_start = dict(self.handlers)
            if fakes.fail_on_start:
                raise ipc.IpcServerBindError("Could not bind IPC socket (fake)")

        def stop(self):
            calls.append("ipc_stop")

    class FakeSession:
        def __init__(self, config, setup):
            calls.append("session_construct")

        def start(self):
            calls.append("session_start")
            if fakes.session_fail_with is not None:
                raise fakes.session_fail_with

        def stop(self):
            calls.append("session_stop")

        def is_running(self):
            return True

        def gave_up_restarting(self):
            return False

    fake_setup = SimpleNamespace(
        encoder="fake-encoder",
        video_source=SimpleNamespace(kind="fake"),
        audio_source=None,
        ffmpeg_path="ffmpeg",
    )
    fake_config = SimpleNamespace(
        ipc_port=0,
        hotkey_enabled=False,
        tray_enabled=False,
        buffer_seconds=30,
        buffer_dir=tmp_path / "buffer",
        buffer_dir_is_temp=False,
        clips_dir=tmp_path / "clips",
        filename_template="clip-test",
        clip_retention_days=0,
        dark_mode=False,
    )
    fakes.config = fake_config

    monkeypatch.setattr(cli, "_configure_logging", lambda: tmp_path / "clipersal.log")
    monkeypatch.setattr(cli, "_ensure_qapplication", lambda: None)
    monkeypatch.setattr(cli, "_another_instance_running", lambda port: False)
    monkeypatch.setattr(cli, "config_from_args", lambda args: fake_config)
    monkeypatch.setattr(cli, "_show_startup_error", startup_errors.append)
    monkeypatch.setattr(cli.capture, "resolve_setup", lambda config: calls.append("resolve_setup") or fake_setup)
    monkeypatch.setattr(cli.capture, "SegmentedCapture", FakeSession)
    monkeypatch.setattr(cli.ipc, "IpcServer", FakeIpcServer)

    return fakes


def test_main_binds_ipc_before_starting_capture(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # main() blocks in its run loop once startup completes; wait until every
    # handler (including QUIT, the last one registered) is attached.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "QUIT" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "QUIT" in fakes.server.handlers

    # The point of the early bind: the port is taken (and already answers
    # PING, so a concurrent launch's single-instance probe sees us) before
    # any of the expensive capture startup happens.
    assert fakes.calls.index("ipc_start") < fakes.calls.index("resolve_setup")
    assert fakes.calls.index("ipc_start") < fakes.calls.index("session_construct")
    assert fakes.calls.index("ipc_start") < fakes.calls.index("session_start")
    assert "PING" in fakes.server.handlers_at_start

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["rc"] == 0
    assert "ipc_stop" in fakes.calls
    assert "session_stop" in fakes.calls


def test_main_bind_failure_exits_before_starting_capture(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.fail_on_start = True

    rc = cli.main([])

    assert rc == 1
    assert len(fakes.startup_errors) == 1
    assert "Could not bind IPC socket" in fakes.startup_errors[0]
    # A lost single-instance race must exit without ever spinning up a
    # duplicate capture session -- previously ffmpeg was started first and
    # only then did the bind fail.
    assert "resolve_setup" not in fakes.calls
    assert "session_start" not in fakes.calls


def test_main_start_failure_exits_cleanly_with_startup_error(monkeypatch, tmp_path) -> None:
    # On Wayland the portal handshake happens inside session.start() -- the
    # user can cancel the desktop's share-dialog (PortalCancelledError) there,
    # which previously escaped main() as an uncaught exception.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.session_fail_with = PortalCancelledError("user declined the share dialog")

    rc = cli.main([])

    assert rc == 1
    assert "session_start" in fakes.calls
    assert len(fakes.startup_errors) == 1
    assert "user declined" in fakes.startup_errors[0]
    assert "ipc_stop" in fakes.calls  # port released, same early-exit shape


def test_main_wayland_setup_errors_hit_the_clean_startup_error_path(monkeypatch, tmp_path) -> None:
    # The Wayland preflight probes (GStreamer/pipewiresrc/portal) raise typed
    # errors from resolve_setup; each must surface exactly like a missing
    # ffmpeg: port released, actionable message shown, no capture started.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)

    setup_errors = [
        GStreamerNotFoundError("GStreamer was not found on PATH (fake)"),
        PipewirePluginMissingError("the 'pipewiresrc' element is missing (fake)"),
        PortalUnavailableError("the ScreenCast service is not reachable (fake)"),
        PortalBackendError("the portal service appears to be wedged (fake)"),
        PortalCancelledError("cancelled in the system dialog (fake)"),
    ]
    for exc in setup_errors:
        fakes.calls.clear()
        fakes.startup_errors.clear()

        def fake_resolve_setup(config, exc=exc):
            raise exc

        monkeypatch.setattr(cli.capture, "resolve_setup", fake_resolve_setup)

        rc = cli.main([])

        assert rc == 1, exc
        assert fakes.startup_errors == [str(exc)]
        assert "session_construct" not in fakes.calls
        assert "session_start" not in fakes.calls
        # The port is released before the error dialog can block on the user.
        assert "ipc_stop" in fakes.calls


def test_concurrent_saves_are_serialized_and_get_distinct_names(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    # Fake save_clip records its own concurrency and -- deliberately, with a
    # sleep between entry and the name pick -- reproduces the check-then-act
    # race in _unique_output_path: without cli.py's save lock, both threads
    # would see "clip-test.mp4" as free and return the SAME path.
    concurrency = {"current": 0, "max": 0}
    concurrency_lock = threading.Lock()

    def fake_save_clip(ffmpeg_path, buffer_dir, clips_dir_, filename_template, trim_seconds):
        with concurrency_lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            time.sleep(0.05)
            output = cli.concat._unique_output_path(clips_dir_, filename_template)
            output.write_bytes(b"clip")
            return output
        finally:
            with concurrency_lock:
                concurrency["current"] -= 1

    monkeypatch.setattr(cli.concat, "save_clip", fake_save_clip)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda clips_dir, days: [])

    result: dict = {}

    def run() -> None:
        result["rc"] = cli.main([])

    main_thread = threading.Thread(target=run, daemon=True)
    main_thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "QUIT" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "QUIT" in fakes.server.handlers
    save = fakes.server.handlers["SAVE"]

    results = []
    threads = [threading.Thread(target=lambda: results.append(save())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads)

    fakes.server.handlers["QUIT"]()
    main_thread.join(timeout=10)

    # The save lock serialized the two handler invocations...
    assert concurrency["max"] == 1
    # ...so the second one saw the first's file and got a suffixed name.
    assert len(results) == 2
    assert sorted(Path(r).name for r in results) == ["clip-test-1.mp4", "clip-test.mp4"]


# ---- apply_settings / rebind_hotkey / shutdown -------------------------------
#
# apply_settings only exists as a closure inside main(), handed to the main
# window as on_apply -- so these tests drive it through a fully faked main()
# run: stand-in QApplication/AppSignals/MainWindow, fake capture session, fake
# IPC server. No real Qt, ffmpeg, sockets, or config files.


def _settings_values(config, **overrides):
    """A full Settings-Save payload (same keys settings_window_qt sends),
    seeded from the config's current values so tests can flip one field."""
    values = {
        "buffer_seconds": config.buffer_seconds,
        "clips_dir": str(config.clips_dir),
        "hotkey_combo": config.hotkey_combo,
        "video_bitrate": config.video_bitrate,
        "quality_preset": config.quality_preset,
        "capture_mode": config.capture_mode,
        "monitor_index": config.monitor_index,
        "window_title": config.window_title,
        "mic_device": config.mic_device,
        "desktop_volume": config.desktop_volume,
        "mic_volume": config.mic_volume,
        "encoder_override": config.encoder_override,
        "filename_template": config.filename_template,
        "clip_retention_days": config.clip_retention_days,
        "launch_on_startup": config.launch_on_startup,
        "check_for_updates": config.check_for_updates,
        "dark_mode": config.dark_mode,
    }
    values.update(overrides)
    return values


def _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=False):
    calls = []
    fakes = SimpleNamespace(
        server=None, calls=calls, on_apply=None, saved_overrides=[], hotkey_events=[], config=None, app=None
    )
    quit_event = threading.Event()

    class FakeSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        # Synchronous delivery -- the real AppSignals queues across threads,
        # but these tests only need the QUIT -> app.quit edge to fire.
        def emit(self, *args):
            for slot in self._slots:
                slot(*args)

    class FakeAppSignals:
        def __init__(self):
            self.quit_requested = FakeSignal()
            self.show_requested = FakeSignal()
            self.save_completed = FakeSignal()
            self.save_failed = FakeSignal()
            self.toast_requested = FakeSignal()
            self.update_available = FakeSignal()
            self.theme_changed = FakeSignal()

    class FakeApp:
        def __init__(self):
            self.stylesheets = []
            fakes.app = self

        def quit(self):
            quit_event.set()

        def exec(self):
            quit_event.wait(10)

        def setStyleSheet(self, sheet):
            # Records instead of applying -- apply_settings' live theme flip
            # is asserted against what lands here.
            self.stylesheets.append(sheet)

        def topLevelWidgets(self):
            return []

    class FakeMainWindow:
        def __init__(self, **kwargs):
            fakes.on_apply = kwargs["on_apply"]

        def show(self):
            pass

        def select_tab(self, tab):
            pass

        def on_save_completed(self):
            pass

        def on_save_failed(self):
            pass

        def show_update_banner(self, *args):
            pass

        def deleteLater(self):
            pass

    class FakeIpcServer:
        def __init__(self, host="127.0.0.1", port=51525):
            self.port = port
            self.handlers = {}
            fakes.server = self

        def register(self, command, handler):
            self.handlers[command] = handler

        def start(self):
            calls.append("ipc_start")

        def stop(self):
            calls.append("ipc_stop")

    class FakeSession:
        def __init__(self, config, setup):
            calls.append("session_construct")

        def start(self):
            calls.append("session_start")

        def stop(self):
            calls.append("session_stop")

        def is_running(self):
            return True

        def gave_up_restarting(self):
            return False

    fake_setup = SimpleNamespace(
        encoder="fake-encoder",
        video_source=SimpleNamespace(kind="fake"),
        audio_source=None,
        ffmpeg_path="ffmpeg",
    )

    def fake_resolve_setup(cfg):
        # Records the bitrate it was called with so tests can tell whether
        # resolution ran against the NEW settings (a candidate config) or the
        # stale ones -- and fails like a real forced-encoder smoke-encode.
        calls.append(("resolve_setup", cfg.video_bitrate))
        if cfg.encoder_override == "bogus-encoder":
            raise cli.NoWorkingEncoderError("Forced encoder 'bogus-encoder' does not work (fake)")
        return fake_setup

    class FakeHotkeyListener:
        def __init__(self, combo, callback):
            self._combo = combo
            fakes.hotkey_events.append(("construct", combo))

        def start(self):
            fakes.hotkey_events.append(("start", self._combo))

        def stop(self):
            fakes.hotkey_events.append(("stop", self._combo))

    config = Config(
        ipc_port=0,
        hotkey_enabled=hotkey_enabled,
        tray_enabled=False,
        check_for_updates=False,
        buffer_dir=tmp_path / "buffer",
        clips_dir=tmp_path / "clips",
    )
    fakes.config = config

    existing_config_file = tmp_path / "config.json"
    existing_config_file.write_text("{}")  # so main() skips the first-run wizard

    monkeypatch.setattr(cli, "_configure_logging", lambda: tmp_path / "clipersal.log")
    monkeypatch.setattr(cli, "_ensure_qapplication", lambda: FakeApp())
    monkeypatch.setattr(cli, "AppSignals", FakeAppSignals)
    monkeypatch.setattr(cli, "_another_instance_running", lambda port: False)
    monkeypatch.setattr(cli, "config_from_args", lambda args: config)
    monkeypatch.setattr(cli, "_show_startup_error", lambda message: None)
    monkeypatch.setattr(cli.capture, "resolve_setup", fake_resolve_setup)
    monkeypatch.setattr(cli.capture, "SegmentedCapture", FakeSession)
    monkeypatch.setattr(cli.ipc, "IpcServer", FakeIpcServer)
    monkeypatch.setattr(cli.config_store, "save_overrides", lambda overrides: fakes.saved_overrides.append(overrides))
    monkeypatch.setattr(cli.config_store, "default_config_path", lambda: existing_config_file)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda clips_dir, days: [])
    monkeypatch.setattr(cli.autostart, "is_supported", lambda os_: False)
    monkeypatch.setattr(cli.hotkey_module, "is_supported", lambda os_, session_type: True)
    monkeypatch.setattr(cli.hotkey_module, "HotkeyListener", FakeHotkeyListener)

    import clipersal.main_window_qt as main_window_qt

    monkeypatch.setattr(main_window_qt, "MainWindow", FakeMainWindow)

    return fakes


def _start_main(fakes):
    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "QUIT" in fakes.server.handlers and fakes.on_apply is not None:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "QUIT" in fakes.server.handlers
    assert fakes.on_apply is not None
    return thread, result


def _stop_main(fakes, thread, result) -> None:
    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["rc"] == 0


def test_apply_settings_bogus_encoder_keeps_old_capture_running_and_untouched(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    # A forced encoder that fails its smoke-encode -- plus an unrelated live
    # field changed, to prove NOTHING gets mutated on the failure path.
    values = _settings_values(fakes.config, encoder_override="bogus-encoder", buffer_seconds=90)
    error = fakes.on_apply(values)

    assert error is not None and "Could not apply" in error
    # The old capture session was never stopped and no replacement built.
    assert fakes.calls.count("session_stop") == 0
    assert fakes.calls.count("session_construct") == 1
    # STATUS must honestly reflect a still-running capture.
    assert fakes.server.handlers["STATUS"]() == "RECORDING"
    # Config was left untouched -- including the live-apply buffer_seconds.
    assert fakes.config.encoder_override is None
    assert fakes.config.video_bitrate == "8M"
    assert fakes.config.buffer_seconds == 60
    # ...and nothing was persisted.
    assert fakes.saved_overrides == []

    # A second Save with the same fields diffs against the REAL (unmutated)
    # config, so it reports the same error instead of a hollow success.
    assert fakes.on_apply(values) == error

    _stop_main(fakes, thread, result)


def test_apply_settings_valid_capture_change_restarts_and_persists(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, video_bitrate="12M"))

    assert error is None
    assert fakes.config.video_bitrate == "12M"
    # Resolution ran against the NEW values on apply (startup used the old).
    assert ("resolve_setup", "8M") in fakes.calls
    assert ("resolve_setup", "12M") in fakes.calls
    # The old session was stopped and its replacement constructed + started.
    stop_idx = fakes.calls.index("session_stop")
    assert stop_idx < fakes.calls.index("session_construct", stop_idx)
    assert fakes.calls.count("session_start") == 2
    assert fakes.saved_overrides[0]["video_bitrate"] == "12M"

    _stop_main(fakes, thread, result)


def test_apply_settings_volume_change_is_capture_restart_class(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, desktop_volume=150))

    assert error is None
    assert fakes.config.desktop_volume == 150
    # Volumes are baked into the ffmpeg command, so a change re-resolves
    # against the candidate config and swaps the session -- same class as a
    # bitrate change. (The candidate only differed in volume, so the fake's
    # recorded bitrate is still "8M" on both resolves.)
    resolve_indices = [i for i, c in enumerate(fakes.calls) if c == ("resolve_setup", "8M")]
    assert len(resolve_indices) == 2  # startup + the apply
    # Resolve-first: the candidate resolution ran BEFORE the old session was
    # stopped, so a resolution failure would have left capture untouched.
    assert resolve_indices[1] < fakes.calls.index("session_stop")
    assert fakes.calls.count("session_construct") == 2
    assert fakes.calls.count("session_start") == 2
    assert fakes.saved_overrides[0]["desktop_volume"] == 150

    _stop_main(fakes, thread, result)


def test_apply_settings_unchanged_volumes_do_not_restart_capture(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    # Only a live-applied field changes; volumes (and every other
    # restart-class field) stay at their current values.
    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is None
    assert fakes.config.buffer_seconds == 90
    assert fakes.config.desktop_volume == 100
    assert fakes.config.mic_volume == 100
    # No re-resolution and no session swap -- just the startup resolve.
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 1
    assert fakes.calls.count("session_stop") == 0
    assert fakes.calls.count("session_construct") == 1
    assert fakes.saved_overrides[0]["desktop_volume"] == 100
    assert fakes.saved_overrides[0]["mic_volume"] == 100

    _stop_main(fakes, thread, result)


def test_apply_settings_invalid_hotkey_is_rejected_before_touching_anything(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=True)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, hotkey_combo="Press keys..."))

    assert error is not None and "Invalid hotkey combo" in error
    assert fakes.config.hotkey_combo == "<ctrl>+<alt>+r"
    assert fakes.saved_overrides == []
    # The old listener is still the only one that exists -- never stopped,
    # no construction of the garbage combo attempted.
    assert fakes.hotkey_events == [("construct", "<ctrl>+<alt>+r"), ("start", "<ctrl>+<alt>+r")]

    _stop_main(fakes, thread, result)


def test_rebind_hotkey_constructs_new_binding_before_stopping_old_listener(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=True)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, hotkey_combo="<ctrl>+<shift>+s"))

    assert error is None
    assert fakes.hotkey_events == [
        ("construct", "<ctrl>+<alt>+r"),  # startup binding
        ("start", "<ctrl>+<alt>+r"),
        ("construct", "<ctrl>+<shift>+s"),  # new binding is built AND started ...
        ("start", "<ctrl>+<shift>+s"),
        ("stop", "<ctrl>+<alt>+r"),  # ... before the old one is dropped
    ]
    assert fakes.saved_overrides[0]["hotkey_combo"] == "<ctrl>+<shift>+s"

    _stop_main(fakes, thread, result)


def test_main_applies_configured_theme_before_building_the_app_stylesheet(monkeypatch, tmp_path) -> None:
    # A dark-mode launch must never flash light: theme.apply_theme() has to
    # run BEFORE _ensure_qapplication() constructs the QApplication and its
    # global stylesheet. Both are faked to record order only -- the real
    # apply_theme would flip process-global token state for no benefit here.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.config.dark_mode = True

    order = []
    monkeypatch.setattr(cli.theme, "apply_theme", lambda dark: order.append(("apply_theme", dark)))
    monkeypatch.setattr(cli, "_ensure_qapplication", lambda: order.append("ensure_qapplication") or None)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "QUIT" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "QUIT" in fakes.server.handlers

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["rc"] == 0
    assert order[:2] == [("apply_theme", True), "ensure_qapplication"]


def test_apply_settings_dark_mode_is_live_applied_without_capture_restart(monkeypatch, tmp_path) -> None:
    from clipersal import theme

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    try:
        error = fakes.on_apply(_settings_values(fakes.config, dark_mode=True))

        assert error is None
        assert fakes.config.dark_mode is True
        # Live-mutated class (like buffer_seconds), NOT capture-restart class:
        # no re-resolution, no session swap -- the theme has nothing to do
        # with the ffmpeg command line.
        resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
        assert len(resolve_calls) == 1  # startup only
        assert fakes.calls.count("session_stop") == 0
        assert fakes.calls.count("session_construct") == 1
        # The tokens actually flipped and the theme_changed slot rebuilt the
        # app's stylesheet from them.
        assert theme.current_theme() == "dark"
        assert fakes.app is not None and fakes.app.stylesheets
        assert theme.DARK_TOKENS["BACKGROUND"] in fakes.app.stylesheets[-1]
        assert fakes.saved_overrides[0]["dark_mode"] is True
    finally:
        # apply_settings really ran apply_theme -- don't leak the dark
        # palette into the rest of the suite.
        theme.apply_theme(False)

    _stop_main(fakes, thread, result)


def test_apply_settings_unchanged_dark_mode_does_not_retheme(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is None
    assert fakes.config.dark_mode is False
    assert fakes.app.stylesheets == []

    _stop_main(fakes, thread, result)


def test_shutdown_stops_session_under_pause_lock(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    calls = fakes.calls

    # A functional Lock wrapper that records acquire/release into the same
    # event stream as the session fakes, so the shutdown ordering can be
    # asserted directly. main() creates pause_lock via threading.Lock, so it
    # gets the fake too (threading.Event's internal lock likewise -- those
    # events are balanced pairs that don't affect the backward scan below).
    real_lock_cls = threading.Lock

    class FakeLock:
        _next_index = 0

        def __init__(self):
            self._index = FakeLock._next_index
            FakeLock._next_index += 1
            self._real = real_lock_cls()

        def acquire(self, *args):
            calls.append(("lock_acquire", self._index))
            return self._real.acquire(*args)

        def release(self):
            calls.append(("lock_release", self._index))
            self._real.release()

        def locked(self):
            return self._real.locked()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc_info):
            self.release()
            return False

    monkeypatch.setattr(cli.threading, "Lock", FakeLock)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "QUIT" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "QUIT" in fakes.server.handlers

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["rc"] == 0

    # The final session stop must be enclosed in an acquire/release pair of
    # the SAME lock -- previously it ran bare, racing apply_settings' swap of
    # state.session and potentially orphaning a freshly started ffmpeg.
    stop_idx = len(calls) - 1 - calls[::-1].index("session_stop")
    enclosing = None
    for event in reversed(calls[:stop_idx]):
        if isinstance(event, tuple) and event[0] == "lock_acquire":
            enclosing = event[1]
            break
        if isinstance(event, tuple) and event[0] == "lock_release":
            break
    assert enclosing is not None
    assert ("lock_release", enclosing) in calls[stop_idx + 1 :]


def test_cleanup_temp_buffer_removes_auto_created_dir(tmp_path: Path) -> None:
    config = Config(clips_dir=tmp_path / "clips")  # no buffer_dir -> auto temp dir
    buffer_dir = config.buffer_dir
    (buffer_dir / "seg-leftover.ts").write_bytes(b"x")

    cli._cleanup_temp_buffer(config)

    assert not buffer_dir.exists()


def test_cleanup_temp_buffer_preserves_user_supplied_dir(tmp_path: Path) -> None:
    config = Config(clips_dir=tmp_path / "clips", buffer_dir=tmp_path / "buf")
    (config.buffer_dir / "seg.ts").write_bytes(b"x")

    cli._cleanup_temp_buffer(config)

    assert (config.buffer_dir / "seg.ts").exists()
