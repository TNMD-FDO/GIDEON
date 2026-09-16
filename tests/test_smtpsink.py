"""STARTTLS, AUTH, and slice-1 ticket 55 message-persistence contracts."""

import base64
import email
import email.message
import email.policy
import json
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch

from gideon.host.site import SiteConfig, load_site
from tools.acceptance.smtpsink import SmtpSink
from tools.redact.core import redact

ROOT = Path(__file__).resolve().parent.parent
REDACT_SITE = ROOT / "tests/fixtures/site/redact-office.yaml"


class SmtpSinkTests(unittest.TestCase):
    USER = "acceptance-user"
    PASSWORD = "acceptance-password"
    _temporary: ClassVar[tempfile.TemporaryDirectory[str]]
    sink: ClassVar[SmtpSink]

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl is required for the SMTP sink test")
        cls._temporary = tempfile.TemporaryDirectory()
        root = Path(cls._temporary.name)
        certfile = root / "sink.crt"
        keyfile = root / "sink.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(keyfile),
                "-out",
                str(certfile),
                "-days",
                "2",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        cls.sink = SmtpSink(
            address="127.0.0.1",
            port=0,
            certfile=str(certfile),
            keyfile=str(keyfile),
            user=cls.USER,
            password=cls.PASSWORD,
            mail_dir=root / "mail",
        )
        cls.sink.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "sink"):
            cls.sink.stop()
        if hasattr(cls, "_temporary"):
            cls._temporary.cleanup()

    @classmethod
    def _tls_context(cls) -> ssl.SSLContext:
        certfile = Path(cls._temporary.name) / "sink.crt"
        return ssl.create_default_context(cafile=str(certfile))

    def _smtp(self) -> smtplib.SMTP:
        return smtplib.SMTP("127.0.0.1", self.sink.port, timeout=5)

    def _start_tls(self, smtp: smtplib.SMTP) -> None:
        smtp.starttls(context=self._tls_context())
        smtp.ehlo()

    def _login_plain(self, smtp: smtplib.SMTP) -> None:
        smtp.login(self.USER, self.PASSWORD)

    def test_ehlo_and_auth_capabilities_change_after_starttls(self) -> None:
        with self._smtp() as smtp:
            code, _ = smtp.ehlo()
            self.assertEqual(code, 250)
            self.assertIn("starttls", smtp.esmtp_features)
            self.assertNotIn("auth", smtp.esmtp_features)
            auth_code, _ = smtp.docmd("AUTH", "PLAIN")
            self.assertEqual(auth_code, 530)
            with self.assertRaises(smtplib.SMTPException):
                smtp.login(self.USER, self.PASSWORD)

            self._start_tls(smtp)
            self.assertNotIn("starttls", smtp.esmtp_features)
            # smtplib keeps the feature's parameters with a leading space.
            self.assertEqual(smtp.esmtp_features.get("auth", "").split(), ["PLAIN", "LOGIN"])
            starttls_again, _ = smtp.docmd("STARTTLS")
            self.assertEqual(starttls_again, 503)

    def test_plain_auth_succeeds_and_wrong_password_is_535(self) -> None:
        with self._smtp() as smtp:
            self._start_tls(smtp)
            self._login_plain(smtp)

        with self._smtp() as smtp:
            self._start_tls(smtp)
            with self.assertRaises(smtplib.SMTPAuthenticationError) as raised:
                smtp.login(self.USER, "wrong-password")
            self.assertEqual(raised.exception.smtp_code, 535)

    def test_login_auth_mechanism_succeeds_separately(self) -> None:
        with self._smtp() as smtp:
            self._start_tls(smtp)
            smtp.user = self.USER
            smtp.password = self.PASSWORD
            code, _ = smtp.auth("LOGIN", smtp.auth_login)
            self.assertEqual(code, 235)

    def test_login_auth_challenge_prompts_succeed(self) -> None:
        with self._smtp() as smtp:
            self._start_tls(smtp)
            smtp.user = self.USER
            smtp.password = self.PASSWORD
            code, _ = smtp.auth("LOGIN", smtp.auth_login, initial_response_ok=False)
            self.assertEqual(code, 235)

    def test_mail_before_auth_is_refused(self) -> None:
        with self._smtp() as smtp:
            self._start_tls(smtp)
            with self.assertRaises(smtplib.SMTPSenderRefused) as raised:
                smtp.sendmail("sender@example.invalid", ["recipient@example.invalid"], "body\n")
            self.assertEqual(raised.exception.smtp_code, 530)

    def test_authenticated_message_is_dot_unstuffed_and_recorded(self) -> None:
        message = "Subject: sink test\r\n\r\nfirst\r\n.second\r\n"
        sender = "sender@example.invalid"
        recipients = ["one@example.invalid", "two@example.invalid"]
        before = len(self.sink.messages)

        with self._smtp() as smtp:
            self._start_tls(smtp)
            self._login_plain(smtp)
            self.assertEqual(smtp.sendmail(sender, recipients, message), {})
            quit_code, _ = smtp.quit()
            self.assertEqual(quit_code, 221)

        recorded = self.sink.messages[before]
        self.assertEqual(recorded.sender, sender)
        self.assertEqual(recorded.recipients, tuple(recipients))
        self.assertEqual(recorded.user, self.USER)
        self.assertTrue(recorded.tls)
        self.assertEqual(recorded.body_path.read_bytes().decode("ascii"), message)
        sidecar = json.loads(recorded.body_path.with_suffix(".json").read_text())
        self.assertEqual(
            sidecar,
            {
                "sender": sender,
                "recipients": recipients,
                "user": self.USER,
                "tls": True,
            },
        )
        self.assertNotIn(self.PASSWORD, recorded.body_path.read_text())

    def test_sequential_clients_receive_distinct_message_numbers(self) -> None:
        before = len(self.sink.messages)
        for body in ("first\n", "second\n"):
            with self._smtp() as smtp:
                self._start_tls(smtp)
                self._login_plain(smtp)
                smtp.sendmail("sender@example.invalid", ["recipient@example.invalid"], body)
                smtp.quit()

        messages = self.sink.messages[before:]
        self.assertEqual(len(messages), 2)
        first_number = int(messages[0].body_path.stem)
        second_number = int(messages[1].body_path.stem)
        self.assertEqual(second_number, first_number + 1)


