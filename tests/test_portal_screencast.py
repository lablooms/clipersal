"""Tests for portal_screencast -- no real D-Bus bus, no real portal.

Everything is driven through the connection_factory seam with a scripted
_FakeConnection/_FakePortal pair. The fakes deal in real jeepney Message
objects (built with jeepney's own new_method_return/new_error/new_signal),
so the tests pin the exact destinations, interfaces, members, signatures and
option contents the module sends -- not just call counts. Signal routing
reuses jeepney's own MessageFilters/FilterHandle, so the filter machinery
the module relies on for the documented "Response before the method call
returns" race is the real thing, not a reimplementation.
"""

import collections
import itertools
import os
import queue
import threading
import time
from pathlib import Path

import pytest
from jeepney import DBusAddress, HeaderFields, new_error, new_method_return, new_signal
from jeepney.io.common import FilterHandle, MessageFilters

from clipersal import portal_screencast as ps

_UNIQUE_NAME = ":1.42"
# The unique name as it appears inside predicted request/session paths: ':'
# stripped, '.' -> '_'.
_SENDER_TOKEN = "1_42"
_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST = "org.freedesktop.portal.ScreenCast"
_WATCHDOG_SECONDS = 5.0


class _FakePortal:
    """Scripts the reply + signals a real xdg-desktop-portal produces for
    each method call, by inspecting the actual jeepney message it receives.
    """

    def __init__(
        self,
        *,
        version=4,
        cursor_modes=0b111,
        node_id=77,
        size=(1920, 1080),
        restore_token="token-new",
        cancel_start=False,
        service_unknown=False,
        pipewire_fd=None,
    ):
        self.version = version
        self.cursor_modes = cursor_modes
        self.node_id = node_id
        self.size = size
        self.restore_token = restore_token
        self.cancel_start = cancel_start
        self.service_unknown = service_unknown
        self.pipewire_fd = pipewire_fd
        # Recorders the tests assert against.
        self.match_rules = []
        self.create_session_options = None
        self.select_sources_options = None
        self.start_options = None
        self.session_path = None

    def _request_path(self, handle_token: str) -> str:
        return f"{_PORTAL_PATH}/request/{_SENDER_TOKEN}/{handle_token}"

    @staticmethod
    def _response_signal(request_path: str, code: int, results: dict):
        emitter = DBusAddress(request_path, interface="org.freedesktop.portal.Request")
        signal = new_signal(emitter, "Response", "ua{sv}", (code, results))
        # Real signals arrive with the sender's UNIQUE name, not the
        # well-known one -- which is why in-process filtering can't match on
        # the well-known name.
        signal.header.fields[HeaderFields.sender] = ":1.99"
        return signal

    def __call__(self, msg):
        """Returns (reply, [signals]) for the fake connection to deliver."""
        fields = msg.header.fields
        member = fields.get(HeaderFields.member)
        interface = fields.get(HeaderFields.interface)

        if member == "AddMatch":
            self.match_rules.append(msg.body[0])
            return new_method_return(msg), []

        if member == "Get" and interface == "org.freedesktop.DBus.Properties":
            if self.service_unknown:
                return (
                    new_error(
                        msg,
                        "org.freedesktop.DBus.Error.ServiceUnknown",
                        "s",
                        ("The name org.freedesktop.portal.Desktop is not activatable",),
                    ),
                    [],
                )
            prop = msg.body[1]
            if prop == "version":
                return new_method_return(msg, "v", (("u", self.version),)), []
            if prop == "AvailableCursorModes":
                return new_method_return(msg, "v", (("u", self.cursor_modes),)), []
            raise AssertionError(f"unexpected property Get: {prop!r}")

        if member == "CreateSession":
            options = msg.body[0]
            self.create_session_options = options
            session_token = options["session_handle_token"][1]
            self.session_path = f"{_PORTAL_PATH}/session/{_SENDER_TOKEN}/{session_token}"
            request_path = self._request_path(options["handle_token"][1])
            return (
                new_method_return(msg, "o", (request_path,)),
                [self._response_signal(request_path, 0, {"session_handle": ("s", self.session_path)})],
            )

        if member == "SelectSources":
            options = msg.body[1]
            self.select_sources_options = options
            request_path = self._request_path(options["handle_token"][1])
            return new_method_return(msg, "o", (request_path,)), [self._response_signal(request_path, 0, {})]

        if member == "Start":
            options = msg.body[2]
            self.start_options = options
            request_path = self._request_path(options["handle_token"][1])
            if self.cancel_start:
                return new_method_return(msg, "o", (request_path,)), [self._response_signal(request_path, 1, {})]
            results = {
                "streams": (
                    "a(ua{sv})",
                    [
                        (
                            self.node_id,
                            {
                                "position": ("(ii)", (0, 0)),
                                "size": ("(ii)", self.size),
                                "source_type": ("u", 1),
                            },
                        )
                    ],
                ),
            }
            if self.restore_token is not None:
                results["restore_token"] = ("s", self.restore_token)
            return new_method_return(msg, "o", (request_path,)), [self._response_signal(request_path, 0, results)]

        if member == "OpenPipeWireRemote":
            return new_method_return(msg, "h", (self.pipewire_fd,)), []

        raise AssertionError(f"unexpected portal call: {interface}.{member}")


