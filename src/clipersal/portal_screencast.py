"""Wayland screen capture via the xdg-desktop-portal ScreenCast API.

On Wayland there is no X11-style "grab the root window": an app must ask the
xdg-desktop-portal frontend (D-Bus name ``org.freedesktop.portal.Desktop``)
for a ScreenCast session, the desktop's portal backend shows the system
share-dialog, and on approval the app receives a PipeWire node id plus a
PipeWire remote file descriptor. The capture layer then feeds raw frames
from that fd into ffmpeg over stdin -- no released ffmpeg has a native
PipeWire input device (the pipewiregrab patch was never merged), so the
fd/stdin route is the only option.

This module is a synchronous facade over that D-Bus conversation. The app is
not asyncio, so all blocking portal traffic runs on one dedicated worker
thread (jeepney.io.blocking): the thread performs the whole
CreateSession -> SelectSources -> Start -> OpenPipeWireRemote chain, reports
the outcome through a queue, and then stays alive as the session's
Closed-signal listener. Keeping every ``receive()`` on a single thread is
deliberate: jeepney's blocking connection has one shared message parser, and
concurrent receives would corrupt it. (Sending from another thread -- what
close() does with its fire-and-forget Session.Close -- is safe.)

Linux-only at runtime, but the module imports cleanly on Windows: jeepney is
pure Python, and the session bus is only touched when
open_screencast_session() actually runs.

Quirks of the portal spec (the org.freedesktop.portal.ScreenCast / .Request /
.Session documentation) that shape the code below:

- The Response signal for a request may be emitted BEFORE the method call
  that created it returns, so callers must predict the request object path
  and subscribe before sending the call.
- CreateSession's Response carries ``session_handle`` typed as a STRING
  variant, even though it is used as an object path everywhere afterwards.
- ``restore_token`` values are single-use and rotate: every successful Start
  hands back a fresh token that must replace the stored one, or the next
  launch silently falls back to showing the dialog again.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jeepney import DBusAddress, HeaderFields, MatchRule, Message, MessageType, Properties, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

from clipersal import config_store

log = logging.getLogger(__name__)

_PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
_PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"
_SESSION_INTERFACE = "org.freedesktop.portal.Session"

_SCREENCAST = DBusAddress(_PORTAL_OBJECT_PATH, bus_name=_PORTAL_BUS_NAME, interface=_SCREENCAST_INTERFACE)

# org.freedesktop.portal.ScreenCast "types" option values.
_SOURCE_TYPES = {"monitor": 1, "window": 2}

_REPLY_TIMEOUT_SECONDS = 10.0  # any individual D-Bus method call
_REQUEST_TIMEOUT_SECONDS = 30.0  # CreateSession/SelectSources Responses (normally instant)
_LISTEN_POLL_SECONDS = 0.5  # Closed-listener wakeups; bounds close() latency
_CLOSE_JOIN_TIMEOUT_SECONDS = 2.0

# Response codes from the org.freedesktop.portal.Request docs.
_RESPONSE_OK = 0
_RESPONSE_CANCELLED = 1

# org.freedesktop.portal.ScreenCast AvailableCursorModes bitmask values.
_CURSOR_HIDDEN = 1
_CURSOR_EMBEDDED = 2
_CURSOR_METADATA = 4

# persist_mode=2 ("persist until explicitly revoked") from the SelectSources
# docs -- this is what turns one approved selection into silent restores on
# later launches.
_PERSIST_UNTIL_REVOKED = 2


class PortalError(RuntimeError):
    """Base class for every failure open_screencast_session can raise."""


class PortalUnavailableError(PortalError):
    """No portal frontend/backend is reachable at all."""


class PortalCancelledError(PortalError):
    """The user denied or dismissed the system share-dialog (Response code 1)."""


class PortalBackendError(PortalError):
    """The portal answered, but wrongly: error replies, timeouts, Response
    code 2, a wedged backend, or messages that don't match the spec."""


