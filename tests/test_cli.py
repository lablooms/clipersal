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

        def uptime_seconds(self):
            return 123.456

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
        clips_max_gb=0,
        theme_mode="system",
        capture_mode="desktop",
        window_title="",
    )
    fakes.config = fake_config

    monkeypatch.setattr(cli, "_configure_logging", lambda: tmp_path / "clipersal.log")
    monkeypatch.setattr(cli, "_ensure_qapplication", lambda: None)
    monkeypatch.setattr(cli, "_another_instance_running", lambda port: False)
    monkeypatch.setattr(cli, "config_from_args", lambda args: fake_config)
    monkeypatch.setattr(cli, "_show_startup_error", startup_errors.append)
    # Never a real registry/gsettings probe -- deterministic "system" theme resolution.
    monkeypatch.setattr(cli.platform_detect, "system_dark_preferred", lambda os_: False)
    # Never a real ctypes/xprop probe -- deterministic {window} resolution.
    monkeypatch.setattr(cli.window_capture, "active_window_title", lambda *a, **k: None)
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

    def fake_save_clip(ffmpeg_path, buffer_dir, clips_dir_, filename_template, trim_seconds, window_title=None):
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
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda clips_dir, days, protected=None: [])

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
        "theme_mode": config.theme_mode,
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
            self.screenshot_saved = FakeSignal()
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
        def __init__(self, combo, callback=None):
            # Accepts either the single-combo form (combo, callback) or a
            # {combo: callback} mapping, mirroring the real HotkeyListener.
            self._mapping = dict(combo) if isinstance(combo, dict) else {combo: callback}
            fakes.hotkey_events.append(("construct", tuple(self._mapping)))

        @classmethod
        def from_mapping(cls, mapping):
            return cls(dict(mapping))

        def start(self):
            fakes.hotkey_events.append(("start", tuple(self._mapping)))

        def stop(self):
            fakes.hotkey_events.append(("stop", tuple(self._mapping)))

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
    # Never a real registry/gsettings probe -- deterministic "system" theme resolution.
    monkeypatch.setattr(cli.platform_detect, "system_dark_preferred", lambda os_: False)
    # Never a real ctypes/xprop probe -- deterministic {window} resolution.
    monkeypatch.setattr(cli.window_capture, "active_window_title", lambda *a, **k: None)
    monkeypatch.setattr(cli.config_store, "default_config_path", lambda: existing_config_file)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda clips_dir, days, protected=None: [])
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


def test_ipc_handlers_answer_before_the_main_window_exists(monkeypatch, tmp_path) -> None:
    # The command handlers are registered before MainWindow is constructed,
    # and the hotkey listener starts inside that window -- so a command can
    # genuinely arrive while `main_window` is still an unassigned closure
    # cell. Reading it must produce the intended "main window unavailable"
    # RuntimeError, not a NameError from the unbound cell.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)

    early: dict = {}

    class EarlyInvokeServer:
        """Invokes SHOW synchronously at registration time -- i.e. before
        cli.py gets anywhere near its MainWindow construction."""

        def __init__(self, host="127.0.0.1", port=51525):
            self.port = port
            self.handlers = {}
            fakes.server = self

        def register(self, command, handler):
            self.handlers[command] = handler
            if command == "SHOW":
                try:
                    handler()
                except Exception as exc:  # noqa: BLE001 -- capture it; registration must continue
                    early["SHOW"] = exc

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(cli.ipc, "IpcServer", EarlyInvokeServer)
    thread, result = _start_main(fakes)

    assert isinstance(early.get("SHOW"), RuntimeError)
    assert "main window unavailable" in str(early["SHOW"])

    _stop_main(fakes, thread, result)


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
    assert fakes.hotkey_events == [("construct", ("<ctrl>+<alt>+r",)), ("start", ("<ctrl>+<alt>+r",))]

    _stop_main(fakes, thread, result)


def test_apply_settings_failed_autostart_registration_blocks_the_whole_apply(monkeypatch, tmp_path) -> None:
    # The launch-on-startup registration is a pre-flight gate like every
    # other validation in apply_settings: when it fails, NOTHING else in the
    # payload may apply or persist either. It used to run after all other
    # fields applied and persisted, and the Settings tab's roll-everything-
    # back response to the error then silently reverted those actually-
    # applied settings on the next save -- a config/UI/disk divergence.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.autostart, "is_supported", lambda os_: True)

    def failing_enable(os_):
        raise OSError("access denied (fake)")

    monkeypatch.setattr(cli.autostart, "enable", failing_enable)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, launch_on_startup=True, buffer_seconds=90))

    assert error is not None and "launch-on-startup" in error
    assert fakes.config.launch_on_startup is False
    assert fakes.config.buffer_seconds == 60  # the unrelated change was blocked too
    assert fakes.saved_overrides == []

    _stop_main(fakes, thread, result)


