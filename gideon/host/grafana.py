"""The stdlib Grafana health client used through the HTTPS ingress."""

import base64
import http.client
import json
import os
import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote, urlsplit

from gideon.host.ingress import SniHTTPSConnection
from gideon.host.sysio import PathLike

_DEFAULT_TIMEOUT: Final[float] = 15.0
_READY_SLEEP_SECONDS: Final[float] = 5.0
_RETRY_FIX: Final[str] = "Check Grafana availability, then retry."
_APPLY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_HEALTH_PATH: Final[str] = "/api/health"
_RECEIVERS_NAMESPACE: Final[str] = "default"
_RECEIVERS_PATH: Final[str] = (
    "/apis/notifications.alerting.grafana.app/v1beta1/"
    f"namespaces/{_RECEIVERS_NAMESPACE}/receivers"
)
_ALERTS_PATH: Final[str] = "/api/alertmanager/grafana/api/v2/alerts"
_CONTACT_TEST_FIX: Final[str] = (
    "Run sudo python3 -m gideon preflight, then correct "
    "/etc/gideon/secrets/smtp_password."
)
_SMTP_CODE = re.compile(r"(?<!\d)(?:[245]\d{2})(?!\d)")


class GrafanaError(Exception):
    """A safe, fix-bearing Grafana failure."""

    def __init__(self, problem: str, fix: str = _RETRY_FIX) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class Response:
    """An HTTP response whose body is excluded from repr output."""

    status: int
    body: object | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Receiver:
    """A Grafana notification receiver and its stored integrations."""

    uid: str
    title: str
    integrations: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert instance reported by Grafana's Alertmanager API."""

    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    starts_at: str
    state: str
    silenced: bool


def _string_pairs(mapping: Mapping[object, object]) -> dict[str, str]:
    return {key: value for key, value in mapping.items() if isinstance(key, str) and isinstance(value, str)}


