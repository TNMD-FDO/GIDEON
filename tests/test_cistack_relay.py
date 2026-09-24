"""Contracts for the standard-library CI relay."""

import ast
import contextlib
import io
import socket
import socketserver
import sys
import threading
import unittest
from pathlib import Path

from tools.cistack import relay

ROOT = Path(__file__).resolve().parents[1]


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class _BidirectionalHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        greeting = b"target greeting"
        self.request.sendall(greeting)
        payload = self.request.recv(64 * 1024)
        self.request.sendall(b"target reply:" + payload)


class _HalfCloseHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        payload = bytearray()
        while True:
            chunk = self.request.recv(64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        self.request.sendall(b"after half-close:" + payload)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        payload = bytearray()
        while True:
            chunk = self.request.recv(64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        self.request.sendall(b"reply:" + payload)


class _Target:
    def __init__(self, handler: type[socketserver.BaseRequestHandler]) -> None:
        self.server = _ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self) -> tuple[str, int]:
        address = self.server.server_address
        return str(address[0]), int(address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise AssertionError("target server did not stop within its bound")


def _read_all(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


class Relay(unittest.TestCase):
    """The relay preserves socket semantics without exposing payloads."""

    def start_relay(self, target: tuple[str, int]) -> tuple[socket.socket, int]:
        listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        thread = threading.Thread(
            target=relay.serve,
            args=(listening, target),
            daemon=True,
        )
        thread.start()

        def stop() -> None:
            listening.close()
            thread.join(timeout=2.0)
            if thread.is_alive():
                raise AssertionError("relay server did not stop within its bound")

        self.addCleanup(stop)
        return listening, int(listening.getsockname()[1])

    def test_bytes_travel_both_ways_over_ephemeral_loopback_ports(self) -> None:
        target = _Target(_BidirectionalHandler)
        self.addCleanup(target.close)
        _, relay_port = self.start_relay(target.address)

        with socket.create_connection(("127.0.0.1", relay_port), timeout=2.0) as client:
            greeting = client.recv(len(b"target greeting"))
            self.assertEqual(greeting, b"target greeting")
            client.sendall(b"client payload")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(_read_all(client), b"target reply:client payload")

    def test_client_half_close_reaches_target_and_target_reply_reaches_client(self) -> None:
        target = _Target(_HalfCloseHandler)
        self.addCleanup(target.close)
        _, relay_port = self.start_relay(target.address)

        with socket.create_connection(("127.0.0.1", relay_port), timeout=2.0) as client:
            client.sendall(b"request before half-close")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(
                _read_all(client),
                b"after half-close:request before half-close",
            )

    def test_unreachable_target_closes_client_with_one_host_line(self) -> None:
        target_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_listener.bind(("127.0.0.1", 0))
        target = ("127.0.0.1", int(target_listener.getsockname()[1]))
        target_listener.close()
        _, relay_port = self.start_relay(target)
        output = io.StringIO()

        with (
            contextlib.redirect_stderr(output),
            socket.create_connection(("127.0.0.1", relay_port), timeout=2.0) as client,
        ):
            client.settimeout(2.0)
            self.assertEqual(client.recv(1), b"")

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(target[0], lines[0])

    def test_concurrent_connections_are_independent(self) -> None:
        target = _Target(_EchoHandler)
        self.addCleanup(target.close)
        _, relay_port = self.start_relay(target.address)
        barrier = threading.Barrier(3)
        payloads = (b"first connection", b"second connection")
        results: list[bytes | Exception | None] = [None, None]

        def exchange(index: int, payload: bytes) -> None:
            try:
                with socket.create_connection(("127.0.0.1", relay_port), timeout=2.0) as client:
                    client.settimeout(2.0)
                    barrier.wait(timeout=2.0)
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    results[index] = _read_all(client)
            except (OSError, RuntimeError) as error:
                results[index] = error

        threads = [
            threading.Thread(target=exchange, args=(index, payload), daemon=True)
            for index, payload in enumerate(payloads)
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [b"reply:first connection", b"reply:second connection"])

    def test_output_never_contains_payload_bytes(self) -> None:
        target = _Target(_EchoHandler)
        self.addCleanup(target.close)
        _, relay_port = self.start_relay(target.address)
        payload = b"payload must not be logged"
        output = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
            socket.create_connection(("127.0.0.1", relay_port), timeout=2.0) as client,
        ):
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(_read_all(client), b"reply:" + payload)

        self.assertNotIn(payload.decode("ascii"), output.getvalue())

class StandardLibraryBoundary(unittest.TestCase):
    """The mounted relay has no dependency on the checkout's packages."""

    def test_relay_imports_only_the_standard_library(self) -> None:
        source = (ROOT / "tools/cistack/relay.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertEqual(imports - set(sys.stdlib_module_names), set())
        self.assertEqual(
            {"yaml"} - set(sys.stdlib_module_names),
            {"yaml"},
        )