def test_apply_settings_failed_autostart_registration_applies_and_persists_nothing(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    enable_calls = []

    def failing_enable(os_):
        enable_calls.append(os_)
        raise OSError("access denied (fake)")

    monkeypatch.setattr(cli.autostart, "is_supported", lambda os_: True)
    monkeypatch.setattr(cli.autostart, "enable", failing_enable)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, launch_on_startup=True))

    # The failure surfaces as an apply error -- no hollow "Saved ✓"
    # over a toggle that didn't take effect.
    assert error is not None and "launch-on-startup" in error
    # Nothing was mutated (the pre-flight gate fires before the mutation
    # block) and nothing persisted, so the Settings tab's roll-everything-back
    # response to an error string tells the truth: config, UI, and disk all
    # still hold the old values.
    assert fakes.config.launch_on_startup is False
    assert fakes.saved_overrides == []

    # Diffing against the REAL config means a second Save with the same
    # intent retries the registration instead of silently no-op'ing.
    assert fakes.on_apply(_settings_values(fakes.config, launch_on_startup=True)) == error
    assert len(enable_calls) == 2

    _stop_main(fakes, thread, result)


def test_apply_settings_successful_autostart_registration_persists_and_deregisters(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(cli.autostart, "is_supported", lambda os_: True)
    monkeypatch.setattr(cli.autostart, "enable", lambda os_: calls.append("enable"))
    monkeypatch.setattr(cli.autostart, "disable", lambda os_: calls.append("disable"))
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, launch_on_startup=True))
    assert error is None
    assert calls == ["enable"]
    assert fakes.config.launch_on_startup is True
    assert fakes.saved_overrides[-1]["launch_on_startup"] is True

    # Toggling back off deregisters, and a no-change Save after that touches
    # nothing -- config tracking reality is what makes the diff honest.
    assert fakes.on_apply(_settings_values(fakes.config, launch_on_startup=False)) is None
    assert calls == ["enable", "disable"]
    assert fakes.config.launch_on_startup is False
    assert fakes.on_apply(_settings_values(fakes.config, launch_on_startup=False)) is None
    assert calls == ["enable", "disable"]

    _stop_main(fakes, thread, result)


def test_apply_settings_failed_capture_restart_rolls_back_and_revives_old_session(monkeypatch, tmp_path) -> None:
    # The restart itself (not just resolve_setup) can still fail: the Wayland
    # portal handshake happens at session.start(), and its share dialog can
    # be cancelled there. The failure must come back as an error string with
    # the live config rolled back and the OLD capture running again -- never
    # an uncaught exception that leaves capture dead while STATUS lies
    # "RECORDING", the config mutated, and the disk stale.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)

    sessions = []

    class FlakySecondSession:
        def __init__(self, config, setup):
            sessions.append(self)
            self.start_attempts = 0
            self.stop_count = 0
            self._running = False
            self._fail_next_start = len(sessions) == 2  # the restart-session's first start fails

        def start(self):
            self.start_attempts += 1
            if self._fail_next_start:
                self._fail_next_start = False
                raise cli.PortalError("user cancelled the share dialog (fake)")
            self._running = True

        def stop(self):
            self.stop_count += 1
            self._running = False

        def is_running(self):
            return self._running

        def gave_up_restarting(self):
            return False

    monkeypatch.setattr(cli.capture, "SegmentedCapture", FlakySecondSession)
    thread, result = _start_main(fakes)

    # A temp-buffer config must keep its temp marker across the rollback --
    # otherwise shutdown would stop deleting the auto-created buffer dir.
    fakes.config.buffer_dir_is_temp = True

    # A capture-restart-class change (bitrate) plus a live-class one
    # (buffer): the rollback must cover both.
    error = fakes.on_apply(_settings_values(fakes.config, video_bitrate="12M", buffer_seconds=90))

    assert error is not None and "Could not apply capture settings" in error
    # The live config rolled all the way back...
    assert fakes.config.video_bitrate == "8M"
    assert fakes.config.buffer_seconds == 60
    assert fakes.config.buffer_dir_is_temp is True
    # ...nothing persisted...
    assert fakes.saved_overrides == []
    # ...and the OLD session was brought back: stopped once for the swap,
    # then started again by the rollback (its first start was main()'s).
    assert sessions[0].start_attempts == 2
    assert sessions[0].stop_count == 1
    assert sessions[0].is_running() is True
    assert fakes.server.handlers["STATUS"]() == "RECORDING"

    _stop_main(fakes, thread, result)


