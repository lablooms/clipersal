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
