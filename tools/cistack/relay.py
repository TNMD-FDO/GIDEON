"""Forward TCP connections from the CI sibling to production's engine."""

from __future__ import annotations

import os
import socket
import sys
import threading
from contextlib import suppress
from typing import Final

_BUFFER_SIZE: Final = 64 * 1024
_LISTEN_TIMEOUT: Final = 0.2
_DEFAULT_PORT: Final = 8000

_connection_lock = threading.Lock()
_active_connections = 0


def _log_open() -> None:
    """Log the active connection count without logging connection content."""

    global _active_connections
    with _connection_lock:
        _active_connections += 1
        active = _active_connections
    print(f"relay: connection opened (active={active})", file=sys.stderr, flush=True)


def _log_close() -> None:
    """Log the active connection count without logging connection content."""

    global _active_connections
    with _connection_lock:
        _active_connections -= 1
        active = _active_connections
    print(f"relay: connection closed (active={active})", file=sys.stderr, flush=True)


def _close_write(sock: socket.socket) -> None:
    """Propagate a reader's EOF to the other socket's write side."""

    with suppress(OSError):
        sock.shutdown(socket.SHUT_WR)


def _copy(source: socket.socket, destination: socket.socket) -> None:
    """Copy one direction and propagate EOF or a socket failure."""

    try:
        while True:
            payload = source.recv(_BUFFER_SIZE)
            if not payload:
                break
            destination.sendall(payload)
    except OSError:
        pass
    finally:
        _close_write(destination)


def _relay_connection(client: socket.socket, target: tuple[str, int]) -> None:
    """Connect one client to the target and copy both directions."""

    try:
        upstream = socket.create_connection(target)
    except OSError:
        print(f"relay: unable to reach target host {target[0]}", file=sys.stderr, flush=True)
        client.close()
        return

    _log_open()
    try:
        client_to_target = threading.Thread(
            target=_copy,
            args=(client, upstream),
            daemon=True,
        )
        target_to_client = threading.Thread(
            target=_copy,
            args=(upstream, client),
            daemon=True,
        )
        client_to_target.start()
        target_to_client.start()
        client_to_target.join()
        target_to_client.join()
    finally:
        client.close()
        upstream.close()
        _log_close()


def serve(listening: socket.socket, target: tuple[str, int]) -> None:
    """Serve accepted connections from ``listening`` to one TCP target."""

    listening.settimeout(_LISTEN_TIMEOUT)
    while True:
        try:
            client, _ = listening.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        threading.Thread(
            target=_relay_connection,
            args=(client, target),
            daemon=True,
        ).start()


def _target(value: str) -> tuple[str, int]:
    """Parse the environment's ``host:port`` target."""

    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text:
        raise ValueError("RELAY_TARGET must be host:port")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("RELAY_TARGET port is outside 1..65535")
    return host, port


def main() -> int:
    """Read relay configuration, bind every interface, and serve forever."""

    try:
        target = _target(os.environ["RELAY_TARGET"])
        port = int(os.environ.get("RELAY_PORT", str(_DEFAULT_PORT)))
        if not 0 <= port <= 65535:
            raise ValueError("RELAY_PORT is outside 0..65535")
    except (KeyError, ValueError):
        print("relay: invalid RELAY_TARGET or RELAY_PORT", file=sys.stderr)
        return 1

    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening.bind(("0.0.0.0", port))
        listening.listen()
        serve(listening, target)
    finally:
        listening.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