class SmtpStoreContracts(unittest.TestCase):
    """Test persistence without opening the sink's real loopback listener."""

    site: ClassVar[SiteConfig]

    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_site(REDACT_SITE)
        if loaded.config is None:
            raise AssertionError(loaded.errors)
        cls.site = loaded.config

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        mail_dir = Path(self._temporary.name) / "mail"
        mail_dir.mkdir()
        with patch.object(
            SmtpSink,
            "_make_ssl_context",
            return_value=cast(ssl.SSLContext, object()),
        ):
            self.sink = SmtpSink(
                address="127.0.0.1",
                port=0,
                certfile="unused",
                keyfile="unused",
                user="capture-user",
                password="capture-password",
                mail_dir=mail_dir,
                redact=lambda text: redact(text, self.site),
            )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _store(self, body: bytes) -> bytes:
        self.sink.store(
            self.site.alerts.smtp.from_,
            (self.site.alerts.recipients[0],),
            "capture-user",
            True,
            body,
        )
        record = self.sink.messages[-1]
        sidecar = json.loads(record.body_path.with_suffix(".json").read_text())
        self.assertEqual(sidecar["sender"], "<alerts.smtp.from>")
        self.assertEqual(
            sidecar["recipients"],
            ["<alerts.recipients[0]>"],
        )
        return record.body_path.read_bytes()

    def _message(self, transfer: str) -> bytes:
        message = email.message.EmailMessage()
        message["To"] = self.site.alerts.recipients[0]
        message["X-Office-Host"] = self.site.hostname
        if transfer == "8bit":
            message.set_content(
                f"non-ASCII recipient {self.site.alerts.recipients[1]}\n",
                cte="8bit",
            )
        elif transfer == "base64":
            message.set_content(
                f"https://{self.site.hostname}/health\n", cte="base64"
            )
        elif transfer == "quoted-printable":
            message.set_content(
                f"backup {self.site.backup.target.host}\n", cte="quoted-printable"
            )
        else:
            message.set_content("plain part\n")
            message.add_alternative(
                f"<p>{self.site.alerts.smtp.from_}</p>\n",
                subtype="html",
                cte="base64",
            )
        return message.as_bytes()

    def test_store_redacts_headers_and_each_transfer_encoding(self) -> None:
        """Ticket 55's sink ruling reaches decoded MIME text before storage."""

        for transfer in ("8bit", "base64", "quoted-printable", "multipart"):
            with self.subTest(transfer=transfer):
                original = self._message(transfer)
                stored = self._store(original)
                parsed = email.message_from_bytes(stored, policy=email.policy.default)
                before = email.message_from_bytes(original, policy=email.policy.default)
                self.assertEqual(parsed.is_multipart(), before.is_multipart())
                self.assertEqual(
                    [part.get_content_type() for part in parsed.walk()],
                    [part.get_content_type() for part in before.walk()],
                )
                self.assertIn(b"To: <alerts.recipients[0]>", stored)
                text_parts = [
                    part.get_content()
                    for part in parsed.walk()
                    if part.get_content_maintype() == "text"
                ]
                expected = {
                    "8bit": "<alerts.recipients[1]>",
                    "base64": "<hostname>",
                    "quoted-printable": "<backup.target.host>",
                    "multipart": "<alerts.smtp.from>",
                }[transfer]
                self.assertTrue(any(expected in content for content in text_parts))

    def test_store_keeps_unchanged_crlf_bytes(self) -> None:
        """Ticket 55 keeps an untouched stored body byte-identical, including CRLF."""

        body = b"Subject: unchanged\r\nX-Test: no-site\r\n\r\nfirst\r\n"
        self.sink.store(
            "sender@example.invalid",
            ("recipient@example.invalid",),
            "capture-user",
            True,
            body,
        )
        record = self.sink.messages[-1]
        self.assertEqual(record.body_path.read_bytes(), body)

    def test_store_falls_back_to_whole_text_when_mime_parser_refuses(self) -> None:
        """Ticket 55 stores a refused body through surrogateescape text redaction."""

        body = f"not a MIME message https://{self.site.hostname}\n".encode()
        stored = self._store(body)
        self.assertEqual(stored, b"not a MIME message https://<hostname>\n")

    def test_store_redacts_a_base64_part_whose_charset_is_unknown(self) -> None:
        """A charset the codec registry lacks still has its transfer encoding undone,
        so the value never reaches the disk base64-encoded (ticket 55's code review)."""

        text = f"https://{self.site.hostname}/health\n"
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        body = (
            "Content-Type: text/plain; charset=x-no-such-charset\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            f"{encoded}\r\n"
        ).encode("ascii")
        stored = self._store(body)
        parsed = email.message_from_bytes(stored, policy=email.policy.default)
        self.assertEqual(parsed.get_content(), "https://<hostname>/health\n")
        self.assertNotIn(encoded.encode("ascii"), stored)
        self.assertNotIn(self.site.hostname.encode("utf-8"), stored)


if __name__ == "__main__":
    unittest.main()
