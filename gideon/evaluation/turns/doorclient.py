"""Call the GIDEON API from inside its container with a mounted bearer key.

The program takes exactly three non-secret arguments: the key-file path, the
target URL, and a positive deadline in seconds.  Its stdin is one JSON object
with a string-valued ``headers`` map and an optional ``body`` value.  A
non-null body is a keyed ``POST`` with ``Content-Type: application/json``; an
omitted or null body is a keyed ``GET``.

Successful stdout is a line-delimited JSON envelope.  The first line is
``{"kind":"head","status":<int>,"media_type":<str>}``.  A non-stream
response has one ``body`` line with its decoded UTF-8 text.  An event-stream
response has one ``data`` line for every received ``data:`` payload, with the
payload text and its elapsed offset, followed by an ``end`` line whose
``done`` value says whether ``data: [DONE]`` arrived.  The final successful
line is ``elapsed`` with the total seconds from the request start.  A network,
decode, or protocol failure ends with a ``failure`` line containing only the
exception class name instead of response or request text.  The client exits
non-zero for such failures.  A missing or empty key file is the one refusal
before an envelope: it prints the fixed refusal on stderr and exits 99.

The socket timeout is reset to the remaining deadline before every network
operation, and the monotonic bound is checked separately, so an active stream
cannot extend the request indefinitely.  This module intentionally imports
only the standard library: it is copied into the ``gideon-api`` container and
must not depend on the checkout's packages.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import socket
import sys
import time
from pathlib import Path
from typing import Final
from urllib.parse import SplitResult, urlsplit

KEY_FILE_REFUSAL: Final[str] = "API key secret file is missing or empty"
KEY_FILE_EXIT_CODE: Final[int] = 99
FAILURE_EXIT_CODE: Final[int] = 1
USAGE_EXIT_CODE: Final[int] = 2
_BODY_READ_SIZE: Final[int] = 64 * 1024
_EVENT_STREAM_MEDIA_TYPE: Final[str] = "text/event-stream"


def _write_line(value: dict[str, object]) -> None:
    """Write one compact envelope line and make it visible immediately."""

    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _write_failure(error: BaseException) -> None:
    """Write a content-free failure line."""

    _write_line({"kind": "failure", "exception": type(error).__name__})


def _read_key(path_text: str) -> str | None:
    """Read the mounted key, removing exactly carriage returns and line feeds."""

    try:
        value = Path(path_text).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print(KEY_FILE_REFUSAL, file=sys.stderr)
        return None
    value = value.replace("\r", "").replace("\n", "")
    if not value:
        print(KEY_FILE_REFUSAL, file=sys.stderr)
        return None
    return value


def _read_request() -> tuple[dict[str, str], object | None]:
    """Decode and validate the one stdin request document without echoing it."""

    document = json.load(sys.stdin)
    if not isinstance(document, dict):
        raise ValueError("request document is not an object")
    raw_headers = document.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError("request headers are not an object")
    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        if (
            not isinstance(name, str)
            or not name
            or "\r" in name
            or "\n" in name
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("request headers are invalid")
        headers[name] = value
    return headers, document.get("body")


def _endpoint(url: str) -> tuple[SplitResult, str, int]:
    """Validate the target URL and return its parsed target and port."""

    if "\r" in url or "\n" in url:
        raise ValueError("target URL contains a line break")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("target URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target URL contains credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("target URL contains a query or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("target URL has an invalid port") from error
    return parsed, parsed.path or "/", port or (443 if parsed.scheme == "https" else 80)


def _remaining(started: float, deadline: float) -> float:
    """Return the remaining bound or raise the standard timeout exception."""

    remaining = deadline - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _set_socket_timeout(sock: socket.socket, started: float, deadline: float) -> None:
    """Arm the request socket for the remaining overall deadline.

    The socket is the one captured at connect, never ``connection.sock``:
    ``getresponse`` hands the socket to the response and clears the
    connection's own reference whenever the response will close, which a
    streamed answer and a ``Connection: close`` reply both do.
    """

    sock.settimeout(_remaining(started, deadline))


def _arm(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    started: float,
    deadline: float,
) -> None:
    """Enforce the overall bound before a read, re-arming a still-open socket.

    The last read of a response releases the socket, so a re-arm after it
    would raise on a closed descriptor; the bound is still checked, so a
    response that ran past the deadline fails as a timeout either way.
    """

    if response.isclosed():
        _remaining(started, deadline)
        return
    _set_socket_timeout(sock, started, deadline)


def _request_headers(headers: dict[str, str], key: str, has_body: bool) -> dict[str, str]:
    """Add the client-owned headers without allowing duplicate credentials."""

    request_headers = {
        name: value
        for name, value in headers.items()
        if name.casefold() not in {"authorization", "content-type", "content-length", "host"}
    }
    request_headers["Authorization"] = f"Bearer {key}"
    if has_body:
        request_headers["Content-Type"] = "application/json"
    return request_headers


def _body_bytes(body: object | None) -> bytes | None:
    """Encode a non-null request body as compact UTF-8 JSON."""

    if body is None:
        return None
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _is_event_stream(media_type: str) -> bool:
    """Return whether a response carries the event-stream media type."""

    return media_type.partition(";")[0].strip().casefold() == _EVENT_STREAM_MEDIA_TYPE


def _read_body(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    started: float,
    deadline: float,
) -> str:
    """Read a whole response while re-arming the overall bound for each chunk."""

    chunks: list[bytes] = []
    while True:
        _arm(response, sock, started, deadline)
        chunk = response.read(_BODY_READ_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    _remaining(started, deadline)
    return b"".join(chunks).decode("utf-8")


def _read_stream(
    response: http.client.HTTPResponse,
    sock: socket.socket,
    started: float,
    deadline: float,
) -> None:
    """Emit every received SSE data payload, then the end line and its flag."""

    done = False
    while True:
        _arm(response, sock, started, deadline)
        line = response.readline()
        if not line:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if not text.startswith("data:"):
            continue
        payload = text[5:]
        if payload.startswith(" "):
            payload = payload[1:]
        if payload == "[DONE]":
            done = True
            break
        _write_line(
            {
                "kind": "data",
                "offset": time.monotonic() - started,
                "payload": payload,
            }
        )
    _remaining(started, deadline)
    _write_line({"kind": "end", "done": done})


def _connection(parsed: SplitResult, port: int, timeout: float) -> http.client.HTTPConnection:
    """Build the standard-library HTTP connection for the target URL."""

    if parsed.hostname is None:
        raise ValueError("target URL has no host")
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def _call(key: str, url: str, deadline: float, headers: dict[str, str], body: object | None) -> None:
    """Make one keyed request and emit its successful envelope."""

    parsed, target, port = _endpoint(url)
    encoded = _body_bytes(body)
    started = time.monotonic()
    connection = _connection(parsed, port, _remaining(started, deadline))
    try:
        method = "POST" if encoded is not None else "GET"
        connection.connect()
        sock = connection.sock
        if sock is None:
            raise OSError("HTTP connection has no socket")
        _set_socket_timeout(sock, started, deadline)
        connection.request(
            method,
            target,
            body=encoded,
            headers=_request_headers(headers, key, encoded is not None),
        )
        _set_socket_timeout(sock, started, deadline)
        response = connection.getresponse()
        media_type = (response.getheader("Content-Type") or "").partition(";")[0].strip()
        _write_line({"kind": "head", "status": response.status, "media_type": media_type})
        if _is_event_stream(media_type):
            _read_stream(response, sock, started, deadline)
        else:
            _write_line({"kind": "body", "text": _read_body(response, sock, started, deadline)})
        _write_line({"kind": "elapsed", "seconds": time.monotonic() - started})
    finally:
        with contextlib.suppress(OSError):
            connection.close()


def _usage() -> int:
    """Print the non-secret invocation shape and return its usage code."""

    print(
        "usage: doorclient.py KEY_FILE URL DEADLINE_SECONDS",
        file=sys.stderr,
    )
    return USAGE_EXIT_CODE


def _deadline(value: str) -> float:
    """Parse a finite positive request deadline."""

    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("deadline must be positive")
    return seconds


def main(argv: list[str] | None = None) -> int:
    """Run the keyed request client and return its process exit code."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3:
        return _usage()
    key = _read_key(arguments[0])
    if key is None:
        return KEY_FILE_EXIT_CODE
    try:
        deadline = _deadline(arguments[2])
        headers, body = _read_request()
        _call(key, arguments[1], deadline, headers, body)
    except Exception as error:  # noqa: BLE001 - the envelope names only the safe class.
        try:
            _write_failure(error)
        except BrokenPipeError:
            return FAILURE_EXIT_CODE
        return FAILURE_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
