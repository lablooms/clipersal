import pytest

from clipersal import trigger
from clipersal.ipc import IpcServer


@pytest.fixture
def running_server():
    server = IpcServer(port=0)
    server.start()
    yield server
    server.stop()


def test_trigger_save_prints_response_and_exits_zero(running_server: IpcServer, capsys) -> None:
    running_server.register("SAVE", lambda arg: "/clips/clip-1.mp4")

    exit_code = trigger.main(["save", "--port", str(running_server.port)])

    assert exit_code == 0
    assert "/clips/clip-1.mp4" in capsys.readouterr().out


def test_trigger_reports_error_and_exits_nonzero_when_unreachable(capsys) -> None:
    exit_code = trigger.main(["ping", "--port", "1"])

    assert exit_code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_trigger_exits_nonzero_on_handler_error(running_server: IpcServer, capsys) -> None:
    def boom(arg):
        raise RuntimeError("not enough captured yet")

    running_server.register("SAVE", boom)

    exit_code = trigger.main(["save", "--port", str(running_server.port)])

    assert exit_code == 1
    assert "not enough captured yet" in capsys.readouterr().out


def test_trigger_save_with_trim_forwards_seconds_as_argument(running_server: IpcServer, capsys) -> None:
    received_args = []

    def handle_save(arg):
        received_args.append(arg)
        return "/clips/clip-trimmed.mp4"

    running_server.register("SAVE", handle_save)

    exit_code = trigger.main(["save", "--trim", "30", "--port", str(running_server.port)])

    assert exit_code == 0
    assert received_args == ["30.0"]
    assert "/clips/clip-trimmed.mp4" in capsys.readouterr().out


def test_trigger_rejects_trim_with_non_save_command(capsys) -> None:
    exit_code = trigger.main(["pause", "--trim", "30"])

    assert exit_code == 1
    assert "--trim is only valid with the 'save' command" in capsys.readouterr().err


def test_trigger_gallery_prints_response_and_exits_zero(running_server: IpcServer, capsys) -> None:
    running_server.register("GALLERY", lambda arg: "opening gallery window")

    exit_code = trigger.main(["gallery", "--port", str(running_server.port)])

    assert exit_code == 0
    assert "opening gallery window" in capsys.readouterr().out


def test_trigger_stats_prints_payload_and_exits_zero(running_server: IpcServer, capsys) -> None:
    running_server.register("STATS", lambda arg: "state=RECORDING|uptime=1.0")

    exit_code = trigger.main(["stats", "--port", str(running_server.port)])

    assert exit_code == 0
    assert "state=RECORDING|uptime=1.0" in capsys.readouterr().out


def test_trigger_screenshot_prints_path_and_exits_zero(running_server: IpcServer, capsys) -> None:
    received_args = []

    def handle_screenshot(arg):
        received_args.append(arg)
        return "/clips/screenshot-1.png"

    running_server.register("SCREENSHOT", handle_screenshot)

    exit_code = trigger.main(["screenshot", "--port", str(running_server.port)])

    assert exit_code == 0
    assert received_args == [None]  # no argument, unlike SAVE's trim
    assert "/clips/screenshot-1.png" in capsys.readouterr().out
