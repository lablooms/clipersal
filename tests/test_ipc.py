import socket
import sys

import pytest

from clipersal.ipc import IpcServer, IpcServerBindError
from clipersal.ipc_client import IpcClientError, send_command


@pytest.fixture
def running_server():
    server = IpcServer(port=0)  # port=0 -> OS picks a free port, avoids test collisions
    server.start()
    yield server
    server.stop()


def test_ping_roundtrip(running_server: IpcServer) -> None:
    running_server.register("PING", lambda arg: "PONG")

    response = send_command("PING", port=running_server.port)

    assert response == "OK PONG"


def test_save_style_handler_returns_result(running_server: IpcServer) -> None:
    running_server.register("SAVE", lambda arg: "C:/clips/clip-123.mp4")

    response = send_command("SAVE", port=running_server.port)

    assert response == "OK C:/clips/clip-123.mp4"


def test_handler_with_no_return_value(running_server: IpcServer) -> None:
    calls = []
    running_server.register("QUIT", lambda arg: calls.append(1) or None)

    response = send_command("QUIT", port=running_server.port)

    assert response == "OK"
    assert calls == [1]


def test_unknown_command_returns_error(running_server: IpcServer) -> None:
    response = send_command("BOGUS", port=running_server.port)

    assert response.startswith("ERROR")


def test_handler_exception_returns_error_not_crash(running_server: IpcServer) -> None:
    def boom(arg) -> str:
        raise RuntimeError("buffer empty")

    running_server.register("SAVE", boom)

    response = send_command("SAVE", port=running_server.port)

    assert response.startswith("ERROR")
    assert "buffer empty" in response

    # Server must still be alive and answering after a handler raised.
    running_server.register("PING", lambda arg: "PONG")
    assert send_command("PING", port=running_server.port) == "OK PONG"


def test_command_is_case_insensitive(running_server: IpcServer) -> None:
    running_server.register("SAVE", lambda arg: "ok")

    assert send_command("save", port=running_server.port) == "OK ok"


def test_client_raises_when_nothing_listening() -> None:
    with pytest.raises(IpcClientError):
        send_command("PING", port=1)  # port 1 is a privileged port nothing binds to in tests


def test_handler_receives_argument(running_server: IpcServer) -> None:
    received = []
    running_server.register("SAVE", lambda arg: received.append(arg) or "ok")

    send_command("SAVE", arg="30", port=running_server.port)

    assert received == ["30"]


def test_handler_receives_none_when_no_argument_sent(running_server: IpcServer) -> None:
    received = []
    running_server.register("SAVE", lambda arg: received.append(arg) or "ok")

    send_command("SAVE", port=running_server.port)

    assert received == [None]


def test_argument_with_extra_whitespace_is_preserved_after_first_split(running_server: IpcServer) -> None:
    received = []
    running_server.register("ECHO", lambda arg: received.append(arg) or "ok")

    send_command("ECHO", arg="hello world", port=running_server.port)

    assert received == ["hello world"]


def _recording_setsockopt(monkeypatch) -> list:
    """Replace socket.socket.setsockopt with a recorder for the duration of a
    test and return the list of (level, optname, value) calls. The real
    setsockopt is skipped entirely -- a loopback test bind works fine with no
    options set, and this keeps the Windows-only SO_EXCLUSIVEADDRUSE value
    settable when these tests run on POSIX.
    """
    calls = []
    monkeypatch.setattr(
        socket.socket,
        "setsockopt",
        lambda self, level, optname, value: calls.append((level, optname, value)),
    )
    return calls


def test_windows_bind_uses_exclusive_addr_use_not_reuseaddr(monkeypatch) -> None:
    # sys.platform is pinned so the test exercises the Windows branch no
    # matter which OS the suite runs on.
    monkeypatch.setattr(sys, "platform", "win32")
    sentinel = -5  # SO_EXCLUSIVEADDRUSE's real value; forced into existence for POSIX runs
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", sentinel, raising=False)
    calls = _recording_setsockopt(monkeypatch)

    server = IpcServer(port=0)
    server.start()
    try:
        assert (socket.SOL_SOCKET, sentinel, 1) in calls
        assert all(optname != socket.SO_REUSEADDR for _, optname, _ in calls)
    finally:
        server.stop()


def test_posix_bind_keeps_reuseaddr(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    calls = _recording_setsockopt(monkeypatch)

    server = IpcServer(port=0)
    server.start()
    try:
        assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls
    finally:
        server.stop()


def test_second_bind_on_same_port_is_refused() -> None:
    # The single-instance backstop itself: while one server holds the port, a
    # second IpcServer must not be able to take it over. On Windows this only
    # holds because _Server sets SO_EXCLUSIVEADDRUSE -- plain SO_REUSEADDR
    # there would happily let this second bind succeed.
    first = IpcServer(port=0)
    first.start()
    try:
        second = IpcServer(port=first.port)
        with pytest.raises(IpcServerBindError):
            second.start()
    finally:
        first.stop()