def _unavailable_message(detail: str) -> str:
    # Actionable on purpose: "portal not found" is the failure a Wayland user
    # can actually fix, and the fix is installing the backend package.
    return (
        f"Wayland screen capture needs xdg-desktop-portal and a desktop portal backend, "
        f"but the ScreenCast service is not reachable ({detail}). Install xdg-desktop-portal "
        f"plus the backend for your desktop -- xdg-desktop-portal-gnome (GNOME) or "
        f"xdg-desktop-portal-kde (KDE Plasma) -- and log in again."
    )


@dataclass(frozen=True)
class PortalStream:
    """The single PipeWire stream a session was approved for (multiple=false
    means the portal always returns exactly one)."""

    node_id: int
    width: int
    height: int


_TOKEN_FILENAME = "wayland_portal.json"


class PortalTokenStore:
    """Persists the portal's restore_token between launches.

    Lives in its own tiny sibling file of config.json, NOT as a key inside
    it: config_store.PERSISTED_KEYS is an allowlist of Settings-window
    fields, and this token is an internal cache -- the same separation
    update_check.py uses for its cache file. Atomic .tmp-then-replace writes
    and corrupt-file tolerance also mirror that module, for the same reason:
    a half-written or mangled file must never cost more than "the share-
    dialog appears once more".
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_store.default_config_path().parent / _TOKEN_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> str | None:
        # Any problem at all (missing, unreadable, corrupt, wrong shape)
        # reads as "no token": restore failure is silent by design, the
        # portal just shows the dialog again. Never raise on a cache.
        if not self._path.exists():
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        token = data.get("restore_token")
        return token if isinstance(token, str) and token else None

    def save(self, token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-to-.tmp-then-replace(), the same pattern as
        # config_store.save_overrides -- a crash mid-write must not destroy
        # the previous (still usable) token.
        tmp_path = self._path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"restore_token": token}, f, indent=2)
        tmp_path.replace(self._path)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not remove portal token file %s: %s", self._path, exc)


class ScreenCastSession:
    """A live portal ScreenCast session: one PipeWire stream plus the fd of
    its PipeWire remote.

    Obtained from open_screencast_session(); not constructed directly.
    close() is idempotent -- the capture stop path and the crash path both
    call it, and a double-close must be a no-op, not an error (among other
    things, a second os.close() could close an unrelated fd that reused the
    number).
    """

    stream: PortalStream
    pipewire_fd: int
    on_closed: Callable[[], None] | None

    def __init__(self, connection: Any) -> None:
        self._conn = connection
        self._session_handle: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        # These three are filled in by the worker thread before
        # open_screencast_session() returns; never observed unset from
        # outside.
        self.stream = None
        self.pipewire_fd = None
        self.on_closed = None

    # -- worker thread: setup chain, then Closed listener -------------------

    def _run(self, source_type: str, token_store: PortalTokenStore, result_q: queue.Queue) -> None:
        try:
            self._negotiate(source_type, token_store)
        except BaseException as exc:  # noqa: BLE001 -- must reach the opener, whatever it is
            result_q.put((False, _as_portal_error(exc)))
            self.close()  # safe from this thread: close() never joins its caller
            return
        result_q.put((True, None))
        self._listen_loop()

    def _negotiate(self, source_type: str, token_store: PortalTokenStore) -> None:
        conn = self._conn
        version = _probe_version(conn)
        cursor_mode = _probe_cursor_mode(conn, version)
        # Below interface version 4 there is no persistence at all: don't
        # send persist options, don't touch the token store, and warn (once)
        # that the dialog will appear on every launch.
        stored_token = token_store.load() if version >= 4 else None
        if version < 4:
            _warn_no_persist_once(version)

        # Bus-level subscription for Response signals, BEFORE the first
        # request is sent -- the documented race (see module docstring) means
        # the first Response could already be on its way when CreateSession
        # returns. One rule covers every request this session makes.
        _add_match(
            conn,
            MatchRule(type="signal", sender=_PORTAL_BUS_NAME, interface=_REQUEST_INTERFACE, member="Response"),
        )

        self._session_handle = _create_session(conn)
        _add_match(
            conn,
            MatchRule(
                type="signal",
                sender=_PORTAL_BUS_NAME,
                interface=_SESSION_INTERFACE,
                member="Closed",
                path=self._session_handle,
            ),
        )

        _select_sources(conn, self._session_handle, source_type, version, cursor_mode, stored_token)
        self.stream, new_token = _start_session(conn, self._session_handle)
        if new_token:
            # restore_token is single-use and rotates on every Start -- save
            # the new one immediately, before anything else can fail and lose
            # it (the old token is already consumed at this point).
            token_store.save(new_token)
        self.pipewire_fd = _open_pipewire_remote(conn, self._session_handle)

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._conn.receive(timeout=_LISTEN_POLL_SECONDS)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 -- connection dropped or closed underneath us
                if not self._stop.is_set():
                    log.warning("Lost the D-Bus connection to xdg-desktop-portal", exc_info=True)
                return
            if self._is_session_closed(msg):
                log.info("Portal session was closed (screen sharing revoked from the desktop)")
                # The stop check keeps close()'s own Session.Close from
                # firing this: closing ourselves is not "the user revoked".
                callback = self.on_closed if not self._stop.is_set() else None
                if callback is not None:
                    try:
                        callback()
                    except Exception:  # noqa: BLE001 -- a callback must not kill the listener
                        log.exception("on_closed callback raised")
                return
            # Anything else (late method returns, stray signals) is dropped.

    def _is_session_closed(self, msg: Message) -> bool:
        if msg.header.message_type is not MessageType.signal:
            return False
        fields = msg.header.fields
        # No sender check: received signals carry the portal's UNIQUE name
        # (:1.x), not the well-known one -- only the daemon-side AddMatch can
        # filter on the well-known name.
        return (
            fields.get(HeaderFields.path) == self._session_handle
            and fields.get(HeaderFields.interface) == _SESSION_INTERFACE
            and fields.get(HeaderFields.member) == "Closed"
        )

    # -- shutdown -------------------------------------------------------------

    def close(self) -> None:
        """Close the portal session, stop the listener thread, and close the
        PipeWire fd. Idempotent and safe to call from any thread, including
        from inside the on_closed callback.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # Stop first: the listener must see _stop set before our own Close
        # can echo back as a Closed signal, so on_closed never fires for a
        # deliberate shutdown.
        self._stop.set()
        if self._session_handle is not None:
            try:
                # Fire-and-forget send: waiting for the reply here would mean
                # a second thread calling receive() on the connection, which
                # races the listener thread's receive (see module docstring).
                self._conn.send(_session_close_call(self._session_handle))
            except Exception:  # noqa: BLE001 -- closing is best-effort, never raise
                log.debug("Could not send Session.Close to the portal", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=_CLOSE_JOIN_TIMEOUT_SECONDS)
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            log.debug("Error closing the portal D-Bus connection", exc_info=True)
        fd, self.pipewire_fd = self.pipewire_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def open_screencast_session(
    source_type: str = "monitor",
    *,
    token_store: PortalTokenStore | None = None,
    connection_factory=None,
) -> ScreenCastSession:
    """Open a ScreenCast portal session, blocking until it is ready.

    This blocks for as long as the user takes in the system share-dialog
    (unbounded by design -- the Response only exists once the user acts), so
    call it from a worker thread, not the GUI thread. Raises
    PortalCancelledError if the user dismisses the dialog,
    PortalUnavailableError if no portal is reachable, PortalBackendError for
    every other portal-side failure.

    connection_factory is the test seam: a callable returning a jeepney
    blocking connection. The default opens the real session bus.
    """
    if source_type not in _SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(_SOURCE_TYPES)}, not {source_type!r}")
    token_store = token_store if token_store is not None else PortalTokenStore()
    factory = connection_factory or _default_connection_factory
    try:
        conn = factory()
    except PortalError:
        raise
    except Exception as exc:  # noqa: BLE001 -- no bus / auth failed / fds unsupported
        raise PortalUnavailableError(_unavailable_message(f"no usable session bus: {exc!r}")) from exc

    session = ScreenCastSession(conn)
    result_q: queue.Queue = queue.Queue()
    thread = threading.Thread(
        target=session._run,
        args=(source_type, token_store, result_q),
        name="clipersal-portal",
        daemon=True,  # must not keep the process alive at exit
    )
    session._thread = thread
    thread.start()
    ok, payload = result_q.get()
    if not ok:
        raise payload
    log.info("Portal session open: %s", session.stream)
    return session


