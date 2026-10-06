"""Serve bounded HTTP CONNECT setup and copy admitted tunnel bytes."""

from __future__ import annotations

import selectors
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from .settings import Settings, normalize_host

_BUFFER_SIZE: Final = 64 * 1024
# exempt: setup heads are small; this bound stops unbounded request and parent headers.
_HEAD_LIMIT: Final = 8192
# exempt: setup must finish promptly, while established transfers have no idle bound.
_SETUP_TIMEOUT: Final = 5.0
# exempt: the accept poll lets a signal stop the first process promptly.
_LISTEN_TIMEOUT: Final = 0.2


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    """A validated request line and, for CONNECT, its DNS destination."""

    method: str
    target: str
    host: str | None = None
    port: int | None = None


class RequestError(ValueError):
    """A request refused with an HTTP status and no user-supplied text."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _read_head(sock: socket.socket, deadline: float) -> tuple[bytes, bytes]:
    """Read through CRLF CRLF, preserving any tunnel bytes received with it."""

    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("setup head timed out")
        sock.settimeout(remaining)
        chunk = sock.recv(_BUFFER_SIZE)
        if not chunk:
            raise ValueError("incomplete setup head")
        data.extend(chunk)
        end = data.find(b"\r\n\r\n")
        if end >= 0:
            if end + 4 > _HEAD_LIMIT:
                raise ValueError("setup head too large")
            return bytes(data[: end + 4]), bytes(data[end + 4 :])
        if len(data) > _HEAD_LIMIT:
            raise ValueError("setup head too large")


def parse_request(head: bytes) -> ParsedRequest:
    """Parse a complete HTTP head without reflecting its content in errors."""

    if not head.endswith(b"\r\n\r\n"):
        raise RequestError(400)
    lines = head[:-4].split(b"\r\n")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeError, ValueError) as exc:
        raise RequestError(400) from exc
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise RequestError(400)
    if any(
        not line or line[:1] in (b" ", b"\t") or b":" not in line
        for line in lines[1:]
    ):
        raise RequestError(400)
    if method == "GET" and target == "/healthz":
        return ParsedRequest(method, target)
    if method != "CONNECT":
        raise RequestError(405)
    host_text, separator, port_text = target.rpartition(":")
    if not separator or not port_text.isascii() or not port_text.isdecimal():
        raise RequestError(400)
    try:
        host = normalize_host(host_text)
    except ValueError as exc:
        raise RequestError(400) from exc
    try:
        port = int(port_text)
    except ValueError as exc:
        raise RequestError(400) from exc
    if not 1 <= port <= 65_535:
        raise RequestError(400)
    return ParsedRequest(method, target, host, port)


def _reply(client: socket.socket, status: int) -> None:
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 502: "Bad Gateway"}[status]
    with suppress(OSError):
        client.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )


def _close_write(sock: socket.socket) -> None:
    """Propagate a reader's EOF to the other socket's write side.

    A TLS parent's leg is left open: its own shutdown discards the TLS state, and
    a bare TCP half-close is an unexpected EOF after which the parent can no
    longer send the reply. The client's tunnelled TLS carries its own close, and
    the leg ends when the parent closes it.
    """

    if isinstance(sock, ssl.SSLSocket):
        return
    with suppress(OSError):
        sock.shutdown(socket.SHUT_WR)


_WOULD_BLOCK: Final = (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError)


class _Flow:
    """One direction of a tunnel: bytes read from the source wait for the destination."""

    def __init__(self, source: socket.socket, destination: socket.socket) -> None:
        self.source = source
        self.destination = destination
        self.buffer = bytearray()
        self.reading = True
        self.done = False
        self.read_waits_on_write = False
        self.write_waits_on_read = False

    @property
    def wants_read(self) -> bool:
        return self.reading and len(self.buffer) < _BUFFER_SIZE

    def step(self) -> bool:
        """Move what is ready without blocking; False once the destination fails."""

        self.read_waits_on_write = False
        self.write_waits_on_read = False
        while self.wants_read:
            try:
                payload = self.source.recv(_BUFFER_SIZE)
            except ssl.SSLWantWriteError:
                self.read_waits_on_write = True
                break
            except _WOULD_BLOCK:
                break
            except OSError:
                payload = b""
            if not payload:
                self.reading = False
                break
            self.buffer.extend(payload)
        while self.buffer:
            try:
                sent = self.destination.send(self.buffer)
            except ssl.SSLWantReadError:
                self.write_waits_on_read = True
                break
            except _WOULD_BLOCK:
                break
            except OSError:
                return False
            del self.buffer[:sent]
        if not self.reading and not self.buffer and not self.done:
            _close_write(self.destination)
            self.done = True
        return True


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Copy both directions in one thread until each has carried its EOF.

    One thread drives both sockets, since a TLS socket must never be read and
    written from two threads at once.
    """

    flows = (_Flow(client, upstream), _Flow(upstream, client))
    for sock in (client, upstream):
        sock.setblocking(False)
    with selectors.DefaultSelector() as selector:
        registered: dict[socket.socket, int] = {}
        while not all(flow.done for flow in flows):
            if not all(flow.step() for flow in flows):
                return
            interest = dict.fromkeys((client, upstream), 0)
            for flow in flows:
                if flow.wants_read:
                    interest[flow.source] |= selectors.EVENT_READ
                if flow.read_waits_on_write:
                    interest[flow.source] |= selectors.EVENT_WRITE
                if flow.write_waits_on_read:
                    interest[flow.destination] |= selectors.EVENT_READ
                elif flow.buffer:
                    interest[flow.destination] |= selectors.EVENT_WRITE
            for sock, events in interest.items():
                current = registered.get(sock, 0)
                if events == current:
                    continue
                if not events:
                    selector.unregister(sock)
                    del registered[sock]
                    continue
                if current:
                    selector.modify(sock, events)
                else:
                    selector.register(sock, events)
                registered[sock] = events
            if all(flow.done for flow in flows):
                return
            if not any(interest.values()):
                return
            selector.select()


