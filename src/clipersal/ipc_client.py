"""Client side of the local IPC command server (see ipc.py).

Used by the hotkey listener's callback and by the `clipersal-trigger`
CLI script -- both just send a command line and print/act on the response,
with no knowledge of capture/concat internals.
"""

from __future__ import annotations

import socket

from clipersal.ipc import DEFAULT_HOST, DEFAULT_PORT


class IpcClientError(RuntimeError):
    pass


# SAVE gets a much longer leash than every other command: the server-side
# remux (concat.py's _CONCAT_TIMEOUT) may legitimately run up to 60s, and a
# client that gives up at the 5s default reports failure while the save
# actually completes. 70s keeps the client comfortably above the server's own
# timeout, so a genuine server-side error -- not a client timeout -- is what
# comes back when a save fails.
SAVE_TIMEOUT = 70.0


def send_command(
    command: str,
    arg: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> str:
    """Send a single command (optionally with one argument, e.g. command="SAVE",
    arg="30" for a trimmed save) to a running clipersal's IPC server and
    return its response line (without the trailing newline).
    """
    line = f"{command} {arg}" if arg is not None else command
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(f"{line}\n".encode("utf-8"))
            # errors="replace": a foreign process answering on the port with
            # non-UTF-8 bytes must not raise UnicodeDecodeError out of here --
            # cli.py's _another_instance_running only catches IpcClientError,
            # so a decode error would crash startup. The garbled replacement
            # line simply isn't an "OK ..." and the port reads as not-ours.
            response = sock.makefile("r", encoding="utf-8", errors="replace").readline()
    except OSError as exc:
        raise IpcClientError(
            f"Could not reach clipersal's IPC server at {host}:{port}: {exc}. Is clipersal running?"
        ) from exc

    response = response.strip()
    if not response:
        raise IpcClientError("Empty response from clipersal's IPC server")
    return response