# -- the setup chain, step by step --------------------------------------------


def _probe_version(conn) -> int:
    try:
        version = _get_property(conn, "version")
    except DBusErrorResponse as exc:
        if exc.name in (
            # No frontend at all (name not owned, not activatable).
            "org.freedesktop.DBus.Error.ServiceUnknown",
            # Frontend is up but nothing provides ScreenCast -- i.e. no
            # backend (the frontend only exports interfaces a backend
            # implements), which is the same "install a backend" fix.
            "org.freedesktop.DBus.Error.UnknownInterface",
        ):
            raise PortalUnavailableError(_unavailable_message(exc.name)) from exc
        raise PortalBackendError(f"Could not read the ScreenCast portal version: {exc}") from exc
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PortalBackendError(f"Unparseable ScreenCast portal version: {version!r}")
    return version


def _probe_cursor_mode(conn, version: int) -> int | None:
    """The best cursor mode the backend offers, or None when cursor_mode
    can't be sent (interface version < 2).

    Embedded is preferred over metadata over hidden because the frames leave
    this process as plain rawvideo to ffmpeg -- there is no side channel to
    carry cursor metadata on, so embedding is the only mode where the cursor
    is actually visible in the replay.
    """
    if version < 2:
        return None
    try:
        modes = _get_property(conn, "AvailableCursorModes")
    except DBusErrorResponse:
        # The property exists at v2+ per the spec, but cursor mode is
        # negotiable -- degrade quietly rather than fail the session.
        return None
    if not isinstance(modes, int) or modes <= 0:
        return None
    for mode in (_CURSOR_EMBEDDED, _CURSOR_METADATA, _CURSOR_HIDDEN):
        if modes & mode:
            return mode
    return None