def test_apply_settings_capture_change_while_crashed_restarts_capture(monkeypatch, tmp_path) -> None:
    # Applying a capture-class change while CRASHED must bring capture back
    # with the new settings. Previously restart_capture only restarted when
    # the old process was still alive, so an apply after a give-up swapped
    # in a session that NEVER started -- while STATUS claimed RECORDING.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)

    sessions = []

    class CrashingSession:
        def __init__(self, config, setup):
            sessions.append(self)
            self.starts = 0
            self.stops = 0
            self._running = False
            self._gave_up = False

        def start(self):
            self.starts += 1
            self._running = True
            self._gave_up = False

        def stop(self):
            self.stops += 1
            self._running = False

        def is_running(self):
            return self._running

        def gave_up_restarting(self):
            return self._gave_up

    monkeypatch.setattr(cli.capture, "SegmentedCapture", CrashingSession)
    thread, result = _start_main(fakes)

    # Simulate the give-up: the startup session's ffmpeg is dead and the
    # restart budget is exhausted.
    sessions[0]._running = False
    sessions[0]._gave_up = True
    assert fakes.server.handlers["STATUS"]() == "CRASHED"

    error = fakes.on_apply(_settings_values(fakes.config, video_bitrate="12M"))

    assert error is None
    # The old session was stopped (resource teardown: the stale ffmpeg.log
    # handle the new start would otherwise trip over) and the NEW session
    # actually started -- capture is back with the new settings.
    assert sessions[0].stops == 1
    assert len(sessions) == 2
    assert sessions[1]._running is True
    assert fakes.server.handlers["STATUS"]() == "RECORDING"

    _stop_main(fakes, thread, result)


def test_apply_settings_persist_failure_reports_an_error_instead_of_raising(monkeypatch, tmp_path) -> None:
    # config_store.save_overrides can fail (a read-only config dir, a full
    # disk): apply_settings' contract with the Settings tab is "return None
    # or an error string", so the OSError must surface as the string, never
    # escape as an uncaught exception out of the autosave slot.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)

    def failing_save(overrides):
        raise OSError("disk full (fake)")

    monkeypatch.setattr(cli.config_store, "save_overrides", failing_save)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is not None and "Could not save settings" in error
    # The in-memory apply already happened (the Settings tab's rollback
    # covers the fields); only the disk write failed.
    assert fakes.config.buffer_seconds == 90

    _stop_main(fakes, thread, result)


def test_rebind_hotkey_constructs_new_binding_before_stopping_old_listener(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=True)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, hotkey_combo="<ctrl>+<shift>+s"))

    assert error is None
    assert fakes.hotkey_events == [
        ("construct", ("<ctrl>+<alt>+r",)),  # startup binding
        ("start", ("<ctrl>+<alt>+r",)),
        ("construct", ("<ctrl>+<shift>+s",)),  # new binding is built AND started ...
        ("start", ("<ctrl>+<shift>+s",)),
        ("stop", ("<ctrl>+<alt>+r",)),  # ... before the old one is dropped
    ]
    assert fakes.saved_overrides[0]["hotkey_combo"] == "<ctrl>+<shift>+s"

    _stop_main(fakes, thread, result)


def test_main_applies_configured_theme_before_building_the_app_stylesheet(monkeypatch, tmp_path) -> None:
    # A dark-mode launch must never flash light: theme.apply_theme() has to
    # run BEFORE _ensure_qapplication() constructs the QApplication and its
    # global stylesheet. Both are faked to record order only -- the real
    # apply_theme would flip process-global token state for no benefit here.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.config.theme_mode = "dark"

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


def test_main_system_theme_mode_follows_the_os_dark_setting(monkeypatch, tmp_path) -> None:
    # theme_mode="system" resolves through platform_detect at startup: a
    # dark-mode OS gets the dark palette applied before the app exists, with
    # no explicit light/dark pick anywhere. Same ordering assertion as the
    # forced-dark test above.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.platform_detect, "system_dark_preferred", lambda os_: True)

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


def test_apply_settings_theme_mode_is_live_applied_without_capture_restart(monkeypatch, tmp_path) -> None:
    from clipersal import theme

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    try:
        error = fakes.on_apply(_settings_values(fakes.config, theme_mode="dark"))

        assert error is None
        assert fakes.config.theme_mode == "dark"
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
        assert fakes.saved_overrides[0]["theme_mode"] == "dark"
        assert "dark_mode" not in fakes.saved_overrides[0]
    finally:
        # apply_settings really ran apply_theme -- don't leak the dark
        # palette into the rest of the suite.
        theme.apply_theme(False)

    _stop_main(fakes, thread, result)