class ProxyServer:
    """Accept clients and connect only to the configured corpus destinations."""

    def __init__(
        self,
        settings: Settings,
        connect: Callable[..., socket.socket] = socket.create_connection,
        tls_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self.settings = settings
        self.connect = connect
        self.tls_context_factory = tls_context_factory
        self._lock = threading.Lock()
        self._active = 0

    def _log(self, event: str, host: str, port: int, status: int | None = None) -> None:
        with self._lock:
            active = self._active
        result = f" status={status}" if status is not None else ""
        print(
            f"egress: {event} host={host} port={port}{result} (active={active})",
            file=sys.stderr,
            flush=True,
        )

    def _upstream(self, host: str, port: int, deadline: float) -> tuple[socket.socket, bytes]:
        parent = self.settings.parent_proxy
        address = (host, port) if parent is None else (parent.host, parent.port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("connection setup timed out")
        upstream = self.connect(address, timeout=remaining)
        try:
            if parent is None:
                return upstream, b""
            if parent.use_tls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("parent TLS setup timed out")
                upstream.settimeout(remaining)
                upstream = self.tls_context_factory().wrap_socket(
                    upstream, server_hostname=parent.host
                )
            lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
            if parent.authorization is not None:
                lines.append(f"Proxy-Authorization: {parent.authorization}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("parent setup timed out")
            upstream.settimeout(remaining)
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            head, extra = _read_head(upstream, deadline)
            parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
            if len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1") or parts[1] != b"200":
                raise ValueError("parent refused CONNECT")
            return upstream, extra
        except (OSError, ValueError):
            upstream.close()
            raise

    def handle(self, client: socket.socket) -> None:
        """Handle one client, emitting only fixed statuses and allowlist host names."""

        with self._lock:
            self._active += 1
        host = "-"
        port = 0
        opened = False
        try:
            with client:
                deadline = time.monotonic() + _SETUP_TIMEOUT
                try:
                    head, extra_client = _read_head(client, deadline)
                    request = parse_request(head)
                except RequestError as exc:
                    self._log("refused", host, port, exc.status)
                    _reply(client, exc.status)
                    return
                except (OSError, ValueError):
                    self._log("refused", host, port, 400)
                    _reply(client, 400)
                    return
                if request.method == "GET":
                    _reply(client, 200)
                    return
                assert request.host is not None and request.port is not None
                # The name passed the DNS grammar, so it is safe to log; a refused
                # one is what the allowlist would have to gain.
                host, port = request.host, request.port
                if host not in self.settings.hosts or port != 443:
                    self._log("refused", host, port, 403)
                    _reply(client, 403)
                    return
                try:
                    upstream, extra_upstream = self._upstream(host, port, deadline)
                except (OSError, ValueError):
                    self._log("refused", host, port, 502)
                    _reply(client, 502)
                    return
                with upstream:
                    client.settimeout(None)
                    upstream.settimeout(None)
                    with suppress(OSError):
                        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        if extra_client:
                            upstream.sendall(extra_client)
                        if extra_upstream:
                            client.sendall(extra_upstream)
                        self._log("connection opened", host, port)
                        opened = True
                        _relay(client, upstream)
        finally:
            with self._lock:
                self._active -= 1
            if opened:
                self._log("connection closed", host, port)

    def serve(self, listening: socket.socket, stop: threading.Event) -> None:
        """Serve each accepted connection in a daemon thread until stopped."""

        listening.settimeout(_LISTEN_TIMEOUT)
        while not stop.is_set():
            try:
                client, _ = listening.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()