def _get_property(conn, name: str):
    call = Properties(_SCREENCAST).get(name)
    reply = conn.send_and_get_reply(call, timeout=_REPLY_TIMEOUT_SECONDS)
    body = unwrap_msg(reply)  # error reply -> DBusErrorResponse, handled by callers
    return _variant_value(body[0]) if body else None


def _create_session(conn) -> str:
    handle_token = _new_handle_token()
    options = {
        "handle_token": ("s", handle_token),
        "session_handle_token": ("s", _new_handle_token()),
    }
    call = new_method_call(_SCREENCAST, "CreateSession", "a{sv}", (options,))
    results = _send_request(conn, call, _request_path(conn.unique_name, handle_token), step="CreateSession")
    handle = _variant_value(results.get("session_handle"))
    # Spec quirk: session_handle arrives as a STRING variant even though it
    # is used as an object path from here on (sent back typed 'o').
    if not isinstance(handle, str) or not handle.startswith("/"):
        raise PortalBackendError(f"CreateSession returned no usable session_handle: {handle!r}")
    return handle


def _select_sources(conn, session_handle: str, source_type: str, version: int, cursor_mode, stored_token) -> None:
    handle_token = _new_handle_token()
    options: dict[str, Any] = {
        "handle_token": ("s", handle_token),
        "types": ("u", _SOURCE_TYPES[source_type]),
        "multiple": ("b", False),
    }
    if cursor_mode is not None:
        options["cursor_mode"] = ("u", cursor_mode)
    if version >= 4:
        options["persist_mode"] = ("u", _PERSIST_UNTIL_REVOKED)
        if stored_token:
            # A stale token failing is silent by design: the portal just
            # shows the dialog again, and Start hands back a fresh token.
            options["restore_token"] = ("s", stored_token)
    call = new_method_call(_SCREENCAST, "SelectSources", "oa{sv}", (session_handle, options))
    _send_request(conn, call, _request_path(conn.unique_name, handle_token), step="SelectSources")