def test_apply_settings_unchanged_theme_mode_does_not_retheme(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is None
    assert fakes.config.theme_mode == "system"
    assert fakes.app.stylesheets == []

    _stop_main(fakes, thread, result)


def test_apply_settings_system_mode_re_reads_the_os_dark_setting(monkeypatch, tmp_path) -> None:
    from clipersal import theme

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    # Forced-light config on a dark-mode OS: picking "system" must resolve
    # through the OS probe at apply time, not just flip a stored bool.
    fakes.config.theme_mode = "light"
    monkeypatch.setattr(cli.platform_detect, "system_dark_preferred", lambda os_: True)
    thread, result = _start_main(fakes)

    try:
        error = fakes.on_apply(_settings_values(fakes.config, theme_mode="system"))

        assert error is None
        assert fakes.config.theme_mode == "system"
        assert theme.current_theme() == "dark"
        assert fakes.app is not None and fakes.app.stylesheets
        assert theme.DARK_TOKENS["BACKGROUND"] in fakes.app.stylesheets[-1]
        assert fakes.saved_overrides[0]["theme_mode"] == "system"
    finally:
        theme.apply_theme(False)

    _stop_main(fakes, thread, result)


def test_apply_settings_invalid_theme_mode_is_rejected(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, theme_mode="blue"))

    # Only reachable via a hand-typed payload, but it must fail cleanly:
    # nothing mutated, nothing persisted, no retheme.
    assert error is not None
    assert "Invalid theme mode" in error
    assert fakes.config.theme_mode == "system"
    assert fakes.saved_overrides == []
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


# ---- STATS / SCREENSHOT IPC handlers -----------------------------------------


