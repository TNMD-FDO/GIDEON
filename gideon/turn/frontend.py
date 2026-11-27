"""The frontend's wire for a turn's caller: its address, the client, and the reads.

The one module naming a frontend route, header, or reply key; the caller's rule reads
the verdicts and reply words returned here and never the wire.
"""

import json
import logging
import re
from http import HTTPStatus
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Final, Literal, override
from urllib.parse import quote
from urllib.request import Request

import httpx

from gideon.guardrail.writer import bounded_id

FRONTEND_URL: Final = "http://open-webui:8080"
# exempt: a starting bound, corrected by the M1 reading.
REPLY_MAX_BYTES: Final = 64 * 1024
TRANSPORT_LOGGERS: Final = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_TOKEN_ALPHABET: Final = re.compile(r"[A-Za-z0-9_.-]+")

type ReplyWord = int | Literal["invalid", "none"]


class _RejectCookies(DefaultCookiePolicy):
    @override
    def set_ok(self, cookie: Cookie, request: Request) -> bool:
        return False


class _DropTransportTrace(logging.Filter):
    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return False


_TRANSPORT_FILTER: Final = _DropTransportTrace()


def frontend_client(
    *, transport: httpx.AsyncBaseTransport | None = None, base_url: str = FRONTEND_URL,
) -> httpx.AsyncClient:
    """Build the frontend client with cookie storage and transport traces disabled."""
    # Logger filters apply to records emitted here, so install on each emitting logger.
    for name in TRANSPORT_LOGGERS:
        logging.getLogger(name).addFilter(_TRANSPORT_FILTER)
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=None,
        cookies=CookieJar(policy=_RejectCookies()),
    )


def unavailable(reply: ReplyWord) -> bool:
    """Report whether the frontend gave no reply or a server-error status."""
    return reply == "none" or isinstance(reply, int) and reply >= HTTPStatus.INTERNAL_SERVER_ERROR


def session_token_shaped(token: str) -> bool:
    """Allow only the JWT alphabet and keep API keys off the session route."""
    return not token.startswith("sk-") and _TOKEN_ALPHABET.fullmatch(token) is not None


async def _json_body(response: httpx.Response) -> object:
    """Read a 200's JSON under the byte cap; ``None`` past the cap."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > REPLY_MAX_BYTES:
            return None
        body.extend(chunk)
    return json.loads(body)


async def _read_json(client: httpx.AsyncClient, path: str, token: str) -> tuple[object, ReplyWord]:
    """GET one route as the token's user; the body only for a 200 that arrived whole.

    A reply cut off in transit is ``none``, as no reply is; an undecodable one is ``invalid``.
    Nothing is chained from a caught error, since each can carry the request or the reply.
    """
    try:
        async with client.stream("GET", path, headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status_code != HTTPStatus.OK:
                return None, response.status_code
            try:
                return await _json_body(response), HTTPStatus.OK.value
            except httpx.TransportError:
                return None, "none"
            except (httpx.HTTPError, httpx.StreamError, ValueError, RecursionError):
                return None, "invalid"
    except (httpx.HTTPError, OSError):
        return None, "none"


async def read_session(client: httpx.AsyncClient, token: str) -> tuple[str | None, ReplyWord]:
    """Return the verified user's id with 200, or ``None`` and the reply word.

    Only a user or admin role verifies: the route answers 200 to a pending account too.
    A token outside a session token's shape is never sent.
    """
    if not session_token_shaped(token):
        return None, "invalid"
    payload, reply = await _read_json(client, "/api/v1/auths/", token)
    if reply != HTTPStatus.OK:
        return None, reply
    if not isinstance(payload, dict) or payload.get("role") not in ("user", "admin"):
        return None, "invalid"
    user_id = bounded_id(payload.get("id"))
    return (user_id, reply) if user_id is not None else (None, "invalid")


async def read_knowledge(client: httpx.AsyncClient, token: str, kb_id: str) -> tuple[bool, ReplyWord]:
    """Permit a knowledge-base read only when the frontend returns the requested id."""
    if not session_token_shaped(token):
        return False, "invalid"
    segment = quote(kb_id, safe="")
    if segment in (".", ".."):
        # quote leaves unreserved dots alone, and httpx normalizes literal dot segments.
        segment = segment.replace(".", "%2E")
    payload, reply = await _read_json(client, f"/api/v1/knowledge/{segment}", token)
    if reply != HTTPStatus.OK:
        return False, reply
    if isinstance(payload, dict) and payload.get("id") == kb_id:
        return True, reply
    return False, "invalid"