def _start_session(conn, session_handle: str) -> tuple[PortalStream, str | None]:
    handle_token = _new_handle_token()
    call = new_method_call(_SCREENCAST, "Start", "osa{sv}", (session_handle, "", {"handle_token": ("s", handle_token)}))
    # No timeout: this is the step whose Response waits on the user in the
    # system dialog, which is unbounded by definition.
    results = _send_request(conn, call, _request_path(conn.unique_name, handle_token), step="Start", timeout=None)
    streams = _variant_value(results.get("streams"))
    if not streams:
        raise PortalBackendError("The portal approved the request but returned no streams")
    # Take the FIRST stream's node id and size -- multiple=false means there
    # is exactly one.
    node_id, props = streams[0]
    size = _variant_value(props.get("size")) if isinstance(props, dict) else None
    # size is (ii) when present; tolerate it missing (0x0 = let the PipeWire
    # side negotiate) rather than fail an otherwise-good session.
    width, height = (int(size[0]), int(size[1])) if size else (0, 0)
    token = _variant_value(results.get("restore_token"))
    new_token = token if isinstance(token, str) and token else None
    return PortalStream(node_id=int(node_id), width=width, height=height), new_token


def _open_pipewire_remote(conn, session_handle: str) -> int:
    call = new_method_call(_SCREENCAST, "OpenPipeWireRemote", "oa{sv}", (session_handle, {}))
    reply = conn.send_and_get_reply(call, timeout=_REPLY_TIMEOUT_SECONDS)
    body = unwrap_msg(reply)
    if not body:
        raise PortalBackendError("OpenPipeWireRemote returned no file descriptor")
    return _take_fd(body[0])


def _send_request(conn, call: Message, request_path: str, *, step: str, timeout: float | None = _REQUEST_TIMEOUT_SECONDS) -> dict:
    """Send a request-style portal call and wait for its Response signal.

    The portal may emit the Response BEFORE the method call returns (the
    documented race in the org.freedesktop.portal.Request docs), so an
    in-process filter is registered first: jeepney's send_and_get_reply
    routes any early signal into the filter's queue instead of dropping it,
    and recv_until_filtered picks it up afterwards. Works for both
    orderings.
    """
    # No sender in this rule: received signals carry the UNIQUE name, so
    # matching on the well-known name in-process would never hit. The path
    # (our unique name + a random token) is specific enough.
    rule = MatchRule(type="signal", interface=_REQUEST_INTERFACE, member="Response", path=request_path)
    with conn.filter(rule) as matches:
        reply = conn.send_and_get_reply(call, timeout=_REPLY_TIMEOUT_SECONDS)
        unwrap_msg(reply)
        try:
            response = conn.recv_until_filtered(matches, timeout=timeout)
        except TimeoutError as exc:
            raise PortalBackendError(f"Timed out waiting for the portal's {step} response") from exc
    return _response_results(response.body, step)