def test_stats_handler_reports_capture_buffer_and_clips(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "STATS" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "STATS" in fakes.server.handlers

    buffer_dir = fakes.config.buffer_dir
    buffer_dir.mkdir(parents=True, exist_ok=True)
    (buffer_dir / "seg-20260101-000000.ts").write_bytes(b"x" * 100)
    (buffer_dir / "seg-20260101-000002.ts").write_bytes(b"x" * 300)
    (buffer_dir / "ffmpeg.log").write_text("not a segment")  # must not be counted
    clips_dir = fakes.config.clips_dir
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "clip-1.mp4").write_bytes(b"clip")
    (clips_dir / "clip-2.mp4").write_bytes(b"clip")
    (clips_dir / "screenshot-1.png").write_bytes(b"shot")  # screenshots are not clips

    from clipersal.ipc_client import parse_stats_payload

    payload = fakes.server.handlers["STATS"]()
    fields = parse_stats_payload(payload)

    assert fields["state"] == "RECORDING"
    assert fields["uptime"] == "123.5"  # the fake session's 123.456, rounded to 1 decimal
    assert fields["segments"] == "2"
    assert fields["buffer_bytes"] == "400"
    assert fields["encoder"] == "fake-encoder"
    assert fields["buffer_seconds"] == "30"
    assert int(fields["clips_free_bytes"]) > 0
    assert fields["clips_count"] == "2"
    assert "\n" not in payload

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_stats_handler_degrades_failed_fields_to_empty_strings(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk gone (fake)")

    monkeypatch.setattr(cli.shutil, "disk_usage", boom)
    clips_dir = fakes.config.clips_dir
    real_glob = Path.glob

    def selective_glob(self, pattern):
        if self == clips_dir:
            raise OSError("clips dir gone (fake)")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", selective_glob)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "STATS" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "STATS" in fakes.server.handlers

    from clipersal.ipc_client import parse_stats_payload

    fields = parse_stats_payload(fakes.server.handlers["STATS"]())

    # The failed probes read as empty strings; the rest still report.
    assert fields["clips_free_bytes"] == ""
    assert fields["clips_count"] == ""
    assert fields["state"] == "RECORDING"
    assert fields["encoder"] == "fake-encoder"

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_screenshot_handler_calls_save_screenshot_and_returns_the_path(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    screenshot_calls = []
    expected = tmp_path / "clips" / "screenshot-1.png"

    def fake_save_screenshot(ffmpeg_path, buffer_dir, clips_dir):
        screenshot_calls.append((ffmpeg_path, buffer_dir, clips_dir))
        return expected

    monkeypatch.setattr(cli.screenshots, "save_screenshot", fake_save_screenshot)

    result = {}

    def run() -> None:
        result["rc"] = cli.main([])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if fakes.server is not None and "SCREENSHOT" in fakes.server.handlers:
            break
        time.sleep(0.01)
    assert fakes.server is not None and "SCREENSHOT" in fakes.server.handlers

    assert fakes.server.handlers["SCREENSHOT"]() == str(expected)
    assert screenshot_calls == [("ffmpeg", fakes.config.buffer_dir, fakes.config.clips_dir)]

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_screenshot_handler_emits_screenshot_saved_with_the_screenshot_toast(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    expected = tmp_path / "clips" / "screenshot-1.png"
    monkeypatch.setattr(cli.screenshots, "save_screenshot", lambda *a: expected)

    import clipersal.toast_qt as toast_qt

    toasts = []
    monkeypatch.setattr(
        toast_qt,
        "show_save_toast",
        lambda parent, ffmpeg_path, clip_path, cache_dir, title="Clip saved": toasts.append((clip_path, title)),
    )

    thread, result = _start_main(fakes)

    assert fakes.server.handlers["SCREENSHOT"]() == str(expected)
    # The FakeAppSignals fixture delivers synchronously, so the toast slot
    # has already run -- with the screenshot title, not "Clip saved".
    assert toasts == [(expected, "Screenshot saved")]

    _stop_main(fakes, thread, result)


# ---- apply_settings: framerate / resolution scale / extra hotkeys -------------


def test_apply_settings_framerate_change_is_capture_restart_class(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, framerate=60))

    assert error is None
    assert fakes.config.framerate == 60
    # The framerate is baked into the capture-source args, so this is the
    # same restart class as a bitrate change: re-resolved, session swapped.
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 2  # startup + the apply
    assert fakes.calls.count("session_construct") == 2
    assert fakes.calls.count("session_start") == 2
    assert fakes.saved_overrides[0]["framerate"] == 60

    _stop_main(fakes, thread, result)


def test_apply_settings_resolution_scale_change_is_capture_restart_class(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, resolution_scale="720p"))

    assert error is None
    assert fakes.config.resolution_scale == "720p"
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 2
    assert fakes.calls.count("session_construct") == 2
    assert fakes.saved_overrides[0]["resolution_scale"] == "720p"

    _stop_main(fakes, thread, result)


def test_apply_settings_17_key_payload_keeps_new_fields_at_defaults(monkeypatch, tmp_path) -> None:
    # The pre-quick-save Settings UI sends exactly the original 17 keys;
    # apply_settings must tolerate the new keys being absent entirely.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is None
    assert fakes.config.framerate == 30
    assert fakes.config.resolution_scale == "native"
    assert fakes.config.quick_save_hotkey_1 == ""
    assert fakes.config.screenshot_hotkey == ""
    # No capture restart (only a live field changed) and no hotkey rebind.
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 1
    assert fakes.hotkey_events == []
    # ...but the defaults are still persisted, so the file stays complete.
    assert fakes.saved_overrides[0]["framerate"] == 30
    assert fakes.saved_overrides[0]["resolution_scale"] == "native"
    assert fakes.saved_overrides[0]["quick_save_seconds_1"] == 30

    _stop_main(fakes, thread, result)


def test_apply_settings_rejects_duplicate_combos_case_insensitively(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, quick_save_hotkey_1="<CTRL>+<ALT>+R"))

    assert error is not None and "both" in error
    assert fakes.config.quick_save_hotkey_1 == ""
    assert fakes.saved_overrides == []
    # Rejected before anything was touched -- no persist, no rebind.
    assert fakes.hotkey_events == []

    _stop_main(fakes, thread, result)


def test_apply_settings_rejects_two_quick_saves_sharing_a_combo(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(
        _settings_values(fakes.config, quick_save_hotkey_1="<ctrl>+1", quick_save_hotkey_2="<ctrl>+1")
    )

    assert error is not None and "quick-save hotkey 1" in error and "quick-save hotkey 2" in error
    assert fakes.saved_overrides == []

    _stop_main(fakes, thread, result)


def test_apply_settings_rejects_invalid_quick_save_combo(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, quick_save_hotkey_1="garbage combo"))

    assert error is not None and "Invalid quick-save hotkey 1" in error
    assert fakes.config.quick_save_hotkey_1 == ""
    assert fakes.saved_overrides == []

    _stop_main(fakes, thread, result)


def test_apply_settings_clamps_out_of_range_quick_save_seconds(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, quick_save_seconds_1=10000, quick_save_seconds_2=1))

    assert error is None
    assert fakes.config.quick_save_seconds_1 == 300
    assert fakes.config.quick_save_seconds_2 == 5
    assert fakes.saved_overrides[0]["quick_save_seconds_1"] == 300
    assert fakes.saved_overrides[0]["quick_save_seconds_2"] == 5

    _stop_main(fakes, thread, result)