class _FakeConnection:
    """A jeepney.io.blocking.DBusConnection stand-in driven by a scripted
    _FakePortal, mirroring the real class's surface and routing semantics
    (reply_serial matching, non-reply messages routed into filters).
    """

    def __init__(self, portal, *, signals_first=True, unique_name=_UNIQUE_NAME):
        self.unique_name = unique_name
        self.outgoing_serial = itertools.count(1)
        self._portal = portal
        # Delivery order of signals vs the method return: True exercises the
        # documented "Response may be emitted before the call returns" race,
        # False the normal ordering.
        self._signals_first = signals_first
        self._filters = MessageFilters()
        self._inbox = queue.Queue()
        self.sent = []
        self.closed = False

    # -- sending -------------------------------------------------------------

    def send(self, message, serial=None):
        self.sent.append(message)

    send_message = send

    def send_and_get_reply(self, message, *, timeout=None):
        self.sent.append(message)
        message.header.serial = next(self.outgoing_serial)
        reply, signals = self._portal(message)
        ordered = [*signals, reply] if self._signals_first else [reply, *signals]
        for item in ordered:
            if item is not None:
                self._inbox.put(item)
        # Same routing loop as jeepney's real send_and_get_reply: return the
        # message whose reply_serial matches ours, route everything else
        # into matching filters.
        while True:
            incoming = self.receive(timeout=timeout)
            if incoming.header.fields.get(HeaderFields.reply_serial) == message.header.serial:
                return incoming
            for handle in self._filters.matches(incoming):
                handle.queue.append(incoming)

    # -- receiving -------------------------------------------------------------

    def receive(self, *, timeout=None):
        if self.closed:
            raise ConnectionResetError("fake connection closed")
        # Watchdog: a real bus may legitimately block forever (the Start
        # dialog); a test must fail, not hang the suite.
        effective = _WATCHDOG_SECONDS if timeout is None else min(timeout, _WATCHDOG_SECONDS)
        try:
            return self._inbox.get(timeout=effective)
        except queue.Empty:
            raise TimeoutError

    def recv_messages(self, *, timeout=None):
        msg = self.receive(timeout=timeout)
        for handle in self._filters.matches(msg):
            handle.queue.append(msg)

    def recv_until_filtered(self, queue, *, timeout=None):
        while len(queue) == 0:
            self.recv_messages(timeout=timeout)
        return queue.popleft()

    def filter(self, rule, *, queue=None, bufsize=1):
        if queue is None:
            queue = collections.deque(maxlen=bufsize)
        return FilterHandle(self._filters, rule, queue)

    def close(self):
        self.closed = True

    # -- test helpers ----------------------------------------------------------

    def push_signal(self, message):
        """Deliver a signal 'from the bus' after setup has finished."""
        self._inbox.put(message)


