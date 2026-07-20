"""Local IPC command server -- the single trigger boundary described in
ARCHITECTURE.md's "IPC / hotkey boundary" section.

A loopback-only (127.0.0.1) TCP socket carrying a trivial line protocol:
a client sends a command word, optionally followed by one argument (e.g.
"SAVE\\n" or "SAVE 30\\n" -- the argument is used for basic trim-before-save,
see concat.save_clip's trim_seconds), and gets back a single
"OK <result>\\n" or "ERROR <message>\\n" line. The hotkey listener, the
`clipersal-trigger` CLI script (used for the Wayland/DE-keybinding
fallback), and any later tray/UI code all go through this same server
rather than calling capture/concat internals directly -- that's what keeps
"how a save gets triggered" fully decoupled from the capture engine.

A loopback TCP socket (rather than a Windows named pipe + a Unix domain
socket, one per platform) is a deliberate simplification: it behaves
identically on Windows and Linux, needs no platform-specific code, and
binding to 127.0.0.1 keeps it unreachable from outside the machine.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 51525


class IpcServerBindError(RuntimeError):
    pass


def _one_line(text: object) -> str:
    """Collapse a possibly multi-line message into a single line. The protocol
    is one response per line and the client does a single readline(), so an
    exception carrying ffmpeg's stderr (ConcatFailedError) would otherwise
    arrive truncated at its first newline -- the client would report a bare
    "ERROR ffmpeg concat failed:" with the actual cause silently dropped.
    """
    return " | ".join(str(text).splitlines())


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        parts = text.split(maxsplit=1)
        command = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else None
        handler = self.server.handlers.get(command)
        if handler is None:
            self.wfile.write(f"ERROR unknown command {command!r}\n".encode("utf-8"))
            return
        try:
            result = handler(arg)
        except Exception as exc:  # noqa: BLE001 -- report to the client, don't crash the server
            log.warning("IPC handler for %s raised: %s", command, exc)
            self.wfile.write(f"ERROR {_one_line(exc)}\n".encode("utf-8"))
            return
        line = f"OK {_one_line(result)}\n" if result else "OK\n"
        self.wfile.write(line.encode("utf-8"))


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        if sys.platform == "win32":
            # SO_REUSEADDR (what allow_reuse_address makes socketserver set in
            # super().server_bind()) does not mean on Windows what it means on
            # POSIX: there it lets a second socket bind+listen on a port that
            # is already actively listened on, silently splitting which
            # process receives connections. That voids the "second clipersal
            # instance fails to bind and exits cleanly" single-instance
            # backstop (see ARCHITECTURE.md's single-instance section), so
            # Windows gets SO_EXCLUSIVEADDRUSE instead -- exclusive ownership
            # of the port -- and allow_reuse_address is turned off so
            # SO_REUSEADDR can't weaken that exclusivity.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.allow_reuse_address = False
        super().server_bind()


class IpcServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._handlers: dict[str, Callable[[str | None], str | None]] = {}
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def register(self, command: str, handler: Callable[[str | None], str | None]) -> None:
        """handler receives the (optional) argument after the command word,
        or None if none was sent -- e.g. registering "SAVE" gets called with
        arg="30" for a client line of "SAVE 30", or arg=None for plain "SAVE".
        """
        self._handlers[command.upper()] = handler

    def start(self) -> None:
        try:
            self._server = _Server((self.host, self.port), _Handler)
        except OSError as exc:
            raise IpcServerBindError(
                f"Could not bind IPC socket on {self.host}:{self.port}: {exc}. "
                "Another clipersal instance may already be running."
            ) from exc
        self._server.handlers = self._handlers
        # port may have been 0 (pick any free port, mainly useful for tests);
        # reflect back whatever the OS actually bound.
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("IPC server listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