def test_rebind_hotkey_includes_exactly_the_configured_combos(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=True)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(
        _settings_values(
            fakes.config,
            quick_save_hotkey_1="<ctrl>+1",
            quick_save_seconds_1=45,
            screenshot_hotkey="<ctrl>+<f12>",
        )
    )

    assert error is None
    # Startup bound just the main combo; the rebind replaces it with the
    # full mapping -- main save, quick-save 1, screenshot (quick-save 2 is
    # empty = disabled and must not appear).
    assert fakes.hotkey_events == [
        ("construct", ("<ctrl>+<alt>+r",)),
        ("start", ("<ctrl>+<alt>+r",)),
        ("construct", ("<ctrl>+<alt>+r", "<ctrl>+1", "<ctrl>+<f12>")),
        ("start", ("<ctrl>+<alt>+r", "<ctrl>+1", "<ctrl>+<f12>")),
        ("stop", ("<ctrl>+<alt>+r",)),
    ]
    assert fakes.config.quick_save_seconds_1 == 45
    assert fakes.saved_overrides[0]["quick_save_hotkey_1"] == "<ctrl>+1"
    assert fakes.saved_overrides[0]["screenshot_hotkey"] == "<ctrl>+<f12>"

    _stop_main(fakes, thread, result)


def test_hotkey_callbacks_send_the_right_ipc_commands(monkeypatch, tmp_path) -> None:
    # The mapping's callbacks must go through the IPC client boundary:
    # main -> SAVE, quick-save -> SAVE <seconds>, screenshot -> SCREENSHOT.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path, hotkey_enabled=True)
    fakes.config.quick_save_hotkey_1 = "<ctrl>+1"
    fakes.config.quick_save_seconds_1 = 45
    fakes.config.screenshot_hotkey = "<ctrl>+<f12>"

    sent = []
    monkeypatch.setattr(
        cli.ipc_client, "send_command", lambda command, port, timeout=5.0: sent.append(command) or "OK done"
    )

    # Capture the mapping rebind_hotkey builds by intercepting from_mapping.
    mappings = []

    class RecordingListener:
        def __init__(self, mapping):
            self._mapping = mapping

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(
        cli.hotkey_module,
        "HotkeyListener",
        SimpleNamespace(from_mapping=lambda mapping: mappings.append(mapping) or RecordingListener(mapping)),
    )

    thread, result = _start_main(fakes)

    assert len(mappings) == 1
    mapping = mappings[0]
    assert set(mapping) == {"<ctrl>+<alt>+r", "<ctrl>+1", "<ctrl>+<f12>"}
    for callback in mapping.values():
        callback()
    assert sent == ["SAVE", "SAVE 45", "SCREENSHOT"]

    _stop_main(fakes, thread, result)


def test_trigger_command_via_ipc_gives_save_commands_the_save_timeout(monkeypatch) -> None:
    # A hotkey-triggered SAVE/SCREENSHOT goes through the same server-side
    # remux as a button save (concat's 60s ceiling), so the 5s default
    # reported slow-but-successful saves as failures in the log. They must
    # get the same SAVE_TIMEOUT leash the main window and tray use.
    sent = []
    monkeypatch.setattr(
        cli.ipc_client,
        "send_command",
        lambda command, port, timeout=5.0: sent.append((command, timeout)) or "OK done",
    )

    cli._trigger_command_via_ipc("SAVE", 51525)
    cli._trigger_command_via_ipc("SAVE 30", 51525)
    cli._trigger_command_via_ipc("SCREENSHOT", 51525)
    cli._trigger_command_via_ipc("STATUS", 51525)

    assert sent == [
        ("SAVE", cli.ipc_client.SAVE_TIMEOUT),
        ("SAVE 30", cli.ipc_client.SAVE_TIMEOUT),
        ("SCREENSHOT", cli.ipc_client.SAVE_TIMEOUT),
        ("STATUS", 5.0),
    ]