def _make_fd(tmp_path: Path, name: str = "pipewire-remote") -> int:
    """A real fd, so session.close() provably closes it (os.fstat must fail
    afterwards). Stands in for the SCM_RIGHTS fd the portal would send.
    """
    return os.open(tmp_path / name, os.O_CREAT | os.O_RDWR)


def _open(portal, conn, tmp_path: Path, source_type="monitor"):
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    session = ps.open_screencast_session(source_type, token_store=store, connection_factory=lambda: conn)
    return session, store


def _closed_signal(session_path: str):
    emitter = DBusAddress(session_path, interface="org.freedesktop.portal.Session")
    signal = new_signal(emitter, "Closed", "a{sv}", ({},))
    signal.header.fields[HeaderFields.sender] = ":1.99"
    return signal


# ---- open_screencast_session: the setup chain ---------------------------------


def test_open_session_full_chain_produces_stream_fd_and_stores_rotated_token(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    store.save("token-old")
    fd = _make_fd(tmp_path)
    # signals_first=True: every Response arrives BEFORE its method return,
    # the documented race the subscribe-before-send logic exists for.
    portal = _FakePortal(pipewire_fd=fd)
    conn = _FakeConnection(portal)

    session = ps.open_screencast_session("monitor", token_store=store, connection_factory=lambda: conn)
    try:
        assert session.stream == ps.PortalStream(node_id=77, width=1920, height=1080)
        assert session.pipewire_fd == fd
        # The single-use token rotated: Start's fresh token replaced the old one.
        assert store.load() == "token-new"
        assert portal.session_path.startswith(f"{_PORTAL_PATH}/session/{_SENDER_TOKEN}/clipersal_")
    finally:
        session.close()

    # The full D-Bus conversation, in order, signatures included.
    chain = [
        (
            m.header.fields.get(HeaderFields.destination),
            m.header.fields.get(HeaderFields.interface),
            m.header.fields.get(HeaderFields.member),
            m.header.fields.get(HeaderFields.signature),
        )
        for m in conn.sent
    ]
    assert chain == [
        (_PORTAL_BUS, "org.freedesktop.DBus.Properties", "Get", "ss"),  # version probe
        (_PORTAL_BUS, "org.freedesktop.DBus.Properties", "Get", "ss"),  # AvailableCursorModes probe
        ("org.freedesktop.DBus", "org.freedesktop.DBus", "AddMatch", "s"),  # Response subscription
        (_PORTAL_BUS, _SCREENCAST, "CreateSession", "a{sv}"),
        ("org.freedesktop.DBus", "org.freedesktop.DBus", "AddMatch", "s"),  # Closed subscription
        (_PORTAL_BUS, _SCREENCAST, "SelectSources", "oa{sv}"),
        (_PORTAL_BUS, _SCREENCAST, "Start", "osa{sv}"),
        (_PORTAL_BUS, _SCREENCAST, "OpenPipeWireRemote", "oa{sv}"),
        (_PORTAL_BUS, "org.freedesktop.portal.Session", "Close", None),  # from session.close()
    ]
    assert conn.sent[0].body == (_SCREENCAST, "version")
    assert conn.sent[1].body == (_SCREENCAST, "AvailableCursorModes")

    create_options = portal.create_session_options
    assert create_options["handle_token"][0] == "s"
    assert create_options["handle_token"][1].startswith("clipersal_")
    assert create_options["session_handle_token"][1].startswith("clipersal_")

    select_options = portal.select_sources_options
    assert select_options["handle_token"][1].startswith("clipersal_")
    assert select_options["types"] == ("u", 1)
    assert select_options["multiple"] == ("b", False)
    assert select_options["cursor_mode"] == ("u", 2)  # embedded, the best of 0b111
    assert select_options["persist_mode"] == ("u", 2)
    assert select_options["restore_token"] == ("s", "token-old")

    start_call = conn.sent[6]
    assert start_call.body[0] == portal.session_path
    assert start_call.body[1] == ""
    assert portal.start_options["handle_token"][1].startswith("clipersal_")

    # Bus-level signal subscriptions carry the well-known sender name (the
    # daemon translates it); one for all Responses, one for this session's
    # Closed.
    assert any("org.freedesktop.portal.Request" in r and "Response" in r for r in portal.match_rules)
    assert any(
        "org.freedesktop.portal.Session" in r and "Closed" in r and portal.session_path in r
        for r in portal.match_rules
    )

    # close() released both the bus connection and the PipeWire fd.
    assert conn.closed
    with pytest.raises(OSError):
        os.fstat(fd)


def test_open_session_window_source_type_sends_types_2(tmp_path: Path) -> None:
    fd = _make_fd(tmp_path)
    portal = _FakePortal(pipewire_fd=fd)
    conn = _FakeConnection(portal)

    session, _store = _open(portal, conn, tmp_path, source_type="window")
    try:
        assert portal.select_sources_options["types"] == ("u", 2)
    finally:
        session.close()


def test_open_session_rotates_token_on_second_open(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")

    portal1 = _FakePortal(pipewire_fd=_make_fd(tmp_path, "fd1"), restore_token="token-1")
    conn1 = _FakeConnection(portal1)
    session1 = ps.open_screencast_session("monitor", token_store=store, connection_factory=lambda: conn1)
    try:
        # Nothing stored yet: persistence requested, but no token offered.
        assert portal1.select_sources_options["persist_mode"] == ("u", 2)
        assert "restore_token" not in portal1.select_sources_options
        assert store.load() == "token-1"
    finally:
        session1.close()

    portal2 = _FakePortal(pipewire_fd=_make_fd(tmp_path, "fd2"), restore_token="token-2")
    # signals_first=False: also pin the normal reply-before-signal ordering.
    conn2 = _FakeConnection(portal2, signals_first=False)
    session2 = ps.open_screencast_session("monitor", token_store=store, connection_factory=lambda: conn2)
    try:
        # The rotated token from the first session was offered back...
        assert portal2.select_sources_options["restore_token"] == ("s", "token-1")
        # ...and the second rotation replaced it again.
        assert store.load() == "token-2"
    finally:
        session2.close()


def test_open_session_user_cancel_raises_cancelled_and_cleans_up(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    portal = _FakePortal(cancel_start=True)  # Response code 1 from Start
    conn = _FakeConnection(portal)

    with pytest.raises(ps.PortalCancelledError):
        ps.open_screencast_session("monitor", token_store=store, connection_factory=lambda: conn)

    assert conn.closed  # a failed open must not leak the bus connection
    assert not store.path.exists()  # ...or invent a token


def test_open_session_without_portal_raises_unavailable_naming_backends(tmp_path: Path) -> None:
    portal = _FakePortal(service_unknown=True)
    conn = _FakeConnection(portal)

    with pytest.raises(ps.PortalUnavailableError) as excinfo:
        ps.open_screencast_session("monitor", token_store=ps.PortalTokenStore(tmp_path / "t.json"), connection_factory=lambda: conn)

    message = str(excinfo.value)
    assert "xdg-desktop-portal-gnome" in message
    assert "xdg-desktop-portal-kde" in message
    assert conn.closed


def test_open_session_factory_failure_raises_unavailable(tmp_path: Path) -> None:
    def factory():
        raise OSError("connect: /run/user/1000/bus: no such file or directory")

    with pytest.raises(ps.PortalUnavailableError):
        ps.open_screencast_session(
            "monitor", token_store=ps.PortalTokenStore(tmp_path / "t.json"), connection_factory=factory
        )


def test_open_session_version_below_4_sends_no_persist_options_and_touches_no_token(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    store.save("token-old")
    fd = _make_fd(tmp_path)
    portal = _FakePortal(version=3, restore_token=None, pipewire_fd=fd)
    conn = _FakeConnection(portal)

    session = ps.open_screencast_session("monitor", token_store=store, connection_factory=lambda: conn)
    try:
        options = portal.select_sources_options
        assert "persist_mode" not in options
        assert "restore_token" not in options  # even though one is stored
        assert options["cursor_mode"] == ("u", 2)  # v3 still supports cursor modes
        # The stored token is neither consumed nor rotated below v4.
        assert store.load() == "token-old"
    finally:
        session.close()


def test_open_session_rejects_unknown_source_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ps.open_screencast_session(
            "desktop", token_store=ps.PortalTokenStore(tmp_path / "t.json"), connection_factory=lambda: None
        )


# ---- ScreenCastSession lifecycle ------------------------------------------------


def test_close_is_idempotent_and_fires_no_callback(tmp_path: Path) -> None:
    fd = _make_fd(tmp_path)
    portal = _FakePortal(pipewire_fd=fd)
    conn = _FakeConnection(portal)
    session, _store = _open(portal, conn, tmp_path)

    fired = []
    session.on_closed = lambda: fired.append(True)
    session.close()
    session.close()  # double-close: the stop path and the crash path both call it

    assert fired == []
    assert conn.closed
    with pytest.raises(OSError):
        os.fstat(fd)
    close_calls = [m for m in conn.sent if m.header.fields.get(HeaderFields.member) == "Close"]
    assert len(close_calls) == 1  # Session.Close sent exactly once
    assert close_calls[0].header.fields[HeaderFields.path] == portal.session_path


def test_portal_closed_signal_invokes_on_closed(tmp_path: Path) -> None:
    fd = _make_fd(tmp_path)
    portal = _FakePortal(pipewire_fd=fd)
    conn = _FakeConnection(portal)
    session, _store = _open(portal, conn, tmp_path)

    fired = threading.Event()
    session.on_closed = fired.set
    try:
        # A Closed signal for a DIFFERENT session path must be ignored.
        conn.push_signal(_closed_signal(f"{_PORTAL_PATH}/session/{_SENDER_TOKEN}/someone_else"))
        time.sleep(0.3)
        assert not fired.is_set()

        conn.push_signal(_closed_signal(portal.session_path))
        assert fired.wait(5.0)
    finally:
        session.close()


# ---- PortalTokenStore -------------------------------------------------------------


def test_token_store_round_trip(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    assert store.load() is None
    store.save("token-1")
    assert store.load() == "token-1"
    store.save("token-2")  # rotation overwrites
    assert store.load() == "token-2"


def test_token_store_load_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "wayland_portal.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert ps.PortalTokenStore(path).load() is None


def test_token_store_load_returns_none_for_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "wayland_portal.json"
    path.write_text('["token-1"]', encoding="utf-8")

    assert ps.PortalTokenStore(path).load() is None


def test_token_store_clear_removes_the_file(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "wayland_portal.json")
    store.save("token-1")

    store.clear()

    assert store.load() is None
    store.clear()  # clearing twice is a no-op, not an error


def test_token_store_save_creates_parent_directories(tmp_path: Path) -> None:
    store = ps.PortalTokenStore(tmp_path / "nested" / "dir" / "wayland_portal.json")

    store.save("token-1")

    assert store.load() == "token-1"


# ---- bus address discovery ---------------------------------------------------------


def test_session_bus_address_prefers_env_var(monkeypatch) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/custom-bus")

    assert ps._session_bus_address() == "unix:path=/tmp/custom-bus"


def test_session_bus_address_falls_back_to_run_user_bus(monkeypatch) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    # os.getuid doesn't exist on Windows; jeepney's own find_session_bus has
    # no fallback at all, which is exactly why this helper exists.
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)

    assert ps._session_bus_address() == "unix:path=/run/user/1234/bus"
