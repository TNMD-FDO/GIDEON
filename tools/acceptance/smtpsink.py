"""A small STARTTLS and AUTH SMTP sink for the acceptance harness.

The sink is deliberately a stdlib-only receiver. It accepts authenticated
messages from the acceptance VM, preserves raw DATA bytes (after SMTP dot
unstuffing) when no redaction changes them, and normalizes a MIME message only
when a header or text part is redacted. Passwords never enter the message
record or an error message.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.errors
import email.header
import email.policy
import hmac
import json
import socketserver
import ssl
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self, cast


@dataclass(frozen=True, slots=True)
class SinkMessage:
    """Facts recorded for one accepted message."""

    sender: str
    recipients: tuple[str, ...]
    user: str
    tls: bool
    body_path: Path


_CRLF: Final = "\r\n"
_BAD_SEQUENCE: Final = "503 5.5.1 Bad sequence of commands"
_BAD_BASE64: Final = "501 5.5.2 Invalid base64"
_AUTH_REQUIRED: Final = "530 5.7.0 Authentication required"
_AUTH_TLS_REQUIRED: Final = "530 5.7.0 Must issue a STARTTLS command first"
_AUTH_INVALID: Final = "535 5.7.8 Authentication credentials invalid"
_LOGIN_USERNAME: Final = base64.b64encode(b"Username:").decode("ascii")
_LOGIN_PASSWORD: Final = base64.b64encode(b"Password:").decode("ascii")


def _identity(text: str) -> str:
    return text


def _redact_body(body: bytes, redact: Callable[[str], str]) -> bytes:
    """Redact a MIME message's headers and text parts; unchanged bytes are kept.

    Every header is read decoded and every text part through its declared
    charset, so a base64 or quoted-printable part and a non-ASCII value reach
    the redactor as text (slice-1 ticket 55's sink ruling). The message is
    re-serialized only when something changed — the stored body is evidence a
    person reads, not a record that must round-trip — and a body the parser
    refuses passes through the redactor as text over a byte-preserving
    decoding, so nothing is ever stored unredacted.
    """

    try:
        message = email.message_from_bytes(body, policy=email.policy.default)
        if not message.keys():
            raise email.errors.MessageError("no header: not a MIME message")
        changed = False
        for part in message.walk():
            headers = [(name, str(value)) for name, value in part.items()]
            masked = [(name, redact(value)) for name, value in headers]
            if masked != headers:
                # Every header of the part is removed and re-added raw in its
                # original order, so a repeated name (Received) keeps its place
                # and a placeholder in an address header keeps its brackets,
                # which the policy's address parser would drop; a non-ASCII
                # value goes in as an encoded word, since a raw header is
                # emitted verbatim.
                for name in {name for name, _ in headers}:
                    del part[name]
                for name, value in masked:
                    if not value.isascii():
                        value = email.header.Header(value, "utf-8").encode()
                    part.set_raw(name, value)
                changed = True
            if part.get_content_maintype() != "text":
                continue
            charset = part.get_content_charset() or "utf-8"
            rewrite = False
            try:
                content = part.get_content()
            except LookupError:
                # A charset the codec registry does not know: the transfer
                # encoding is still undone and the text read as UTF-8, so a
                # base64 part never reaches the disk with its value encoded
                # (the code review of v0.1.52); the part is rewritten as UTF-8.
                payload = part.get_payload(decode=True)
                raw = payload if isinstance(payload, bytes) else b""
                content = raw.decode("utf-8", errors="replace")
                charset = "utf-8"
                rewrite = True
            replaced = redact(content)
            if replaced != content or rewrite:
                # The setter regenerates the transfer encoding for the masked
                # text; the part keeps its subtype and, when known, its charset.
                part.set_content(replaced, subtype=part.get_content_subtype(), charset=charset)
                changed = True
        return message.as_bytes() if changed else body
    except (email.errors.MessageError, LookupError, UnicodeError, ValueError, TypeError):
        text = body.decode("utf-8", errors="surrogateescape")
        return redact(text).encode("utf-8", errors="surrogateescape")


class _SinkServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, sink: SmtpSink) -> None:
        self.sink = sink
        super().__init__((sink.address, sink.port), _SmtpHandler)


class _SmtpHandler(socketserver.StreamRequestHandler):
    server: _SinkServer

    def setup(self) -> None:
        super().setup()
        self._tls = False
        self._authenticated_user: str | None = None
        self._sender: str | None = None
        self._recipients: list[str] = []

    @property
    def sink(self) -> SmtpSink:
        return self.server.sink

    def _reply(self, line: str) -> None:
        self.wfile.write((line + _CRLF).encode("ascii"))
        self.wfile.flush()

    def _read_command(self) -> tuple[str, str] | None:
        line = self.rfile.readline()
        if not line:
            return None
        text = line.rstrip(b"\r\n").decode("ascii", errors="replace")
        verb, separator, arguments = text.partition(" ")
        return verb.upper(), arguments.strip() if separator else ""

    def _reset_envelope(self) -> None:
        self._sender = None
        self._recipients = []

    def _read_base64(self, encoded: str | None = None) -> bytes | None:
        if encoded is None:
            self._reply("334 ")
            line = self.rfile.readline()
            if not line:
                return None
            encoded = line.rstrip(b"\r\n").decode("ascii", errors="replace")
        return self._decode_base64(encoded)

    def _decode_base64(self, encoded: str) -> bytes | None:
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError):
            self._reply(_BAD_BASE64)
            return None

    def _credentials(self, encoded: str | None) -> tuple[str, str] | None:
        decoded = self._read_base64(encoded)
        if decoded is None:
            return None
        fields = decoded.split(b"\0")
        if len(fields) != 3:
            self._reply(_BAD_BASE64)
            return None
        try:
            username = fields[1].decode("utf-8")
            password = fields[2].decode("utf-8")
        except UnicodeDecodeError:
            self._reply(_BAD_BASE64)
            return None
        return username, password

    def _authenticate(self, mechanism: str, encoded: str) -> bool:
        if mechanism == "PLAIN":
            credentials = self._credentials(encoded or None)
        elif mechanism == "LOGIN":
            if encoded:
                username_bytes = self._decode_base64(encoded)
            else:
                self._reply("334 " + _LOGIN_USERNAME)
                username_bytes = self._read_base64_line()
            if username_bytes is None:
                return False
            self._reply("334 " + _LOGIN_PASSWORD)
            password_bytes = self._read_base64_line()
            if password_bytes is None:
                return False
            try:
                credentials = (
                    username_bytes.decode("utf-8"),
                    password_bytes.decode("utf-8"),
                )
            except UnicodeDecodeError:
                self._reply(_BAD_BASE64)
                return False
        else:
            self._reply("504 5.5.4 Unrecognized authentication type")
            return False

        if credentials is None:
            return False
        username, password = credentials
        user_matches = hmac.compare_digest(username, self.sink.user)
        password_matches = hmac.compare_digest(password, self.sink.password)
        if not (user_matches and password_matches):
            self._reply(_AUTH_INVALID)
            return False
        self._authenticated_user = username
        self._reply("235 2.7.0 Authentication successful")
        return True

    def _read_base64_line(self) -> bytes | None:
        line = self.rfile.readline()
        if not line:
            return None
        encoded = line.rstrip(b"\r\n").decode("ascii", errors="replace")
        return self._decode_base64(encoded)

    def _start_tls(self) -> None:
        self.wfile.flush()
        self.rfile.close()
        self.wfile.close()
        context = self.sink.ssl_context
        self.connection = context.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb", self.rbufsize)
        self.wfile = self.connection.makefile("wb", self.wbufsize)
        self._tls = True
        # STARTTLS resets the RFC 5321 envelope and authentication state.
        self._authenticated_user = None
        self._reset_envelope()

    def _read_data(self) -> bytes | None:
        body = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            if line in (b".\r\n", b".\n"):
                return bytes(body)
            if line.startswith(b".."):
                line = line[1:]
            body.extend(line)

    def _store_message(self, body: bytes) -> int:
        return self.sink.store(
            self._sender or "",
            tuple(self._recipients),
            self._authenticated_user or "",
            self._tls,
            body,
        )

    def _serve(self) -> None:
        self._reply(f"220 {self.sink.name} ESMTP acceptance sink")
        while True:
            command = self._read_command()
            if command is None:
                return
            verb, arguments = command
            if verb == "EHLO":
                if not arguments:
                    self._reply("501 5.5.4 EHLO requires an argument")
                else:
                    self._reply(f"250-{self.sink.name}")
                    if self._tls:
                        self._reply("250-AUTH PLAIN LOGIN")
                    else:
                        self._reply("250-STARTTLS")
                    self._reply("250 8BITMIME")
            elif verb == "HELO":
                if not arguments:
                    self._reply("501 5.5.4 HELO requires an argument")
                else:
                    self._reply(f"250 {self.sink.name}")
            elif verb == "STARTTLS":
                if self._tls:
                    self._reply(_BAD_SEQUENCE)
                else:
                    self._reply("220 Ready to start TLS")
                    self._start_tls()
            elif verb == "AUTH":
                if not self._tls:
                    self._reply(_AUTH_TLS_REQUIRED)
                elif self._authenticated_user is not None:
                    self._reply(_BAD_SEQUENCE)
                else:
                    mechanism, separator, encoded = arguments.partition(" ")
                    if not mechanism:
                        self._reply("501 5.5.4 AUTH requires a mechanism")
                    else:
                        self._authenticate(mechanism.upper(), encoded if separator else "")
            elif verb == "MAIL":
                if self._authenticated_user is None:
                    self._reply(_AUTH_REQUIRED)
                else:
                    self._mail(arguments)
            elif verb == "RCPT":
                self._rcpt(arguments)
            elif verb == "DATA":
                self._data()
            elif verb == "RSET":
                self._reset_envelope()
                self._reply("250 2.0.0 Ok")
            elif verb == "NOOP":
                self._reply("250 2.0.0 Ok")
            elif verb == "QUIT":
                self._reply("221 2.0.0 Bye")
                return
            else:
                self._reply("500 5.5.2 Command unrecognized")

    def _mail(self, arguments: str) -> None:
        prefix, separator, rest = arguments.partition(":")
        if prefix.upper() != "FROM" or not separator:
            self._reply("501 5.5.4 MAIL syntax error")
            return
        value = rest.strip()
        if not value.startswith("<") or ">" not in value:
            self._reply("501 5.5.4 MAIL syntax error")
            return
        sender, _, parameters = value[1:].partition(">")
        if not sender or (parameters and not parameters[0].isspace()):
            self._reply("501 5.5.4 MAIL syntax error")
            return
        self._reset_envelope()
        self._sender = sender
        self._reply("250 2.1.0 Ok")

    def _rcpt(self, arguments: str) -> None:
        if self._authenticated_user is None:
            self._reply(_AUTH_REQUIRED)
            return
        if self._sender is None:
            self._reply(_BAD_SEQUENCE)
            return
        prefix, separator, rest = arguments.partition(":")
        if prefix.upper() != "TO" or not separator:
            self._reply("501 5.5.4 RCPT syntax error")
            return
        value = rest.strip()
        if not value.startswith("<") or ">" not in value:
            self._reply("501 5.5.4 RCPT syntax error")
            return
        recipient, _, parameters = value[1:].partition(">")
        if not recipient or parameters.strip():
            self._reply("501 5.5.4 RCPT syntax error")
            return
        self._recipients.append(recipient)
        self._reply("250 2.1.5 Ok")

    def _data(self) -> None:
        if self._authenticated_user is None:
            self._reply(_AUTH_REQUIRED)
            return
        if self._sender is None or not self._recipients:
            self._reply(_BAD_SEQUENCE)
            return
        self._reply("354 End data with <CR><LF>.<CR><LF>")
        body = self._read_data()
        if body is None:
            return
        number = self._store_message(body)
        self._reply(f"250 2.0.0 Ok: stored as {number:03d}")
        self._reset_envelope()

    def handle(self) -> None:
        try:
            self._serve()
        except (BrokenPipeError, ConnectionError, ssl.SSLError):
            return
        except Exception as exc:  # noqa: BLE001  # per-connection boundary must not traceback
            print(
                f"acceptance SMTP connection failed: {type(exc).__name__}",
                file=sys.stderr,
            )


class SmtpSink:
    """Threaded SMTP receiver used by one acceptance run."""

    def __init__(
        self,
        *,
        address: str,
        port: int,
        certfile: str,
        keyfile: str,
        user: str,
        password: str,
        mail_dir: Path,
        name: str = "gideon-acceptance",
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.address = address
        self.port = port
        self.certfile = certfile
        self.keyfile = keyfile
        self.user = user
        self.password = password
        self.mail_dir = mail_dir
        self.name = name
        self.redact = redact if redact is not None else _identity
        self._lock = threading.Lock()
        self._messages: list[SinkMessage] = []
        self._server: _SinkServer | None = None
        self._thread: threading.Thread | None = None
        self.ssl_context = self._make_ssl_context()

    @property
    def messages(self) -> tuple[SinkMessage, ...]:
        """A stable, thread-safe snapshot of accepted messages."""

        with self._lock:
            return tuple(self._messages)

    def _make_ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certfile, self.keyfile)
        return context

    def start(self) -> None:
        """Bind and serve in a daemon thread; port zero selects a free port."""

        if self._server is not None:
            raise RuntimeError("the SMTP sink is already running")
        self.mail_dir.mkdir(parents=True, exist_ok=True)
        server = _SinkServer(self)
        self._server = server
        self.port = cast(tuple[str, int], server.server_address)[1]
        self._thread = threading.Thread(
            target=self._serve,
            args=(server,),
            name="gideon-acceptance-smtp",
            daemon=True,
        )
        self._thread.start()

    def _serve(self, server: _SinkServer) -> None:
        try:
            server.serve_forever()
        except Exception as exc:  # noqa: BLE001  # server thread must report without traceback
            print(f"acceptance SMTP server failed: {type(exc).__name__}", file=sys.stderr)

    def stop(self) -> None:
        """Stop serving and close the listening socket."""

        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    def store(
        self,
        sender: str,
        recipients: tuple[str, ...],
        user: str,
        tls: bool,
        body: bytes,
    ) -> int:
        """Persist one accepted message and publish it atomically to readers."""

        with self._lock:
            number = len(self._messages) + 1
            body_path = self.mail_dir / f"{number:03d}.eml"
            redacted_body = _redact_body(body, self.redact)
            sidecar = {
                "sender": sender,
                "recipients": list(recipients),
                "user": user,
                "tls": tls,
            }
            sidecar_text = self.redact(json.dumps(sidecar, sort_keys=True) + "\n")
            body_path.write_bytes(redacted_body)
            body_path.with_suffix(".json").write_text(
                sidecar_text,
                encoding="utf-8",
            )
            self._messages.append(
                SinkMessage(sender, recipients, user, tls, body_path)
            )
            return number

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.stop()