def test_save_handler_runs_size_cap_with_just_saved_clip_protected(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.config.clips_max_gb = 2
    clips_dir = fakes.config.clips_dir
    clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = clips_dir / "clip-test.mp4"

    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: saved_clip)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda *a, **k: [])
    monkeypatch.setattr(cli.clip_metadata, "favorites", lambda clips_dir_: {"favorite.mp4"})
    size_cap_calls = []
    monkeypatch.setattr(
        cli.concat,
        "enforce_size_cap",
        lambda clips_dir_, max_bytes, protected=None: size_cap_calls.append((clips_dir_, max_bytes, protected)) or [],
    )

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

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)

    # GiB -> bytes, and the just-saved clip is protected alongside the
    # favorites: a save must never delete the clip it just produced.
    assert size_cap_calls == [(clips_dir, 2 * (1 << 30), {"favorite.mp4", "clip-test.mp4"})]

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_save_handler_skips_size_cap_when_unlimited(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)  # clips_max_gb = 0
    clips_dir = fakes.config.clips_dir
    clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = clips_dir / "clip-test.mp4"

    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: saved_clip)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda *a, **k: [])
    size_cap_calls = []
    monkeypatch.setattr(
        cli.concat, "enforce_size_cap", lambda *a, **k: size_cap_calls.append((a, k)) or []
    )

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

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)
    assert size_cap_calls == []

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_save_handler_passes_the_active_window_title_to_save_clip(monkeypatch, tmp_path) -> None:
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)  # capture_mode == "desktop"
    clips_dir = fakes.config.clips_dir
    clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = clips_dir / "clip-test.mp4"
    captured = {}
    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: captured.update(k) or saved_clip)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda *a, **k: [])
    # Outside window-capture mode the {window} placeholder names the clip
    # after whatever window has focus at save time.
    monkeypatch.setattr(cli.window_capture, "active_window_title", lambda os_, session_type: "Valorant")

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

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)
    assert captured["window_title"] == "Valorant"

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_save_handler_names_window_mode_clips_after_the_captured_window(monkeypatch, tmp_path) -> None:
    # In window-capture mode the captured window IS the subject, so its
    # configured title names the clip -- the foreground-window probe is not
    # consulted at all.
    fakes = _install_headless_startup_fakes(monkeypatch, tmp_path)
    fakes.config.capture_mode = "window"
    fakes.config.window_title = "My Captured App"
    clips_dir = fakes.config.clips_dir
    clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = clips_dir / "clip-test.mp4"
    captured = {}
    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: captured.update(k) or saved_clip)
    monkeypatch.setattr(cli.concat, "enforce_clip_retention", lambda *a, **k: [])

    def probe_must_not_run(*a, **k):
        raise AssertionError("active_window_title must not be probed in window-capture mode")

    monkeypatch.setattr(cli.window_capture, "active_window_title", probe_must_not_run)

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

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)
    assert captured["window_title"] == "My Captured App"

    fakes.server.handlers["QUIT"]()
    thread.join(timeout=10)
    assert result["rc"] == 0


def test_apply_settings_save_sound_is_live_apply_class(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, save_sound_enabled=True))

    assert error is None
    assert fakes.config.save_sound_enabled is True
    # Live-mutate class (like buffer_seconds): no re-resolution, no session
    # swap -- it doesn't touch the ffmpeg command line.
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 1
    assert fakes.calls.count("session_stop") == 0
    assert fakes.saved_overrides[0]["save_sound_enabled"] is True

    _stop_main(fakes, thread, result)


