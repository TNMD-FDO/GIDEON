"""The stdlib Open WebUI client and its idempotent bootstrap.

The frontend is reached over the network, so this module deliberately does
not use the host-I/O seam for HTTP.  Secret files and the manifest still go
through :class:`gideon.host.sysio.Host`; credentials never appear in command
arguments, diagnostics, or object representations.  Apply, users reconcile,
and the turn harness are its callers; slice 2's eval command will use the same
streaming path.
"""

import contextlib
import http.client
import json
import os
import socket
import ssl
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, cast
from urllib.parse import quote, urlsplit

import yaml  # type: ignore[import-untyped]

from gideon.host import stack
from gideon.host.ingress import SniHTTPSConnection
from gideon.host.render.owui import BREAK_GLASS, EVAL_IDENTITY, EVAL_PASSWORD_SECRET
from gideon.host.secrets import SecretReadResult, read_secret, secret_path, write_secret
from gideon.host.sysio import Host, PathLike

_DEFAULT_TIMEOUT: Final[float] = 15.0
_READY_SLEEP_SECONDS: Final[float] = 5.0
_RETRY_FIX: Final[str] = "Check Open WebUI availability, then retry."
_VALID_ROLES: Final[frozenset[str]] = frozenset({"admin", "user", "pending"})
_FUNCTIONS_PATH: Final[str] = "/api/v1/functions/"
# The bare /api/v1/models/ path is served by the single-page app; the JSON
# listing is /list, paginated like knowledge — and it holds preset rows only.
# A base model's own row is listed by /base, unpaged and admin-only; the
# removal report and the read-back need both (docs/research/owui-model-record.md §1.3).
_MODELS_PATH: Final[str] = "/api/v1/models/list"
_BASE_MODELS_PATH: Final[str] = "/api/v1/models/base"
# The live listing every signed-in caller sees.  The frontend's chat route reads
# a per-worker model cache that only this listing refreshes — a browser hits it
# on every page load, an API-path caller never — so a record the sync pushed is
# live for every path only after one read of it (docs/research/
# owui-model-record.md §8.1; slice-1 ticket 14's proof, where the stamp's
# attachment stayed stale for the harness's managed turn until a page loaded).
_LIVE_MODELS_PATH: Final[str] = "/api/models"
_KNOWLEDGE_PATH: Final[str] = "/api/v1/knowledge/"
_MANIFEST_FIX: Final[str] = "Correct /etc/gideon/rendered/open-webui/manifest.yaml, then retry."


class OwuiError(Exception):
    """A safe, fix-bearing Open WebUI failure."""

    def __init__(self, problem: str, fix: str = _RETRY_FIX) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class Response:
    """An HTTP response; the body is excluded from ``repr`` because it can contain a token."""

    status: int
    body: object | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class User:
    """The user fields needed by desired-state reconciliation."""

    id: str
    email: str
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class Group:
    """The group fields exposed by the Open WebUI group API."""

    id: str
    name: str
    permissions: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Knowledge:
    """A knowledge base and its owner."""

    id: str
    name: str
    user_id: str