class Client:
    """A small JSON client for Grafana's API through the ingress."""

    def __init__(
        self,
        base_url: str,
        *,
        ca_path: PathLike | None = None,
        server_hostname: str | None = None,
        credential: tuple[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise GrafanaError(
                "Grafana base URL is invalid.",
                "Correct the Grafana URL, then retry.",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise GrafanaError(
                "Grafana base URL is invalid.",
                "Use an http:// or https:// Grafana URL, then retry.",
            )
        if parsed.username is not None or parsed.password is not None:
            raise GrafanaError(
                "Grafana base URL must not contain credentials.",
                "Remove credentials from the Grafana URL, then retry.",
            )
        if parsed.query or parsed.fragment:
            raise GrafanaError(
                "Grafana base URL must not contain a query or fragment.",
                "Use only the Grafana origin and path, then retry.",
            )
        if timeout <= 0:
            raise GrafanaError(
                "Grafana timeout must be positive.",
                "Set a positive Grafana timeout, then retry.",
            )
        if credential is not None and len(credential) != 2:
            raise GrafanaError(
                "Grafana basic authentication is invalid.",
                "Correct the Grafana administrator credential, then retry.",
            )

        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._timeout = timeout
        self._credential = credential
        self._context: ssl.SSLContext | None = None
        self._server_hostname = server_hostname
        if self._scheme == "https":
            try:
                self._context = ssl.create_default_context(
                    cafile=os.fspath(ca_path) if ca_path is not None else None
                )
            except OSError as exc:
                raise GrafanaError(
                    "Grafana CA file is unreadable.",
                    "Correct the Grafana CA file, then retry.",
                ) from exc
            self._server_hostname = server_hostname or self._host

    def _target_path(self, path: str) -> str:
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise GrafanaError(
                "Grafana request path is invalid.",
                "Correct the Grafana health path, then retry.",
            )
        return f"{self._base_path}{path}" or "/"

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            if self._context is None or self._server_hostname is None:
                raise GrafanaError(
                    "Grafana HTTPS is not configured.",
                    "Correct the Grafana TLS settings, then retry.",
                )
            return SniHTTPSConnection(
                self._host,
                self._port,
                context=self._context,
                server_hostname=self._server_hostname,
                timeout=self._timeout,
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send a JSON request, returning HTTP status failures without raising."""

        request_headers = {"Accept": "application/json"}
        if headers is not None:
            request_headers.update(headers)
        if self._server_hostname is not None:
            request_headers["Host"] = self._server_hostname
        if self._credential is not None:
            username, password = self._credential
            value = base64.b64encode(f"{username}:{password}".encode())
            request_headers["Authorization"] = f"Basic {value.decode('ascii')}"
        payload: bytes | None = None
        if body is not None:
            try:
                payload = json.dumps(body).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise GrafanaError(
                    "Grafana request body is not valid JSON.",
                    "Correct the Grafana request, then retry.",
                ) from exc
            request_headers.setdefault("Content-Type", "application/json")
        target = self._target_path(path)
        connection = self._connection()
        try:
            connection.request(method, target, body=payload, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise GrafanaError(f"Grafana request failed for {path}.") from exc
        finally:
            connection.close()

        if not raw:
            return Response(status)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if 200 <= status < 300:
                raise GrafanaError(f"Grafana returned invalid JSON for {path}.") from exc
            return Response(status)
        return Response(status, parsed)

    def get_receiver(self, title: str) -> Receiver | None:
        """Read one provisioned receiver by its display title."""

        response = self.request("GET", _RECEIVERS_PATH)
        if not 200 <= response.status < 300:
            raise GrafanaError(
                f"Grafana receiver lookup returned HTTP {response.status}.",
                _APPLY_FIX,
            )
        if not isinstance(response.body, Mapping):
            return None
        items = response.body.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata")
            spec = item.get("spec")
            if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                continue
            uid = metadata.get("name")
            receiver_title = spec.get("title")
            integrations = spec.get("integrations")
            if not isinstance(uid, str) or not isinstance(receiver_title, str):
                continue
            if receiver_title != title or not isinstance(integrations, list):
                continue
            stored = tuple(
                integration
                for integration in integrations
                if isinstance(integration, Mapping)
            )
            return Receiver(uid, receiver_title, stored)
        return None

    def get_alerts(self) -> tuple[Alert, ...]:
        """Read the alert instances currently held by Grafana's Alertmanager."""

        response = self.request("GET", _ALERTS_PATH)
        if not 200 <= response.status < 300:
            raise GrafanaError(
                f"Grafana alert lookup returned HTTP {response.status}.",
                _APPLY_FIX,
            )
        if not isinstance(response.body, list):
            raise GrafanaError(
                "Grafana returned an invalid alert list.",
                _APPLY_FIX,
            )

        alerts: list[Alert] = []
        for item in response.body:
            if not isinstance(item, Mapping):
                continue
            labels = item.get("labels")
            annotations = item.get("annotations")
            starts_at = item.get("startsAt")
            status = item.get("status")
            if (
                not isinstance(labels, Mapping)
                or not isinstance(annotations, Mapping)
                or not isinstance(status, Mapping)
            ):
                continue
            state = status.get("state")
            silenced_by = status.get("silencedBy")
            alerts.append(
                Alert(
                    _string_pairs(labels),
                    _string_pairs(annotations),
                    starts_at if isinstance(starts_at, str) else "",
                    state if isinstance(state, str) else "",
                    isinstance(silenced_by, list) and bool(silenced_by),
                )
            )
        return tuple(alerts)

    @staticmethod
    def _redact_recipients(text: str, recipients: Sequence[str]) -> str:
        redacted = text
        for recipient in recipients:
            if recipient:
                redacted = redacted.replace(recipient, "<recipient>")
        return redacted

    def test_receiver(
        self,
        receiver: Receiver,
        integration: Mapping[str, object],
        *,
        recipients: Sequence[str] = (),
    ) -> "TestResult":
        """Ask Grafana to exercise one stored receiver integration."""

        if not isinstance(integration.get("version"), str):
            raise GrafanaError(
                "Grafana receiver integration has no version.",
                "Run sudo python3 -m gideon apply, then retry.",
            )
        settings = integration.get("settings", {})
        if not isinstance(settings, Mapping):
            settings = {}
        secure_fields = integration.get("secureFields", {})
        if not isinstance(secure_fields, Mapping):
            secure_fields = {}
        config = {
            "uid": str(integration.get("uid", "")),
            "type": str(integration.get("type", "")),
            "version": integration["version"],
            "disableResolveMessage": integration.get("disableResolveMessage", False),
            "settings": dict(settings),
            "secureFields": dict(secure_fields),
        }
        body = {
            "integration": config,
            "alert": {
                "labels": {"alertname": "GideonAlertsTest", "class": "page"},
                "annotations": {"summary": "gideon alerts test"},
            },
        }
        response = self.request(
            "POST",
            f"{_RECEIVERS_PATH}/{quote(receiver.uid, safe='')}/test",
            body,
            headers={"Request-Timeout": "30"},
        )
        status: str | None = None
        error: str | None = None
        if isinstance(response.body, Mapping):
            value = response.body.get("status")
            if isinstance(value, str):
                status = value
            for key in ("error", "message"):
                value = response.body.get(key)
                if isinstance(value, str) and value:
                    error = value
                    break
        error_text = self._redact_recipients(
            error or "Grafana receiver test failed.",
            recipients,
        )
        smtp_match = _SMTP_CODE.search(error_text)
        smtp_code = None if smtp_match is None else int(smtp_match.group(0))
        ok = 200 <= response.status < 300 and status == "success"
        return TestResult(
            ok=ok,
            status_code=response.status,
            problem=None if ok else error_text,
            fix="" if ok else _CONTACT_TEST_FIX,
            smtp_code=smtp_code,
        )


@dataclass(frozen=True, slots=True)
class TestResult:
    """The bounded result of a Grafana receiver test."""

    ok: bool
    status_code: int
    problem: str | None = None
    fix: str = ""
    smtp_code: int | None = None


@dataclass(frozen=True, slots=True)
class ReadyResult:
    """The bounded Grafana readiness result."""

    ok: bool
    problem: str | None = None
    fix: str = ""


def wait_ready(
    client: Client,
    *,
    attempts: int,
    sleep: Callable[[float], None],
) -> ReadyResult:
    """Poll Grafana's health endpoint without escaping startup failures."""

    if attempts <= 0:
        return ReadyResult(
            False,
            "Grafana readiness was not attempted.",
            _RETRY_FIX,
        )
    last_problem = "Grafana is not ready."
    last_fix = _RETRY_FIX
    for attempt in range(attempts):
        try:
            response = client.request("GET", _HEALTH_PATH)
            if 200 <= response.status < 300:
                return ReadyResult(True)
            last_problem = f"Grafana health endpoint returned HTTP {response.status}."
            last_fix = _RETRY_FIX
        except (GrafanaError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, GrafanaError):
                last_problem, last_fix = exc.problem, exc.fix
            else:
                last_problem = "Grafana readiness request failed."
                last_fix = _RETRY_FIX
        if attempt + 1 < attempts:
            sleep(_READY_SLEEP_SECONDS)
    return ReadyResult(False, last_problem, last_fix)


def ingress_client_factory(
    hostname: str, *, ca_path: PathLike
) -> Callable[..., Client]:
    """Build clients that reach Grafana through Caddy on loopback."""

    def factory(*, credential: tuple[str, str] | None = None) -> Client:
        return Client(
            "https://127.0.0.1:443/grafana",
            ca_path=ca_path,
            server_hostname=hostname,
            credential=credential,
        )

    return factory
