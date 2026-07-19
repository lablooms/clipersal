import threading
import time
from types import SimpleNamespace

import clipersal.cli as cli
from clipersal import ipc
from clipersal.cli import _another_instance_running
from clipersal.ipc import IpcServer


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
    fakes = SimpleNamespace(server=None, fail_on_start=False, calls=calls, startup_errors=startup_errors)

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
        clips_dir=tmp_path / "clips",
    )

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