class Client:
    """A small JSON client for the Open WebUI API."""

    def __init__(
        self,
        base_url: str,
        *,
        ca_path: PathLike | None = None,
        server_hostname: str | None = None,
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise OwuiError(
                "Open WebUI base URL is invalid.",
                "Correct the Open WebUI URL, then retry.",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise OwuiError(
                "Open WebUI base URL is invalid.",
                "Use an http:// or https:// Open WebUI URL, then retry.",
            )
        if parsed.username is not None or parsed.password is not None:
            raise OwuiError(
                "Open WebUI base URL must not contain credentials.",
                "Remove credentials from the Open WebUI URL, then retry.",
            )
        if parsed.query or parsed.fragment:
            raise OwuiError(
                "Open WebUI base URL must not contain a query or fragment.",
                "Use only the Open WebUI origin, then retry.",
            )
        if timeout <= 0:
            raise OwuiError(
                "Open WebUI timeout must be positive.",
                "Set a positive Open WebUI timeout, then retry.",
            )

        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key
        self._token = token
        self._context: ssl.SSLContext | None = None
        # Caddy routes by the HTTP Host header as well as by SNI; the ingress
        # is dialled on loopback, so both must carry the site hostname.
        self._server_hostname: str | None = server_hostname
        if self._scheme == "https":
            try:
                self._context = ssl.create_default_context(
                    cafile=os.fspath(ca_path) if ca_path is not None else None
                )
            except OSError as exc:
                raise OwuiError(
                    "Open WebUI CA file is unreadable.",
                    "Correct the Open WebUI CA file, then retry.",
                ) from exc
            self._server_hostname = server_hostname or self._host

    def _target_path(self, path: str) -> str:
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise OwuiError(
                "Open WebUI request path is invalid.",
                "Correct the requested endpoint, then retry.",
            )
        return f"{self._base_path}{path}" or "/"

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            if self._context is None or self._server_hostname is None:
                raise OwuiError(
                    "Open WebUI HTTPS is not configured.",
                    "Correct the Open WebUI TLS settings, then retry.",
                )
            return SniHTTPSConnection(
                self._host,
                self._port,
                context=self._context,
                server_hostname=self._server_hostname,
                timeout=self._timeout,
            )
        return http.client.HTTPConnection(
            self._host, self._port, timeout=self._timeout
        )

    def _prepare(
        self, path: str, body: object | None, accept: str
    ) -> tuple[bytes | None, dict[str, str], str]:
        """One request's parts: the JSON body, the headers (Host, the credential), the target."""

        try:
            encoded = None if body is None else json.dumps(body).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise OwuiError(
                f"Open WebUI request body for {path} is not valid JSON.",
                "Correct the Open WebUI request data, then retry.",
            ) from exc
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if self._server_hostname is not None:
            headers["Host"] = self._server_hostname
        credential = self._api_key if self._api_key is not None else self._token
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        return encoded, headers, self._target_path(path)

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        """Send one JSON request and return its status without raising for HTTP errors."""

        encoded, headers, target = self._prepare(path, body, "application/json")
        try:
            connection = self._connection()
        except OwuiError:
            raise
        except OSError as exc:
            raise OwuiError(
                f"Open WebUI request failed for {path}.",
                _RETRY_FIX,
            ) from exc
        try:
            connection.request(method, target, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise OwuiError(
                f"Open WebUI request failed for {path}.",
                _RETRY_FIX,
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                connection.close()

        if not raw:
            return Response(status)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OwuiError(
                f"Open WebUI returned invalid JSON for {path}.",
                _RETRY_FIX,
            ) from exc
        return Response(status, decoded)

    def stream(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Iterator[str]:
        """Yield each SSE ``data:`` payload until ``[DONE]``, which is required.

        A body that ends first, a non-event-stream content type, or a failed
        read is a refusal, so a partial stream is never taken for a whole one.
        *deadline* (a monotonic instant) bounds every read: the time remaining,
        never above the client's timeout, is the socket timeout for that read.
        The connection is closed when iteration ends, early or not.
        """

        encoded, headers, target = self._prepare(path, body, "text/event-stream")
        connection: http.client.HTTPConnection | None = None
        try:
            try:
                connection = self._connection()
            except OwuiError:
                raise
            except OSError as exc:
                raise OwuiError(
                    f"Open WebUI request failed for {path}.",
                    _RETRY_FIX,
                ) from exc

            try:
                connection.request(method, target, body=encoded, headers=headers)
                self._arm_deadline(connection.sock, deadline, monotonic)
                response = connection.getresponse()
                if response.status < 200 or response.status >= 300:
                    self._arm_deadline(_response_socket(response, connection), deadline, monotonic)
                    response.read()
                    raise OwuiError(
                        f"Open WebUI {path} returned HTTP {response.status}.",
                        _RETRY_FIX,
                    )
                content_type = response.getheader("Content-Type") or ""
                media_type = content_type.partition(";")[0].strip().casefold()
                if media_type != "text/event-stream":
                    raise OwuiError(
                        f"Open WebUI stream for {path} returned an invalid content type.",
                        _RETRY_FIX,
                    )
                while True:
                    self._arm_deadline(_response_socket(response, connection), deadline, monotonic)
                    line = response.readline()
                    if not line:
                        raise OwuiError(
                            f"Open WebUI stream for {path} ended before [DONE].",
                            _RETRY_FIX,
                        )
                    try:
                        text = line.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError as exc:
                        raise OwuiError(
                            f"Open WebUI stream for {path} could not be decoded.",
                            _RETRY_FIX,
                        ) from exc
                    if not text or text.startswith(":") or not text.startswith("data:"):
                        continue
                    payload = text.removeprefix("data:").removeprefix(" ")
                    if payload == "[DONE]":
                        return
                    yield payload
            except OwuiError:
                raise
            except TimeoutError as exc:
                raise OwuiError(
                    f"Open WebUI stream for {path} timed out.",
                    _RETRY_FIX,
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise OwuiError(
                    f"Open WebUI stream request failed for {path}.",
                    _RETRY_FIX,
                ) from exc
        finally:
            if connection is not None:
                with contextlib.suppress(OSError):
                    connection.close()

    def _arm_deadline(
        self, sock: socket.socket | None, deadline: float | None, monotonic: Callable[[], float]
    ) -> None:
        """Bound the next read by *deadline*: the time remaining, never above the client's timeout."""

        if deadline is None:
            return
        remaining = min(self._timeout, deadline - monotonic())
        if remaining <= 0:
            raise OwuiError("Open WebUI stream timed out.", _RETRY_FIX)
        if sock is None:
            raise OwuiError("Open WebUI stream socket is unavailable.", _RETRY_FIX)
        sock.settimeout(remaining)

    def _checked(self, method: str, path: str, body: object | None = None) -> object | None:
        response = self.request(method, path, body)
        return _require_success(response, path)

    def ready(self) -> bool:
        """Return the frontend readiness flag."""

        body = _as_mapping(self._checked("GET", "/ready"), "/ready")
        status = body.get("status")
        if not isinstance(status, bool):
            raise _invalid_response("/ready")
        return status

    def signin(self, email: str, password: str) -> str:
        """Sign in and return the session token without retaining the password."""

        body = _as_mapping(
            self._checked(
                "POST", "/api/v1/auths/signin", {"email": email, "password": password}
            ),
            "/api/v1/auths/signin",
        )
        token = _required_text(body, "token", "/api/v1/auths/signin")
        return token

    def mint_api_key(self) -> str:
        """Mint an API key using the session credential on this client."""

        if self._api_key is None and self._token is None:
            raise OwuiError(
                "Open WebUI API-key minting requires a session.",
                "Sign in to Open WebUI, then retry.",
            )
        body = _as_mapping(
            self._checked("POST", "/api/v1/auths/api_key"),
            "/api/v1/auths/api_key",
        )
        return _required_text(body, "api_key", "/api/v1/auths/api_key")

    def users_all(self) -> tuple[User, ...]:
        body = _as_mapping(self._checked("GET", "/api/v1/users/all"), "/api/v1/users/all")
        values = _as_list(body.get("users"), "/api/v1/users/all")
        return tuple(_user(value, "/api/v1/users/all") for value in values)

    def update_role(self, user_id: str, role: str) -> User:
        _validate_role(role)
        path = f"/api/v1/users/{user_id}/update"
        return _user(
            _as_mapping(self._checked("POST", path, {"role": role}), path), path
        )

    def add_user(self, name: str, email: str, password: str, role: str) -> User:
        _validate_role(role)
        path = "/api/v1/auths/add"
        return _user(
            _as_mapping(
                self._checked(
                    "POST",
                    path,
                    {"name": name, "email": email, "password": password, "role": role},
                ),
                path,
            ),
            path,
        )

    def groups(self) -> tuple[Group, ...]:
        path = "/api/v1/groups/"
        values = _as_list(self._checked("GET", path), path)
        return tuple(_group(value, path) for value in values)

    def group_members(self, group_id: str) -> tuple[str, ...]:
        path = f"/api/v1/groups/id/{group_id}/users"
        values = _as_list(self._checked("POST", path), path)
        member_ids: list[str] = []
        for value in values:
            member = _as_mapping(value, path)
            member_ids.append(_required_text(member, "id", path))
        return tuple(member_ids)

    def create_group(
        self, name: str, permissions: Mapping[str, object]
    ) -> Group:
        path = "/api/v1/groups/create"
        return _group(
            _as_mapping(
                self._checked(
                    "POST",
                    path,
                    {"name": name, "description": "", "permissions": permissions},
                ),
                path,
            ),
            path,
        )

    def update_group(
        self, group_id: str, name: str, permissions: Mapping[str, object]
    ) -> Group:
        path = f"/api/v1/groups/id/{group_id}/update"
        return _group(
            _as_mapping(
                self._checked(
                    "POST",
                    path,
                    {"name": name, "description": "", "permissions": permissions},
                ),
                path,
            ),
            path,
        )

    def delete_group(self, group_id: str) -> None:
        path = f"/api/v1/groups/id/{group_id}/delete"
        self._checked("DELETE", path)

    def add_group_users(self, group_id: str, ids: Sequence[str]) -> Group:
        path = f"/api/v1/groups/id/{group_id}/users/add"
        return _group(
            _as_mapping(
                self._checked("POST", path, {"user_ids": list(ids)}), path
            ),
            path,
        )

    def remove_group_users(self, group_id: str, ids: Sequence[str]) -> Group:
        path = f"/api/v1/groups/id/{group_id}/users/remove"
        return _group(
            _as_mapping(
                self._checked("POST", path, {"user_ids": list(ids)}), path
            ),
            path,
        )

    def functions_sync(self, functions: Sequence[Mapping[str, object]]) -> None:
        path = "/api/v1/functions/sync"
        self._checked("POST", path, {"functions": list(functions)})

    def function_by_id(self, identifier: str) -> Mapping[str, object] | None:
        """Return one Function row, or None when the row endpoint is empty."""

        path = f"/api/v1/functions/id/{quote(identifier, safe='')}"
        value = self._checked("GET", path)
        if value is None:
            return None
        return _as_mapping(value, path)

    def function_valves(self, identifier: str) -> Mapping[str, object]:
        """Return one Function's valves, treating an empty response as no valves."""

        path = f"/api/v1/functions/id/{quote(identifier, safe='')}/valves"
        value = self._checked("GET", path)
        if value is None:
            return {}
        return _as_mapping(value, path)

    def models_sync(self, models: Sequence[Mapping[str, object]]) -> None:
        path = "/api/v1/models/sync"
        self._checked("POST", path, {"models": list(models)})

    def paged_items(self, path: str) -> tuple[Mapping[str, object], ...]:
        """Every item of a ``{items, total}`` listing, walking pages until one is empty."""

        items: list[Mapping[str, object]] = []
        page = 1
        while True:
            body = _as_mapping(self._checked("GET", f"{path}?page={page}"), path)
            page_items = _as_list(body.get("items"), path)
            if not page_items:
                return tuple(items)
            items.extend(_as_mapping(value, path) for value in page_items)
            page += 1

    def knowledge_all(self) -> tuple[Knowledge, ...]:
        return tuple(
            Knowledge(
                id=_required_text(item, "id", _KNOWLEDGE_PATH),
                name=_required_text(item, "name", _KNOWLEDGE_PATH),
                user_id=_required_text(item, "user_id", _KNOWLEDGE_PATH),
            )
            for item in self.paged_items(_KNOWLEDGE_PATH)
        )


def _response_socket(
    response: http.client.HTTPResponse, connection: http.client.HTTPConnection
) -> socket.socket | None:
    """The socket a response reads from.

    The connection keeps it while the response may be followed by another;
    on a closing response ``http.client`` drops its reference and only the
    response's file object still holds the socket.
    """

    if connection.sock is not None:
        return connection.sock
    raw = getattr(response.fp, "raw", None)
    sock = getattr(raw, "_sock", None) or getattr(response.fp, "_sock", None)
    return cast(socket.socket | None, sock)


@dataclass(frozen=True, slots=True)
class ReadyResult:
    """The bounded readiness probe result."""

    ok: bool
    problem: str | None = None
    fix: str = ""


def wait_ready(
    client: Client,
    *,
    attempts: int,
    sleep: Callable[[float], None],
) -> ReadyResult:
    """Poll ``/ready`` without allowing startup failures to escape as tracebacks."""

    if attempts <= 0:
        return ReadyResult(
            False,
            "Open WebUI readiness was not attempted.",
            _RETRY_FIX,
        )
    last_problem = "Open WebUI is not ready."
    last_fix = _RETRY_FIX
    for attempt in range(attempts):
        try:
            path = "/ready"
            response = client.request("GET", path)
            body = _as_mapping(_require_success(response, path), path)
            status = body.get("status")
            if not isinstance(status, bool):
                raise _invalid_response(path)
            if status:
                return ReadyResult(True)
        except (OwuiError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, OwuiError):
                last_problem, last_fix = exc.problem, exc.fix
            else:
                last_problem = "Open WebUI readiness request failed."
                last_fix = _RETRY_FIX
        if attempt + 1 < attempts:
            sleep(_READY_SLEEP_SECONDS)
    return ReadyResult(False, last_problem, last_fix)


def load_manifest(io: Host, path: PathLike) -> Mapping[str, object]:
    """Read and parse a rendered Open WebUI manifest through the host seam."""

    try:
        text = io.read_text(path)
        document = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(
            f"Open WebUI manifest is unreadable: {path}."
        ) from exc
    if not isinstance(document, Mapping) or any(
        not isinstance(key, str) for key in document
    ):
        raise ValueError(f"Open WebUI manifest must be a mapping: {path}.")
    return cast(Mapping[str, object], document)


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """The idempotent frontend desired-state changes and any refusal."""

    created_groups: tuple[str, ...] = ()
    updated_groups: tuple[str, ...] = ()
    removed_groups: tuple[str, ...] = ()
    removed_functions: tuple[str, ...] = ()
    removed_models: tuple[str, ...] = ()
    minted: tuple[str, ...] = ()
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True, slots=True)
class _DesiredGroup:
    name: str
    membership: str
    members: tuple[str, ...]
    permissions: Mapping[str, object]


def _validate_role(role: str) -> None:
    """The server stores any role string; only these three mean anything to it."""

    if role not in _VALID_ROLES:
        raise OwuiError(
            "Open WebUI role is invalid.",
            "Use admin, user, or pending as the role, then retry.",
        )


def _invalid_response(path: str) -> OwuiError:
    return OwuiError(f"Open WebUI returned an invalid response for {path}.")


def _require_success(response: Response, path: str) -> object | None:
    if response.status < 200 or response.status >= 300:
        raise OwuiError(f"Open WebUI {path} returned HTTP {response.status}.")
    return response.body


def _as_mapping(value: object | None, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise _invalid_response(path)
    return cast(Mapping[str, object], value)


def _as_list(value: object | None, path: str) -> list[object]:
    if not isinstance(value, list):
        raise _invalid_response(path)
    return value


def _required_text(value: Mapping[str, object], key: str, path: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise _invalid_response(path)
    return result


def _user(value: object, path: str) -> User:
    item = _as_mapping(value, path)
    return User(
        id=_required_text(item, "id", path),
        email=_required_text(item, "email", path),
        name=_required_text(item, "name", path),
        role=_required_text(item, "role", path),
    )


def _group(value: object, path: str) -> Group:
    item = _as_mapping(value, path)
    permissions = item.get("permissions", {})
    permissions_mapping = _as_mapping(permissions, path)
    return Group(
        id=_required_text(item, "id", path),
        name=_required_text(item, "name", path),
        permissions=dict(permissions_mapping),
    )


def _logs_fix(rendered_dir: PathLike) -> str:
    return stack.logs_fix(rendered_dir, "open-webui")


def loopback_client_factory(
    base_url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Callable[..., Client]:
    """A plain-HTTP factory for a frontend published on loopback, with no ingress.

    The ``gideon-ci`` sibling's frontend has no Caddy and no certificate: its
    tool and the turn harness under ``--stack ci`` reach it at *base_url*.
    """

    def factory(*, api_key: str | None = None, token: str | None = None) -> Client:
        return Client(base_url, api_key=api_key, token=token, timeout=timeout)

    return factory


def ingress_client_factory(
    hostname: str,
    *,
    ca_path: PathLike | None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Callable[..., Client]:
    """The production factory: the ingress on loopback, SNI and verification for *hostname*.

    Apply and reconcile reach the frontend the way users do — through Caddy
    with the office CA — so a working sign-in and a working push share a path.
    """

    def factory(*, api_key: str | None = None, token: str | None = None) -> Client:
        return Client(
            "https://127.0.0.1:443",
            ca_path=ca_path,
            server_hostname=hostname,
            api_key=api_key,
            token=token,
            timeout=timeout,
        )

    return factory


def _secret_failure(result: SecretReadResult, name: str) -> OwuiError:
    return OwuiError(
        result.problem or f"Secret file is unavailable: {secret_path(name)}.",
        result.fix or f"Correct {secret_path(name)}, then retry.",
    )


def _manifest_groups(manifest: Mapping[str, object]) -> tuple[_DesiredGroup, ...]:
    raw_groups = manifest.get("groups")
    if not isinstance(raw_groups, list):
        raise OwuiError(
            "Open WebUI manifest groups are invalid.",
            _MANIFEST_FIX,
        )
    groups: list[_DesiredGroup] = []
    names: set[str] = set()
    for raw_group in raw_groups:
        group = _as_mapping(raw_group, "/etc/gideon/rendered/open-webui/manifest.yaml")
        name = group.get("name")
        membership = group.get("membership")
        permissions = group.get("permissions")
        members = group.get("members", [])
        if (
            not isinstance(name, str)
            or not isinstance(membership, str)
            or not isinstance(permissions, Mapping)
            or not isinstance(members, list)
            or any(not isinstance(member, str) for member in members)
            or name in names
        ):
            raise OwuiError(
                "Open WebUI manifest groups are invalid.",
                _MANIFEST_FIX,
            )
        names.add(name)
        groups.append(
            _DesiredGroup(
                name=name,
                membership=membership,
                members=tuple(cast(list[str], members)),
                permissions=cast(Mapping[str, object], permissions),
            )
        )
    return tuple(groups)


def _manifest_payload(
    manifest: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    value = manifest.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OwuiError(
            f"Open WebUI manifest {key} are invalid.",
            _MANIFEST_FIX,
        )
    return tuple(cast(Mapping[str, object], item) for item in value)


def _permissions_match(
    desired: Mapping[str, object], observed: Mapping[str, object]
) -> bool:
    for key, wanted in desired.items():
        if key not in observed:
            return False
        actual = observed[key]
        if isinstance(wanted, Mapping):
            if not isinstance(actual, Mapping) or not _permissions_match(
                cast(Mapping[str, object], wanted), cast(Mapping[str, object], actual)
            ):
                return False
        elif actual != wanted:
            return False
    return True


def _payload_ids(payload: Sequence[Mapping[str, object]], key: str) -> frozenset[str]:
    identifiers: set[str] = set()
    for item in payload:
        identifier = item.get("id")
        if identifier is not None:
            if not isinstance(identifier, str):
                raise OwuiError(
                    f"Open WebUI manifest {key} has an invalid id.",
                    _MANIFEST_FIX,
                )
            identifiers.add(identifier)
    return frozenset(identifiers)


def _listed_rows(client: Client, path: str, *, paged: bool) -> tuple[Mapping[str, object], ...]:
    """The rows the server currently holds at a plain-list or paged listing, each carrying an id."""

    if paged:
        items = client.paged_items(path)
    else:
        values = _as_list(_require_success(client.request("GET", path), path), path)
        items = tuple(_as_mapping(value, path) for value in values)
    for item in items:
        _required_text(item, "id", path)
    return items


def _ids(rows: Sequence[Mapping[str, object]], path: str) -> tuple[str, ...]:
    return tuple(_required_text(row, "id", path) for row in rows)


def _listed_models(client: Client) -> tuple[Mapping[str, object], ...]:
    """Every model row the server holds: the paged preset listing, then the base rows."""

    return (
        *_listed_rows(client, _MODELS_PATH, paged=True),
        *_listed_rows(client, _BASE_MODELS_PATH, paged=False),
    )


def _live_attachment_difference(
    desired: Mapping[str, object], live: Mapping[str, object]
) -> str | None:
    """The live listing's entry disagreeing with the manifest's attachment key, or None.

    The listing carries the record under ``info`` with its ``meta``; the
    per-model Filter attachment is the one key a turn's path reads from it
    (docs/research/owui-filter-function.md §3.2).  Names the field, never a value.
    """

    identifier = desired.get("id")
    label = identifier if isinstance(identifier, str) else "unknown"
    desired_meta = desired.get("meta")
    wanted = desired_meta.get("filterIds") if isinstance(desired_meta, Mapping) else None
    info = live.get("info")
    live_meta = info.get("meta") if isinstance(info, Mapping) else None
    observed = live_meta.get("filterIds") if isinstance(live_meta, Mapping) else None
    if (wanted or None) != (observed or None):
        return f"model {label} live entry field meta.filterIds differs."
    return None


def _refresh_live_models(client: Client, desired_by_id: Mapping[str, Mapping[str, object]]) -> None:
    """Read the live model listing so the frontend's cache holds the pushed records.

    Every manifest model the listing shows is held to its attachment key; a
    model the listing omits — the base model until the engine answers, and a
    preset over an undiscovered base — is verify's concern, not this read's.
    """

    body = _as_mapping(
        _require_success(client.request("GET", _LIVE_MODELS_PATH), _LIVE_MODELS_PATH),
        _LIVE_MODELS_PATH,
    )
    for value in _as_list(body.get("data"), _LIVE_MODELS_PATH):
        entry = _as_mapping(value, _LIVE_MODELS_PATH)
        desired = desired_by_id.get(_required_text(entry, "id", _LIVE_MODELS_PATH))
        if desired is None:
            continue
        difference = _live_attachment_difference(desired, entry)
        if difference is not None:
            raise OwuiError(f"Open WebUI live model listing: {difference}")


def _read_back(kind: str, listed: Sequence[str], desired: frozenset[str]) -> None:
    """Refuse unless a sync's read-back holds exactly the manifest's ids.

    The pinned sync routes answer a swallowed persistence failure with HTTP
    200 and an empty list (docs/research/owui-model-record.md §1.2), so the
    listing after the push, never the status, is the proof.
    """

    differing = sorted(desired.symmetric_difference(listed))
    if differing:
        raise OwuiError(f"Open WebUI {kind} sync read-back differs by id: {differing[0]}.")


def _grant_triples(value: object) -> frozenset[tuple[str, str, str]] | None:
    """A grant list as its (principal_type, principal_id, permission) triples; None if malformed."""

    if not isinstance(value, list):
        return None
    triples: set[tuple[str, str, str]] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            return None
        triple = tuple(entry.get(key) for key in ("principal_type", "principal_id", "permission"))
        if not all(isinstance(part, str) for part in triple):
            return None
        triples.add(cast(tuple[str, str, str], triple))
    return frozenset(triples)


def _model_difference(
    desired: Mapping[str, object], observed: Mapping[str, object]
) -> str | None:
    """The first manifest-owned field of a model row that differs from its manifest entry, or None.

    Whole-mapping equality for ``params`` and ``meta.capabilities``; every
    other ``meta`` key equal where the manifest carries it and absent or null
    where it does not (the pinned row model materializes its declared keys as
    null); grants as triples; the server-owned ``user_id``, ``updated_at``,
    and ``created_at`` never compared.  Names the field, never a value.
    """

    identifier = desired.get("id")
    label = identifier if isinstance(identifier, str) else "unknown"
    for key in ("id", "name", "base_model_id", "is_active"):
        if key not in observed or desired.get(key) != observed[key]:
            return f"model {label} field {key} differs."

    desired_params = desired.get("params")
    observed_params = observed.get("params")
    if (
        not isinstance(desired_params, Mapping)
        or not isinstance(observed_params, Mapping)
        or desired_params != observed_params
    ):
        return f"model {label} field params differs."

    desired_meta = desired.get("meta")
    observed_meta = observed.get("meta")
    if not isinstance(desired_meta, Mapping) or not isinstance(observed_meta, Mapping):
        return f"model {label} field meta differs."
    desired_capabilities = desired_meta.get("capabilities")
    observed_capabilities = observed_meta.get("capabilities")
    if (
        not isinstance(desired_capabilities, Mapping)
        or not isinstance(observed_capabilities, Mapping)
        or desired_capabilities != observed_capabilities
    ):
        return f"model {label} field meta.capabilities differs."
    for key, wanted in desired_meta.items():
        if key != "capabilities" and (key not in observed_meta or observed_meta[key] != wanted):
            return f"model {label} field meta.{key} differs."
    for key, actual in observed_meta.items():
        if key not in desired_meta and actual is not None:
            return f"model {label} field meta.{key} differs."
    desired_grants = _grant_triples(desired.get("access_grants"))
    observed_grants = _grant_triples(observed.get("access_grants"))
    if desired_grants is None or observed_grants is None or desired_grants != observed_grants:
        return f"model {label} field access_grants differs."
    return None


def _function_difference(
    desired: Mapping[str, object],
    observed: Mapping[str, object],
    observed_valves: Mapping[str, object],
) -> str | None:
    """Return the first manifest-owned Function field that differs, or None.

    The row read-back overwrites ``user_id`` and ``updated_at`` but preserves
    the manifest's other fields; ``created_at`` is also deliberately outside
    this comparison.  The valves endpoint returns an empty mapping when the
    stored value is absent.  Content is compared byte-for-byte, but the
    diagnostic names only the field so source text never enters a refusal.
    """

    identifier = desired.get("id")
    label = identifier if isinstance(identifier, str) else "unknown"
    for key in ("content", "name", "type", "is_active", "is_global"):
        if key not in observed or desired.get(key) != observed[key]:
            return f"function {label} field {key} differs."

    desired_meta = desired.get("meta")
    observed_meta = observed.get("meta")
    if not isinstance(desired_meta, Mapping) or not isinstance(observed_meta, Mapping):
        return f"function {label} field meta differs."
    for key, wanted in desired_meta.items():
        if key not in observed_meta or observed_meta[key] != wanted:
            return f"function {label} field meta.{key} differs."

    desired_valves = desired.get("valves")
    if desired_valves is None or desired_valves == {}:
        desired_valves = {}
    if not isinstance(desired_valves, Mapping) or desired_valves != observed_valves:
        return f"function {label} field valves differs."
    return None


def _failure(
    report: BootstrapReport, step: str, error: OwuiError, rendered_dir: PathLike
) -> BootstrapReport:
    return replace(
        report,
        problem=f"{step}: {error.problem}",
        fix=_logs_fix(rendered_dir),
    )


def _secret_refusal(
    report: BootstrapReport, step: str, name: str, result: SecretReadResult
) -> BootstrapReport:
    error = _secret_failure(result, name)
    return replace(report, problem=f"{step}: {error.problem}", fix=error.fix)


def _mint_key(
    io: Host,
    client_factory: Callable[..., Client],
    *,
    email: str,
    password_secret: str,
    key_secret: str,
) -> str | OwuiError:
    """Sign in as one identity, mint its API key, and persist it at mode 0440.

    Minting overwrites any key the account held (each account has one), which is
    the intended outcome when the file is gone: the lost key stops working.
    """

    password = read_secret(io, password_secret)
    if not password.ok or password.value is None:
        return _secret_failure(password, password_secret)
    try:
        token = client_factory().signin(email, password.value)
        key = client_factory(token=token).mint_api_key()
    except OwuiError as exc:
        return exc
    problem = write_secret(io, key_secret, key)
    if problem is not None:
        return OwuiError(problem, f"Correct ownership and mode for {secret_path(key_secret)}, then retry.")
    return key


def _ensure_key(
    io: Host,
    client_factory: Callable[..., Client],
    report: BootstrapReport,
    *,
    step: str,
    email: str,
    password_secret: str,
    key_secret: str,
    rendered_dir: PathLike,
) -> tuple[str | None, BootstrapReport]:
    """The identity's API key, minting it only when its secret file is absent."""

    existing = read_secret(io, key_secret)
    if existing.ok and existing.value is not None:
        return existing.value, report
    if not existing.missing:
        return None, _secret_refusal(report, step, key_secret, existing)
    minted = _mint_key(
        io, client_factory, email=email, password_secret=password_secret, key_secret=key_secret
    )
    if isinstance(minted, OwuiError):
        return None, replace(report, problem=f"{step}: {minted.problem}", fix=minted.fix or _logs_fix(rendered_dir))
    return minted, replace(report, minted=report.minted + (key_secret,))


def bootstrap(
    io: Host,
    client_factory: Callable[..., Client],
    manifest: Mapping[str, object],
    *,
    rendered_dir: PathLike,
) -> BootstrapReport:
    """Push the rendered manifest and machine identities into Open WebUI.

    Order matters and is fixed: the admin key (so everything else can be
    called), groups (the eval identity's group must exist before its key can
    be minted), identities, manifest-owned memberships, the eval key, then the
    desired-state syncs.  Function rows are read back by id and valves after
    the Functions sync, model rows are read back by listing and fields, and
    the live model listing is then read so the frontend's per-worker cache
    holds the pushed records for the next turn on any path; a second run
    reports nothing changed.
    """

    report = BootstrapReport()
    try:
        desired_groups = _manifest_groups(manifest)
        functions = _manifest_payload(manifest, "functions")
        models = _manifest_payload(manifest, "models")
    except OwuiError as exc:
        return replace(report, problem=f"manifest: {exc.problem}", fix=exc.fix)
    desired_by_name = {group.name: group for group in desired_groups}

    admin_key, report = _ensure_key(
        io,
        client_factory,
        report,
        step="admin API key",
        email=BREAK_GLASS.email,
        password_secret="gideon_admin_password",
        key_secret="gideon_admin_api_key",
        rendered_dir=rendered_dir,
    )
    if admin_key is None:
        return report
    try:
        client = client_factory(api_key=admin_key)
        users = list(client.users_all())
        observed_groups = client.groups()
    except OwuiError as exc:
        return _failure(report, "groups", exc, rendered_dir)

    # Groups: create or converge permissions; remove what the manifest omits.
    groups_by_name = {group.name: group for group in observed_groups}
    for desired in desired_groups:
        observed = groups_by_name.get(desired.name)
        try:
            if observed is None:
                groups_by_name[desired.name] = client.create_group(desired.name, desired.permissions)
                report = replace(report, created_groups=report.created_groups + (desired.name,))
            elif not _permissions_match(desired.permissions, observed.permissions):
                groups_by_name[desired.name] = client.update_group(
                    observed.id, desired.name, desired.permissions
                )
                report = replace(report, updated_groups=report.updated_groups + (desired.name,))
        except OwuiError as exc:
            return _failure(report, f"group {desired.name}", exc, rendered_dir)
    for observed in observed_groups:
        if observed.name in desired_by_name:
            continue
        try:
            client.delete_group(observed.id)
        except OwuiError as exc:
            return _failure(report, f"remove group {observed.name}", exc, rendered_dir)
        report = replace(report, removed_groups=report.removed_groups + (observed.name,))

    # Identities: the eval account exists with role user (the break-glass admin
    # is seeded by the frontend itself at first boot).
    eval_user = next(
        (user for user in users if user.email.casefold() == EVAL_IDENTITY.email.casefold()), None
    )
    try:
        if eval_user is None:
            eval_password = read_secret(io, EVAL_PASSWORD_SECRET)
            if not eval_password.ok or eval_password.value is None:
                return _secret_refusal(report, "evaluation identity", EVAL_PASSWORD_SECRET, eval_password)
            eval_user = client.add_user(
                EVAL_IDENTITY.username, EVAL_IDENTITY.email, eval_password.value, EVAL_IDENTITY.role
            )
            users.append(eval_user)
        elif eval_user.role != EVAL_IDENTITY.role:
            client.update_role(eval_user.id, EVAL_IDENTITY.role)
    except OwuiError as exc:
        return _failure(report, "evaluation identity", exc, rendered_dir)

    # Memberships: only manifest-owned groups; LDAP-owned ones belong to sign-in sync.
    user_ids_by_name = {user.name: user.id for user in users}
    for desired in desired_groups:
        if desired.membership != "manifest":
            continue
        group = groups_by_name[desired.name]
        missing = [member for member in desired.members if member not in user_ids_by_name]
        if missing:
            return replace(
                report,
                problem=f"group {desired.name}: manifest member(s) not found: {', '.join(missing)}.",
                fix=_MANIFEST_FIX,
            )
        desired_ids = {user_ids_by_name[member] for member in desired.members}
        try:
            observed_ids = set(client.group_members(group.id))
            if desired_ids - observed_ids:
                client.add_group_users(group.id, sorted(desired_ids - observed_ids))
            if observed_ids - desired_ids:
                client.remove_group_users(group.id, sorted(observed_ids - desired_ids))
        except OwuiError as exc:
            return _failure(report, f"membership {desired.name}", exc, rendered_dir)

    # The eval key needs its group's api_keys permission, hence after memberships.
    eval_key, report = _ensure_key(
        io,
        client_factory,
        report,
        step="evaluation API key",
        email=EVAL_IDENTITY.email,
        password_secret=EVAL_PASSWORD_SECRET,
        key_secret="gideon_eval_api_key",
        rendered_dir=rendered_dir,
    )
    if eval_key is None:
        return report

    # Desired-state syncs, listed first so removals are reported by id, and
    # read back after each push (_read_back says why the status is not enough).
    try:
        listed_function_ids = _ids(_listed_rows(client, _FUNCTIONS_PATH, paged=False), _FUNCTIONS_PATH)
        listed_model_ids = _ids(_listed_models(client), _MODELS_PATH)
        function_ids = _payload_ids(functions, "functions")
        model_ids = _payload_ids(models, "models")
    except OwuiError as exc:
        return _failure(report, "resource listings", exc, rendered_dir)
    report = replace(
        report,
        removed_functions=tuple(identifier for identifier in listed_function_ids if identifier not in function_ids),
        removed_models=tuple(identifier for identifier in listed_model_ids if identifier not in model_ids),
    )
    try:
        client.functions_sync(functions)
        _read_back("functions", _ids(_listed_rows(client, _FUNCTIONS_PATH, paged=False), _FUNCTIONS_PATH), function_ids)
        for desired_function in functions:
            identifier = _required_text(desired_function, "id", "functions")
            observed_function = client.function_by_id(identifier)
            difference: str | None
            if observed_function is None:
                difference = f"function {identifier} row missing."
            else:
                difference = _function_difference(
                    desired_function,
                    observed_function,
                    client.function_valves(identifier),
                )
            if difference is not None:
                raise OwuiError(f"Open WebUI functions sync read-back: {difference}")
    except OwuiError as exc:
        return _failure(report, "functions sync", exc, rendered_dir)
    desired_by_id: dict[str, Mapping[str, object]] = {}
    for item in models:
        model_id_value = item.get("id")
        if isinstance(model_id_value, str):
            desired_by_id[model_id_value] = item
    try:
        client.models_sync(models)
        observed_models = _listed_models(client)
        _read_back("models", _ids(observed_models, _MODELS_PATH), model_ids)
        # Every observed id is now a manifest id; a same-id row may still be stale.
        for model_row in observed_models:
            difference = _model_difference(
                desired_by_id[_required_text(model_row, "id", _MODELS_PATH)], model_row
            )
            if difference is not None:
                raise OwuiError(f"Open WebUI models sync read-back: {difference}")
    except OwuiError as exc:
        return _failure(report, "models sync", exc, rendered_dir)
    # The stored rows are the manifest's; the live listing makes them the
    # frontend's too, for the next turn on any path (_LIVE_MODELS_PATH says why).
    try:
        _refresh_live_models(client, desired_by_id)
    except OwuiError as exc:
        return _failure(report, "models refresh", exc, rendered_dir)
    return report
