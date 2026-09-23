"""Grafana's ingress client and bounded readiness probe."""

import base64
import http.client
import http.server
import json
import threading
import unittest
from typing import Any, ClassVar
from unittest.mock import patch

from gideon.host.grafana import (
    Alert,
    Client,
    GrafanaError,
    Receiver,
    Response,
    SniHTTPSConnection,
    wait_ready,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    alert_status: ClassVar[int] = 200
    alert_body: ClassVar[object] = []
    alert_headers: ClassVar[dict[str, str]] = {}
    alert_path: ClassVar[str] = ""
    post_body: ClassVar[dict[str, Any] | None] = None
    post_headers: ClassVar[dict[str, str]] = {}
    post_path: ClassVar[str] = ""
    post_response: ClassVar[dict[str, Any]] = {"status": "success", "duration": "1ms"}

    def _reply(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/grafana/api/alertmanager/grafana/api/v2/alerts":
            self.__class__.alert_headers = dict(self.headers.items())
            self.__class__.alert_path = self.path
            self._reply(self.alert_status, json.dumps(self.alert_body).encode())
        elif self.path == "/grafana/api/health":
            self._reply(200, b'{"database": "ok"}')
        elif self.path == "/grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers":
            self._reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "slack-receiver"},
                                "spec": {
                                    "title": "other",
                                    "integrations": [
                                        {
                                            "uid": "slack",
                                            "type": "slack",
                                            "version": "1",
                                            "disableResolveMessage": False,
                                            "settings": {},
                                            "secureFields": {},
                                        }
                                    ],
                                },
                            },
                            {
                                "metadata": {"name": "page-email"},
                                "spec": {
                                    "title": "page",
                                    "integrations": [
                                        {
                                            "uid": "page-email",
                                            "type": "email",
                                            "version": "1",
                                            "disableResolveMessage": True,
                                            "settings": {
                                                "addresses": "alerts@example.invalid",
                                                "singleEmail": True,
                                            },
                                            "secureFields": {"password": True},
                                        }
                                    ],
                                },
                            },
                        ]
                    }
                ).encode(),
            )
        elif self.path == "/grafana/echo":
            self._reply(
                200,
                json.dumps(
                    {
                        "authorization": self.headers.get("Authorization"),
                        "host": self.headers.get("Host"),
                    }
                ).encode(),
            )
        else:
            self._reply(503, b'{"message": "not ready"}')

    def do_POST(self) -> None:
        if self.path != "/grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/page-email/test":
            self._reply(404, b'{"message":"not found"}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.__class__.post_body = json.loads(raw.decode())
        self.__class__.post_headers = dict(self.headers.items())
        self.__class__.post_path = self.path
        self._reply(200, json.dumps(self.post_response).encode())

    def log_message(self, format: str, *args: object) -> None:
        return


class ClientOverLoopback(unittest.TestCase):
    server: http.server.HTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, **kwargs: Any) -> Client:
        return Client(
            f"http://127.0.0.1:{self.server.server_port}/grafana",
            **kwargs,
        )

    def test_health_path_and_basic_credentials_are_sent(self) -> None:
        password = "not-in-error"
        response = self.client(
            server_hostname="grafana.example.invalid",
            credential=("grafana-admin", password),
        ).request("GET", "/echo")
        assert isinstance(response.body, dict)
        self.assertEqual(response.body["host"], "grafana.example.invalid")
        self.assertEqual(
            response.body["authorization"],
            "Basic "
            + base64.b64encode(f"grafana-admin:{password}".encode()).decode(),
        )
        self.assertNotIn(password, repr(response))
        self.assertTrue(
            wait_ready(self.client(), attempts=1, sleep=lambda _: None).ok
        )

    def test_receiver_lookup_and_test_echo_the_stored_integration(self) -> None:
        client = self.client(credential=("grafana-admin", "password"))
        receiver = client.get_receiver("page")
        self.assertIsNotNone(receiver)
        assert receiver is not None
        self.assertIsInstance(receiver, Receiver)
        self.assertEqual(receiver.uid, "page-email")
        integration = receiver.integrations[0]
        result = client.test_receiver(
            receiver, integration, recipients=("alerts@example.invalid",)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(_Handler.post_headers["Request-Timeout"], "30")
        assert _Handler.post_body is not None
        self.assertEqual(
            _Handler.post_body["integration"], integration
        )
        self.assertEqual(_Handler.post_body["alert"]["labels"]["alertname"], "GideonAlertsTest")
        self.assertEqual(
            _Handler.post_path,
            "/grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/page-email/test",
        )

    def test_get_alerts_path_credentials_and_alert_fields(self) -> None:
        _Handler.alert_body = [
            {
                "labels": {"alertname": "One", "class": "page"},
                "annotations": {"summary": "First", "runbook": "#one"},
                "startsAt": "2099-01-02T03:04:05Z",
                "status": {"state": "active", "silencedBy": []},
            },
            {
                "labels": {"alertname": "Two"},
                "annotations": {},
                "startsAt": "2099-01-02T03:04:06Z",
                "status": {"state": "suppressed", "silencedBy": ["silence-id"]},
            },
        ]
        _Handler.alert_status = 200
        password = "alert-password"
        alerts = self.client(credential=("grafana-admin", password)).get_alerts()
        self.assertEqual(_Handler.alert_path, "/grafana/api/alertmanager/grafana/api/v2/alerts")
        self.assertEqual(
            _Handler.alert_headers["Authorization"],
            "Basic " + base64.b64encode(f"grafana-admin:{password}".encode()).decode(),
        )
        self.assertEqual(len(alerts), 2)
        self.assertIsInstance(alerts[0], Alert)
        self.assertEqual(alerts[0].labels["alertname"], "One")
        self.assertEqual(alerts[0].annotations["summary"], "First")
        self.assertEqual(alerts[0].starts_at, "2099-01-02T03:04:05Z")
        self.assertEqual(alerts[0].state, "active")
        self.assertFalse(alerts[0].silenced)
        self.assertTrue(alerts[1].silenced)

    def test_get_alerts_refuses_non_list_body_without_body_text(self) -> None:
        _Handler.alert_body = {"private": "response detail"}
        _Handler.alert_status = 200
        try:
            with self.assertRaises(GrafanaError) as ctx:
                self.client().get_alerts()
            self.assertIn("alert list", ctx.exception.problem)
            self.assertIn("apply", ctx.exception.fix)
            self.assertNotIn("response detail", str(ctx.exception))
        finally:
            _Handler.alert_body = []

    def test_get_alerts_refuses_non_success_status_without_body_text(self) -> None:
        _Handler.alert_body = {"private": "response detail"}
        _Handler.alert_status = 503
        try:
            with self.assertRaises(GrafanaError) as ctx:
                self.client().get_alerts()
            self.assertIn("503", ctx.exception.problem)
            self.assertIn("apply", ctx.exception.fix)
            self.assertNotIn("response detail", str(ctx.exception))
        finally:
            _Handler.alert_body = []
            _Handler.alert_status = 200

    def test_failed_receiver_status_redacts_addresses_and_extracts_smtp_code(self) -> None:
        _Handler.post_response = {
            "status": "failure",
            "duration": "1ms",
            "error": "smtp returned 550 for alerts@example.invalid",
        }
        try:
            receiver = self.client().get_receiver("page")
            assert receiver is not None
            result = self.client().test_receiver(
                receiver,
                receiver.integrations[0],
                recipients=("alerts@example.invalid",),
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.smtp_code, 550)
            self.assertNotIn("alerts@example.invalid", result.problem or "")
            self.assertIn("<recipient>", result.problem or "")
            self.assertIn("/etc/gideon/secrets/smtp_password", result.fix)
        finally:
            _Handler.post_response = {"status": "success", "duration": "1ms"}

    def test_integration_without_version_refuses_apply(self) -> None:
        receiver = Receiver("page-email", "page", ())
        with self.assertRaises(GrafanaError) as ctx:
            self.client().test_receiver(
                receiver,
                {"uid": "page-email", "type": "email"},
            )
        self.assertIn("no version", ctx.exception.problem)
        self.assertIn("apply", ctx.exception.fix)

    def test_credentials_never_enter_an_error_or_client_repr(self) -> None:
        password = "private-grafana-password"
        client = Client(
            "http://127.0.0.1:1/grafana",
            credential=("grafana-admin", password),
        )
        with self.assertRaises(GrafanaError) as ctx:
            client.request("GET", "/api/health")
        self.assertNotIn(password, repr(client))
        self.assertNotIn(password, str(ctx.exception))
        self.assertNotIn(password, repr(ctx.exception))

    def test_sni_connection_uses_the_site_hostname(self) -> None:
        class Context:
            def __init__(self) -> None:
                self.server_hostname: str | None = None

            def wrap_socket(self, sock: object, *, server_hostname: str) -> object:
                self.server_hostname = server_hostname
                return sock

        context = Context()
        connection = SniHTTPSConnection(
            "127.0.0.1",
            443,
            context=context,  # type: ignore[arg-type]
            server_hostname="grafana.example.invalid",
            timeout=1,
        )
        connection.sock = object()
        with patch.object(http.client.HTTPConnection, "connect"):
            connection.connect()
        self.assertEqual(context.server_hostname, "grafana.example.invalid")


class WaitReady(unittest.TestCase):
    def test_wait_is_bounded_and_sleep_is_injectable(self) -> None:
        class Down:
            def request(self, method: str, path: str) -> Response:
                return Response(503)

        slept: list[float] = []
        result = wait_ready(Down(), attempts=3, sleep=slept.append)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertEqual(len(slept), 2)
        self.assertIn("503", result.problem or "")

    def test_transport_failure_is_fix_bearing(self) -> None:
        class Down:
            def request(self, method: str, path: str) -> Response:
                raise GrafanaError("Grafana request failed for /api/health.")

        result = wait_ready(Down(), attempts=2, sleep=lambda _: None)  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("then retry", result.fix)


if __name__ == "__main__":
    unittest.main()
