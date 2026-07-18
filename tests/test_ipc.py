import pytest

from clipersal.ipc import IpcServer
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