def _response_results(body, step: str) -> dict:
    try:
        code, results = body
    except (TypeError, ValueError) as exc:
        raise PortalBackendError(f"Malformed {step} Response body: {body!r}") from exc
    if code == _RESPONSE_CANCELLED:
        raise PortalCancelledError("Screen sharing was cancelled in the system dialog -- nothing is being captured.")
    if code != _RESPONSE_OK:
        raise PortalBackendError(f"The portal's {step} request failed with Response code {code}")
    if not isinstance(results, dict):
        raise PortalBackendError(f"Malformed {step} Response results: {results!r}")
    return results


# -- small helpers --------------------------------------------------------------


def _add_match(conn, rule: MatchRule) -> None:
    reply = conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=_REPLY_TIMEOUT_SECONDS)
    unwrap_msg(reply)


def _session_close_call(session_handle: str) -> Message:
    session = DBusAddress(session_handle, bus_name=_PORTAL_BUS_NAME, interface=_SESSION_INTERFACE)
    return new_method_call(session, "Close")


def _new_handle_token() -> str:
    # App-prefixed per the portal docs' recommendation; uuid hex keeps it
    # unique across sessions and requests.
    return f"clipersal_{uuid.uuid4().hex}"


def _request_path(unique_name: str, handle_token: str) -> str:
    # The documented request-path scheme: our unique bus name with the ':'
    # stripped and '.' replaced by '_' (':1.42' -> '1_42'), which is what
    # makes the Response path predictable before the call is sent.
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{_PORTAL_OBJECT_PATH}/request/{sender}/{handle_token}"


def _variant_value(value):
    """Unwrap a jeepney variant's (signature, value) tuple; pass plain
    values through. Real jeepney parses variants to 2-tuples; test fakes
    hand us the same shape.
    """
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def _take_fd(value) -> int:
    # Real jeepney wraps received fds in jeepney.fds.FileDescriptor, whose
    # ownership must be claimed with to_raw_fd() -- otherwise its __del__
    # closes the fd under us. Tests hand us a plain int.
    if hasattr(value, "to_raw_fd"):
        return value.to_raw_fd()
    return int(value)


def _as_portal_error(exc: BaseException) -> PortalError:
    """Map whatever the worker thread hit onto the public error types, so
    callers only ever see PortalError subclasses."""
    if isinstance(exc, PortalError):
        return exc
    if isinstance(exc, TimeoutError):
        return PortalBackendError("Timed out talking to xdg-desktop-portal -- the portal service appears to be wedged")
    wrapped = PortalBackendError(f"Unexpected failure talking to the portal: {exc!r}")
    wrapped.__cause__ = exc
    return wrapped


_warned_no_persist = False


def _warn_no_persist_once(version: int) -> None:
    global _warned_no_persist
    if _warned_no_persist:
        return
    _warned_no_persist = True
    log.warning(
        "ScreenCast portal version %s is too old for persisted screen-sharing approval "
        "(needs version 4): the system share-dialog will appear on every launch. "
        "Upgrade xdg-desktop-portal and its backend.",
        version,
    )


def _session_bus_address() -> str:
    # jeepney's own find_session_bus() only reads DBUS_SESSION_BUS_ADDRESS
    # and has no fallback; the de-facto standard socket location (used by
    # libdbus, sd-bus, dbus-next) is /run/user/<uid>/bus, which covers
    # sessions where the env var never got exported.
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        return addr
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # not a Unix -- this path is Linux-only at runtime
        raise PortalUnavailableError(_unavailable_message("no DBUS_SESSION_BUS_ADDRESS"))
    return f"unix:path=/run/user/{getuid()}/bus"


def _default_connection_factory():
    # Imported here, not at module top: this is the one place that touches
    # the real session bus, and it must only happen at call time (Linux,
    # Wayland) -- never at import on Windows. enable_fds=True is required
    # for OpenPipeWireRemote's 'h' reply (SCM_RIGHTS fd passing); jeepney
    # negotiates it during the auth handshake.
    from jeepney.io.blocking import open_dbus_connection

    return open_dbus_connection(_session_bus_address(), enable_fds=True)
