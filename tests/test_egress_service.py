"""Contracts for the corpus egress door over loopback sockets."""

import base64
import io
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from gideon.egress import proxy, settings

_SOCKET_TIMEOUT = 2.0
_HOST = "allowed.example"
_CONNECT = f"CONNECT {_HOST}:443 HTTP/1.1\r\nHost: {_HOST}:443\r\n\r\n".encode()


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read one complete HTTP head and preserve any bytes following it."""

    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(64 * 1024)
        if not chunk:
            raise AssertionError("socket closed before its HTTP head")
        data.extend(chunk)
    end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:end]), bytes(data[end:])


def _read_all(sock: socket.socket) -> bytes:
    """Read through EOF under the socket's timeout."""

    chunks: list[bytes] = []
    while chunk := sock.recv(64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


class Loopback:
    """Run exactly one bounded socket handler on an ephemeral loopback port."""

    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self.handler = handler
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(_SOCKET_TIMEOUT)
        self.address = ("127.0.0.1", int(self.listener.getsockname()[1]))
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            with self.listener:
                accepted, _ = self.listener.accept()
                with accepted:
                    accepted.settimeout(_SOCKET_TIMEOUT)
                    self.handler(accepted)
        except BaseException as exc:  # noqa: BLE001 - transfer thread errors to the test.
            self.errors.append(exc)

    def __enter__(self) -> "Loopback":
        return self

    def __exit__(self, *_args: object) -> None:
        self.listener.close()
        self.thread.join(timeout=_SOCKET_TIMEOUT + 0.5)
        if self.thread.is_alive():
            raise AssertionError("loopback handler did not stop within its bound")
        if self.errors:
            raise self.errors[0]


def _client(door: Loopback) -> socket.socket:
    client = socket.create_connection(door.address, timeout=_SOCKET_TIMEOUT)
    client.settimeout(_SOCKET_TIMEOUT)
    return client


class Settings(unittest.TestCase):
    """The settings refuse invalid environment by name."""

    def setUp(self) -> None:
        self.environ = {
            settings.HOSTS_ENV: f" {_HOST.upper()} , other.example ",
            settings.PORT_ENV: "3128",
        }

    def test_missing_empty_and_invalid_settings_name_the_variable_and_fix(self) -> None:
        for name in (settings.HOSTS_ENV, settings.PORT_ENV):
            for value in (None, " "):
                with self.subTest(name=name, value=value):
                    environ = dict(self.environ)
                    if value is None:
                        environ.pop(name)
                    else:
                        environ[name] = value
                    with self.assertRaises(settings.EgressSettingsError) as raised:
                        settings.load_settings(environ)
                    self.assertIn(name, str(raised.exception))
                    self.assertIn("Fix:", str(raised.exception))

        for name, value in (
            (settings.HOSTS_ENV, f"{_HOST},"),
            (settings.PORT_ENV, "0"),
            (settings.PORT_ENV, "65536"),
            (settings.PORT_ENV, "3.5"),
            (settings.PARENT_PROXY_ENV, "ftp://parent.example:3128"),
            (settings.PARENT_PROXY_ENV, "http://parent.example:99999"),
        ):
            with self.subTest(name=name, value=value):
                environ = dict(self.environ)
                environ[name] = value
                with self.assertRaises(settings.EgressSettingsError) as raised:
                    settings.load_settings(environ)
                self.assertIn(name, str(raised.exception))
                self.assertIn("Fix:", str(raised.exception))
                self.assertNotIn(value, str(raised.exception))

    def test_host_folding_and_both_parent_schemes(self) -> None:
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                environ = dict(self.environ)
                environ[settings.PARENT_PROXY_ENV] = (
                    f"{scheme}://user:paper%3Asecret@parent.example:8080"
                )
                loaded = settings.load_settings(environ)
                self.assertEqual(loaded.hosts, frozenset({_HOST, "other.example"}))
                self.assertIsNotNone(loaded.parent_proxy)
                assert loaded.parent_proxy is not None
                self.assertEqual((loaded.parent_proxy.host, loaded.parent_proxy.port), ("parent.example", 8080))
                self.assertEqual(loaded.parent_proxy.use_tls, scheme == "https")
                assert loaded.parent_proxy.authorization is not None
                self.assertTrue(loaded.parent_proxy.authorization.startswith("Basic "))
                self.assertEqual(
                    base64.b64decode(loaded.parent_proxy.authorization.removeprefix("Basic ")),
                    b"user:paper:secret",
                )

        environ = dict(self.environ)
        environ[settings.PARENT_PROXY_ENV] = "http://parent.example:8080"
        parent = settings.load_settings(environ).parent_proxy
        assert parent is not None
        self.assertIsNone(parent.authorization)

        for scheme, port in (("http", 80), ("https", 443)):
            with self.subTest(scheme=scheme, port="omitted"):
                environ[settings.PARENT_PROXY_ENV] = f"{scheme}://parent.example"
                parent = settings.load_settings(environ).parent_proxy
                assert parent is not None
                self.assertEqual((parent.host, parent.port), ("parent.example", port))


class RequestParsing(unittest.TestCase):
    """The door parses a DNS CONNECT target before applying the group."""

    def test_folded_and_trailing_dot_targets_and_other_port(self) -> None:
        for target, port in (("AlLoWeD.ExAmPlE", 443), ("ALLOWED.EXAMPLE.", 443), ("allowed.example", 444)):
            with self.subTest(target=target, port=port):
                head = f"CONNECT {target}:{port} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
                parsed = proxy.parse_request(head)
                self.assertEqual(parsed.method, "CONNECT")
                self.assertEqual((parsed.host, parsed.port), (_HOST, port))

    def test_other_method_and_unparsable_line_have_distinct_statuses(self) -> None:
        for head, status in (
            (b"GET http://allowed.example/ HTTP/1.1\r\n\r\n", 405),
            (b"CONNECT missing-version\r\n\r\n", 400),
        ):
            with self.subTest(head=head):
                with self.assertRaises(proxy.RequestError) as raised:
                    proxy.parse_request(head)
                self.assertEqual(raised.exception.status, status)


class Tunnel(unittest.TestCase):
    """The door admits only allowed HTTPS tunnels and copies both ways."""

    def setUp(self) -> None:
        self.environ = {settings.HOSTS_ENV: _HOST, settings.PORT_ENV: "3128"}

    def _server(
        self,
        connect: Callable[..., socket.socket],
        tls_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> proxy.ProxyServer:
        return proxy.ProxyServer(
            settings.load_settings(self.environ),
            connect=connect,
            tls_context_factory=tls_context_factory,
        )

    def test_health_is_200_without_body_or_log_or_upstream(self) -> None:
        def forbidden_connect(*_args: object, **_kwargs: object) -> socket.socket:
            self.fail("healthcheck dialled an upstream")

        log = io.StringIO()
        with (
            redirect_stderr(log),
            Loopback(self._server(forbidden_connect).handle) as door,
            _client(door) as client,
        ):
            client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            reply = _read_all(client)
        self.assertTrue(reply.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertTrue(reply.endswith(b"\r\n\r\n"))
        self.assertEqual(log.getvalue(), "")

    def test_oversized_head_is_400_without_upstream(self) -> None:
        def forbidden_connect(*_args: object, **_kwargs: object) -> socket.socket:
            self.fail("oversized head dialled an upstream")

        with Loopback(self._server(forbidden_connect).handle) as door, _client(door) as client:
            client.sendall(
                b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Pad: "
                + b"x" * proxy._HEAD_LIMIT
                + b"\r\n\r\n"
            )
            reply = _read_all(client)
        self.assertTrue(reply.startswith(b"HTTP/1.1 400 Bad Request\r\n"))

    def test_allowed_direct_tunnel_copies_both_ways_and_propagates_eof(self) -> None:
        received: list[bytes] = []
        dialled: list[tuple[str, int]] = []

        def upstream(sock: socket.socket) -> None:
            sock.sendall(b"UPSTREAM_SENTINEL")
            received.append(_read_all(sock))
            sock.sendall(b"RETURN_SENTINEL")

        with Loopback(upstream) as peer:
            def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                dialled.append(address)
                return socket.create_connection(peer.address, timeout=timeout)

            log = io.StringIO()
            with (
                redirect_stderr(log),
                Loopback(self._server(connect).handle) as door,
                _client(door) as client,
            ):
                client.sendall(_CONNECT)
                head, extra = _read_head(client)
                self.assertEqual(head, b"HTTP/1.1 200 Connection established\r\n\r\n")
                client.sendall(b"CLIENT_SENTINEL")
                client.shutdown(socket.SHUT_WR)
                payload = extra + _read_all(client)

        self.assertEqual(dialled, [(_HOST, 443)])
        self.assertEqual(received, [b"CLIENT_SENTINEL"])
        self.assertEqual(payload, b"UPSTREAM_SENTINELRETURN_SENTINEL")
        self.assertIn(f"connection opened host={_HOST} port=443 (active=1)", log.getvalue())
        self.assertIn(f"connection closed host={_HOST} port=443 (active=0)", log.getvalue())
        for sentinel in ("CLIENT_SENTINEL", "UPSTREAM_SENTINEL", "RETURN_SENTINEL"):
            self.assertNotIn(sentinel, log.getvalue())

    def test_refused_name_and_port_are_403_without_dial(self) -> None:
        dialled: list[tuple[str, int]] = []

        def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
            dialled.append(address)
            raise AssertionError("refused request dialled an upstream")

        log = io.StringIO()
        with redirect_stderr(log):
            for target in ("blocked.example:443", f"{_HOST}:444"):
                with self.subTest(target=target), Loopback(self._server(connect).handle) as door:
                    with _client(door) as client:
                        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
                        reply = _read_all(client)
                    self.assertTrue(reply.startswith(b"HTTP/1.1 403 Forbidden\r\n"))
        self.assertEqual(dialled, [])
        self.assertIn("refused host=blocked.example port=443 status=403", log.getvalue())

    def test_parent_connect_waits_for_complete_head_and_hides_credential(self) -> None:
        self.environ[settings.PARENT_PROXY_ENV] = (
            "http://parent:paper%3Asecret@parent.example:8080"
        )
        parent_head: list[bytes] = []
        parent_payload: list[bytes] = []
        status_sent = threading.Event()
        finish_head = threading.Event()

        def parent(sock: socket.socket) -> None:
            head, extra = _read_head(sock)
            parent_head.append(head)
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n")
            status_sent.set()
            if not finish_head.wait(timeout=_SOCKET_TIMEOUT):
                raise AssertionError("parent head was not released")
            sock.sendall(b"X-Parent-Only: HEADER_SENTINEL\r\n\r\n")
            parent_payload.append(extra + _read_all(sock))
            sock.sendall(b"PARENT_SENTINEL")

        with Loopback(parent) as peer:
            def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                self.assertEqual(address, ("parent.example", 8080))
                return socket.create_connection(peer.address, timeout=timeout)

            log = io.StringIO()
            with (
                redirect_stderr(log),
                Loopback(self._server(connect).handle) as door,
                _client(door) as client,
            ):
                client.sendall(_CONNECT)
                self.assertTrue(status_sent.wait(timeout=_SOCKET_TIMEOUT))
                client.settimeout(0.1)
                with self.assertRaises(TimeoutError):
                    client.recv(1)
                client.settimeout(_SOCKET_TIMEOUT)
                finish_head.set()
                head, extra = _read_head(client)
                self.assertEqual(head, b"HTTP/1.1 200 Connection established\r\n\r\n")
                client.sendall(b"CLIENT_SENTINEL")
                client.shutdown(socket.SHUT_WR)
                payload = extra + _read_all(client)

        authorization = "Basic " + base64.b64encode(b"parent:paper:secret").decode("ascii")
        self.assertIn(b"CONNECT allowed.example:443 HTTP/1.1\r\n", parent_head[0])
        self.assertIn(b"Host: allowed.example:443\r\n", parent_head[0])
        self.assertIn(f"Proxy-Authorization: {authorization}\r\n".encode(), parent_head[0])
        self.assertEqual(parent_payload, [b"CLIENT_SENTINEL"])
        self.assertEqual(payload, b"PARENT_SENTINEL")
        self.assertNotIn(b"HEADER_SENTINEL", payload)
        for secret in ("paper", "secret", authorization, "CLIENT_SENTINEL", "PARENT_SENTINEL"):
            self.assertNotIn(secret, log.getvalue())

    def test_parent_407_becomes_content_free_502(self) -> None:
        self.environ[settings.PARENT_PROXY_ENV] = "http://parent:secret@parent.example:8080"
        parent_head: list[bytes] = []

        def parent(sock: socket.socket) -> None:
            head, _ = _read_head(sock)
            parent_head.append(head)
            sock.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=PRIVATE_SENTINEL\r\n\r\n"
            )

        with Loopback(parent) as peer:
            def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                self.assertEqual(address, ("parent.example", 8080))
                return socket.create_connection(peer.address, timeout=timeout)

            log = io.StringIO()
            with (
                redirect_stderr(log),
                Loopback(self._server(connect).handle) as door,
                _client(door) as client,
            ):
                client.sendall(_CONNECT)
                reply = _read_all(client)

        self.assertTrue(parent_head)
        self.assertTrue(reply.startswith(b"HTTP/1.1 502 Bad Gateway\r\n"))
        self.assertNotIn(b"PRIVATE_SENTINEL", reply)
        self.assertNotIn(b"407", reply)
        self.assertIn(f"refused host={_HOST} port=443 status=502", log.getvalue())
        self.assertNotIn("secret", log.getvalue())
        self.assertNotIn("PRIVATE_SENTINEL", log.getvalue())

    def test_https_parent_carries_both_ways_and_the_half_close(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required for the HTTPS parent test")
        with tempfile.TemporaryDirectory() as temporary:
            certfile = Path(temporary) / "parent.crt"
            keyfile = Path(temporary) / "parent.key"
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "ec",
                    "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(keyfile), "-out", str(certfile), "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                ],
                check=True,
                capture_output=True,
                timeout=10,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certfile, keyfile)
            self.environ[settings.PARENT_PROXY_ENV] = "https://user:secret@localhost:8443"
            parent_head: list[bytes] = []
            parent_payload: list[bytes] = []

            def parent(sock: socket.socket) -> None:
                with server_context.wrap_socket(sock, server_side=True) as tls:
                    head, extra = _read_head(tls)
                    parent_head.append(head)
                    tls.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    received = bytearray(extra)
                    while len(received) < len(b"CLIENT_SENTINEL"):
                        received.extend(tls.recv(64 * 1024))
                    parent_payload.append(bytes(received))
                    tls.sendall(b"TLS_SENTINEL")

            with Loopback(parent) as peer:
                def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                    self.assertEqual(address, ("localhost", 8443))
                    return socket.create_connection(peer.address, timeout=timeout)

                log = io.StringIO()
                with (
                    redirect_stderr(log),
                    Loopback(
                        self._server(
                            connect,
                            tls_context_factory=lambda: ssl.create_default_context(cafile=str(certfile)),
                        ).handle
                    ) as door,
                    _client(door) as client,
                ):
                    client.sendall(_CONNECT)
                    head, extra = _read_head(client)
                    self.assertEqual(head, b"HTTP/1.1 200 Connection established\r\n\r\n")
                    client.sendall(b"CLIENT_SENTINEL")
                    client.shutdown(socket.SHUT_WR)
                    payload = extra + _read_all(client)

            self.assertTrue(parent_head)
            # After the client's half-close the parent's reply still crosses the TLS leg.
            self.assertEqual(parent_payload, [b"CLIENT_SENTINEL"])
            self.assertEqual(payload, b"TLS_SENTINEL")
            self.assertNotIn("secret", log.getvalue())
            self.assertNotIn("TLS_SENTINEL", log.getvalue())

    def test_established_tunnel_outlives_setup_bound_without_client_bytes(self) -> None:
        late = threading.Event()

        def upstream(sock: socket.socket) -> None:
            if not late.wait(timeout=_SOCKET_TIMEOUT):
                raise AssertionError("late transfer was not released")
            sock.sendall(b"AFTER_BOUND_SENTINEL")

        with Loopback(upstream) as peer:
            def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                self.assertEqual(address, (_HOST, 443))
                return socket.create_connection(peer.address, timeout=timeout)

            with (
                patch.object(proxy, "_SETUP_TIMEOUT", 0.4),
                Loopback(self._server(connect).handle) as door,
                _client(door) as client,
            ):
                client.sendall(_CONNECT)
                head, extra = _read_head(client)
                self.assertEqual(head, b"HTTP/1.1 200 Connection established\r\n\r\n")
                time.sleep(0.6)
                late.set()
                self.assertEqual(extra + _read_all(client), b"AFTER_BOUND_SENTINEL")

    def test_slow_client_pauses_upstream_reads_then_resumes(self) -> None:
        # Far beyond the relay buffer and the loopback socket buffers together,
        # so the door must stop reading upstream and later resume.
        body = bytes(range(256)) * (64 * 1024 * 2)

        def upstream(sock: socket.socket) -> None:
            sock.sendall(body)

        with Loopback(upstream) as peer:
            def connect(address: tuple[str, int], *, timeout: float) -> socket.socket:
                return socket.create_connection(peer.address, timeout=timeout)

            with Loopback(self._server(connect).handle) as door, _client(door) as client:
                client.sendall(_CONNECT)
                head, extra = _read_head(client)
                self.assertEqual(head, b"HTTP/1.1 200 Connection established\r\n\r\n")
                time.sleep(0.3)
                client.shutdown(socket.SHUT_WR)
                received = extra + _read_all(client)
        self.assertEqual(len(received), len(body))
        self.assertEqual(received, body)
