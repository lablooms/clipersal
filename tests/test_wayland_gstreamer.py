import io
import os
import subprocess
import time

import pytest

from clipersal.subprocess_utils import NO_WINDOW_KWARGS
from clipersal.wayland_gstreamer import (
    GStreamerFramePump,
    GStreamerNotFoundError,
    PipewirePluginMissingError,
    ensure_gstreamer,
)


def _which_gst(name: str):
    return f"/usr/bin/{name}"


def test_ensure_gstreamer_returns_path_when_probe_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", _which_gst)
    monkeypatch.setattr(
        "clipersal.wayland_gstreamer.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=b"", stderr=b""),
    )

    assert ensure_gstreamer() == "/usr/bin/gst-launch-1.0"


def test_ensure_gstreamer_missing_from_path_raises_not_found(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", lambda name: None)

    with pytest.raises(GStreamerNotFoundError):
        ensure_gstreamer()


def test_ensure_gstreamer_missing_inspect_raises_not_found(monkeypatch) -> None:
    def which(name: str):
        return None if name == "gst-inspect-1.0" else _which_gst(name)

    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", which)

    with pytest.raises(GStreamerNotFoundError):
        ensure_gstreamer()


def test_ensure_gstreamer_missing_pipewiresrc_raises_plugin_error_with_hint(monkeypatch) -> None:
    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", _which_gst)
    monkeypatch.setattr(
        "clipersal.wayland_gstreamer.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout=b"", stderr=b""),
    )

    with pytest.raises(PipewirePluginMissingError) as excinfo:
        ensure_gstreamer()

    assert "gstreamer1.0-pipewire" in str(excinfo.value)


def test_ensure_gstreamer_inspect_timeout_raises_plugin_error_with_hint(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gst-inspect-1.0", timeout=10)

    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", _which_gst)
    monkeypatch.setattr("clipersal.wayland_gstreamer.subprocess.run", fake_run)

    with pytest.raises(PipewirePluginMissingError) as excinfo:
        ensure_gstreamer()

    assert "gstreamer1.0-pipewire" in str(excinfo.value)


def test_ensure_gstreamer_inspect_oserror_raises_not_found(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise OSError("gst-inspect-1.0 vanished")

    monkeypatch.setattr("clipersal.wayland_gstreamer.shutil.which", _which_gst)
    monkeypatch.setattr("clipersal.wayland_gstreamer.subprocess.run", fake_run)

    with pytest.raises(GStreamerNotFoundError):
        ensure_gstreamer()


class _FakeGstProcess:
    """Stands in for subprocess.Popen running gst-launch-1.0 -- stdout is a
    finite BytesIO so the pump thread can be exercised without real GStreamer.
    """

    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None) -> int | None:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _install_fake_popen(monkeypatch, payload: bytes) -> dict:
    record: dict = {}

    def fake_popen(argv, **kwargs):
        record["argv"] = argv
        record["kwargs"] = kwargs
        record["proc"] = _FakeGstProcess(payload)
        return record["proc"]

    monkeypatch.setattr("clipersal.wayland_gstreamer.subprocess.Popen", fake_popen)
    return record


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pipeline_argv_has_fd_path_and_bgra_caps_in_order(monkeypatch) -> None:
    record = _install_fake_popen(monkeypatch, payload=b"")
    pump = GStreamerFramePump("/usr/bin/gst-launch-1.0", pipewire_fd=42, node_id=7, sink=io.BytesIO())

    pump.start()
    try:
        assert record["argv"] == [
            "/usr/bin/gst-launch-1.0",
            "-q",
            "pipewiresrc",
            "fd=42",
            "path=7",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=BGRA",
            "!",
            "fdsink",
        ]
    finally:
        pump._pipewire_fd = None  # fake fd: nothing real to close
        pump.stop()


def test_popen_passes_portal_fd_and_no_window_kwargs(monkeypatch) -> None:
    record = _install_fake_popen(monkeypatch, payload=b"")
    pump = GStreamerFramePump("gst-launch-1.0", pipewire_fd=42, node_id=7, sink=io.BytesIO())

    pump.start()
    try:
        assert record["kwargs"]["pass_fds"] == (42,)
        assert record["kwargs"]["stdout"] is subprocess.PIPE
        assert record["kwargs"]["stderr"] is subprocess.DEVNULL
        for key, value in NO_WINDOW_KWARGS.items():
            assert record["kwargs"][key] == value
    finally:
        pump._pipewire_fd = None  # fake fd: nothing real to close
        pump.stop()


def test_pump_copies_all_bytes_to_sink_and_reports_running(monkeypatch) -> None:
    payload = bytes(range(256)) * 4096  # 1 MiB, spans several pump chunks
    _install_fake_popen(monkeypatch, payload)
    sink = io.BytesIO()
    pump = GStreamerFramePump("gst-launch-1.0", pipewire_fd=42, node_id=7, sink=sink)

    assert not pump.is_running
    pump.start()
    try:
        assert pump.is_running
        assert _wait_for(lambda: sink.getvalue() == payload)
        assert sink.getvalue() == payload
    finally:
        pump._pipewire_fd = None  # fake fd: nothing real to close
        pump.stop()
    assert not pump.is_running


def test_stdout_eof_sets_exited_unexpectedly(monkeypatch) -> None:
    _install_fake_popen(monkeypatch, payload=b"a few frames")
    pump = GStreamerFramePump("gst-launch-1.0", pipewire_fd=42, node_id=7, sink=io.BytesIO())

    pump.start()
    try:
        assert _wait_for(lambda: pump.exited_unexpectedly)
    finally:
        pump._pipewire_fd = None  # fake fd: nothing real to close
        pump.stop()


def test_stop_is_idempotent_and_closes_the_portal_fd(monkeypatch) -> None:
    _install_fake_popen(monkeypatch, payload=b"")
    fd_read, fd_write = os.pipe()
    try:
        pump = GStreamerFramePump("gst-launch-1.0", pipewire_fd=fd_read, node_id=7, sink=io.BytesIO())

        pump.start()
        pump.stop()
        pump.stop()  # second stop must be a no-op, not an error

        # The pump owns the portal fd once constructed: stop() closes it.
        with pytest.raises(OSError):
            os.fstat(fd_read)
    finally:
        os.close(fd_write)