def test_apply_settings_clips_max_gb_live_applies_and_sweeps(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    size_cap_calls = []
    monkeypatch.setattr(
        cli.concat,
        "enforce_size_cap",
        lambda clips_dir, max_bytes, protected=None: size_cap_calls.append((clips_dir, max_bytes, protected)) or [],
    )
    monkeypatch.setattr(cli.clip_metadata, "favorites", lambda clips_dir: {"star.mp4"})
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, clips_max_gb=5))

    assert error is None
    assert fakes.config.clips_max_gb == 5
    # Live-mutate class: no re-resolution, no session swap.
    resolve_calls = [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resolve_setup"]
    assert len(resolve_calls) == 1
    assert fakes.calls.count("session_stop") == 0
    # Changing the cap sweeps immediately (same pattern as retention), with
    # the favorites protected.
    assert size_cap_calls == [(fakes.config.clips_dir, 5 * (1 << 30), {"star.mp4"})]
    assert fakes.saved_overrides[0]["clips_max_gb"] == 5

    _stop_main(fakes, thread, result)


def test_apply_settings_zero_or_negative_clips_max_gb_skips_sweep_and_clamps(monkeypatch, tmp_path) -> None:
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    size_cap_calls = []
    monkeypatch.setattr(
        cli.concat, "enforce_size_cap", lambda *a, **k: size_cap_calls.append((a, k)) or []
    )
    thread, result = _start_main(fakes)

    assert fakes.on_apply(_settings_values(fakes.config, clips_max_gb=0)) is None
    assert fakes.config.clips_max_gb == 0
    assert size_cap_calls == []

    # A negative cap (hand-edited config) clamps to 0 = unlimited rather
    # than failing the whole apply.
    assert fakes.on_apply(_settings_values(fakes.config, clips_max_gb=-3)) is None
    assert fakes.config.clips_max_gb == 0
    assert fakes.saved_overrides[-1]["clips_max_gb"] == 0
    assert size_cap_calls == []

    _stop_main(fakes, thread, result)


def test_apply_settings_17_key_payload_keeps_wave5_fields_at_defaults(monkeypatch, tmp_path) -> None:
    # Same tolerance as the wave-2 fields: a Settings payload without the
    # new keys must leave them at their current (default) values.
    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    thread, result = _start_main(fakes)

    error = fakes.on_apply(_settings_values(fakes.config, buffer_seconds=90))

    assert error is None
    assert fakes.config.clips_max_gb == 0
    assert fakes.config.save_sound_enabled is False
    # ...but the defaults are still persisted, so the file stays complete.
    assert fakes.saved_overrides[0]["clips_max_gb"] == 0
    assert fakes.saved_overrides[0]["save_sound_enabled"] is False

    _stop_main(fakes, thread, result)


# ---- save sound: QApplication.beep on the toast paths ----------------------------


def test_save_toast_plays_a_beep_when_save_sound_enabled(monkeypatch, tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    fakes.config.save_sound_enabled = True
    fakes.config.clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = fakes.config.clips_dir / "clip-test.mp4"
    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: saved_clip)

    import clipersal.toast_qt as toast_qt

    monkeypatch.setattr(toast_qt, "show_save_toast", lambda *a, **k: None)
    beeps = []
    monkeypatch.setattr(QApplication, "beep", staticmethod(lambda: beeps.append(True)))

    thread, result = _start_main(fakes)

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)
    # The FakeAppSignals fixture delivers synchronously, so the toast slot
    # (toast -> beep) has already run by the time SAVE returns.
    assert beeps == [True]

    _stop_main(fakes, thread, result)


def test_save_toast_does_not_beep_when_save_sound_disabled(monkeypatch, tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)  # save_sound_enabled = False
    fakes.config.clips_dir.mkdir(parents=True, exist_ok=True)
    saved_clip = fakes.config.clips_dir / "clip-test.mp4"
    monkeypatch.setattr(cli.concat, "save_clip", lambda *a, **k: saved_clip)

    import clipersal.toast_qt as toast_qt

    toasts = []
    monkeypatch.setattr(toast_qt, "show_save_toast", lambda *a, **k: toasts.append(a))
    beeps = []
    monkeypatch.setattr(QApplication, "beep", staticmethod(lambda: beeps.append(True)))

    thread, result = _start_main(fakes)

    assert fakes.server.handlers["SAVE"]() == str(saved_clip)
    # The toast still shows -- only the sound is gated by the setting.
    assert len(toasts) == 1
    assert beeps == []

    _stop_main(fakes, thread, result)


def test_screenshot_toast_also_beeps_when_save_sound_enabled(monkeypatch, tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    fakes = _install_apply_settings_fakes(monkeypatch, tmp_path)
    fakes.config.save_sound_enabled = True
    expected = tmp_path / "clips" / "screenshot-1.png"
    monkeypatch.setattr(cli.screenshots, "save_screenshot", lambda *a: expected)

    import clipersal.toast_qt as toast_qt

    monkeypatch.setattr(toast_qt, "show_save_toast", lambda *a, **k: None)
    beeps = []
    monkeypatch.setattr(QApplication, "beep", staticmethod(lambda: beeps.append(True)))

    thread, result = _start_main(fakes)

    assert fakes.server.handlers["SCREENSHOT"]() == str(expected)
    assert beeps == [True]

    _stop_main(fakes, thread, result)


# ---- WheelGuard: the app-level scroll-wheel guard on the shared QApplication ------


def test_ensure_qapplication_installs_the_wheel_guard() -> None:
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from clipersal.qt_widgets import WheelGuard

    app = cli._ensure_qapplication()
    assert app is not None
    assert isinstance(app._wheel_guard, WheelGuard)


def test_ensure_qapplication_does_not_stack_duplicate_wheel_guards() -> None:
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    first = cli._ensure_qapplication()._wheel_guard
    assert cli._ensure_qapplication()._wheel_guard is first


# ---- app identity: window icon + AppUserModelID on the shared QApplication ----


def test_configure_app_identity_sets_a_non_null_window_icon() -> None:
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = cli._ensure_qapplication()
    # Called directly (not via construction): the QApplication may predate
    # this test in a full-suite run, and the icon is only wired at
    # construction time.
    cli._configure_app_identity(app)
    assert app.windowIcon().isNull() is False
