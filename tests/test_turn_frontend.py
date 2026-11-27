"""The frontend's session and knowledge reads for turn admission: only a 200 naming the id permits."""

import json
import logging
import re
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
from test_render import inputs

from gideon.host.render.caddy import CaddyfileArtifact
from gideon.turn.frontend import (
    FRONTEND_URL,
    REPLY_MAX_BYTES,
    TRANSPORT_LOGGERS,
    ReplyWord,
    frontend_client,
    read_knowledge,
    read_session,
    session_token_shaped,
    unavailable,
)

TOKEN = "fictitious.header_payload.fixture-signature"
USER_ID = "fixture-user_1"
SESSION_ROUTE = "/api/v1/auths/"
KNOWLEDGE_ROUTE = "/api/v1/knowledge/"
KB_ID = "fixture-kb"


class ReplyStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


class _FrontendRead(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name in TRANSPORT_LOGGERS:
            logger = logging.getLogger(name)
            self.enterContext(patch.object(logger, "filters", logger.filters.copy()))


class Session(_FrontendRead):
    async def _read_response(self, response: httpx.Response) -> tuple[str | None, ReplyWord]:
        async with frontend_client(transport=httpx.MockTransport(lambda _: response)) as client:
            return await read_session(client, TOKEN)

    async def test_session_oracle_rows(self) -> None:
        # The session column of the research note's table. The detail text is immaterial.
        for case, status, body, expected in (
            ("user (a)", 200, {"id": USER_ID, "role": "user", "token": TOKEN}, (USER_ID, 200)),
            ("no credentials (d)", 401, {"detail": "Fixture: no credentials"}, (None, 401)),
            ("malformed JWT (e)", 401, {"detail": "Fixture: unauthorized"}, (None, 401)),
            ("wrong signature (e)", 401, {"detail": "Fixture: unauthorized"}, (None, 401)),
            ("wrong algorithm (e)", 401, {"detail": "Fixture: unauthorized"}, (None, 401)),
            ("missing claim (e)", 401, {"detail": "Fixture: unauthorized"}, (None, 401)),
            ("expired JWT (f)", 401, {"detail": "Fixture: unauthorized"}, (None, 401)),
            ("deleted user (g)", 401, {"detail": "Fixture: invalid token"}, (None, 401)),
            ("pending user (h)", 200, {"id": USER_ID, "role": "pending"}, (None, "invalid")),
            ("admin (i)", 200, {"id": USER_ID, "role": "admin"}, (USER_ID, 200)),
        ):
            with self.subTest(case=case):
                self.assertEqual(await self._read_response(httpx.Response(status, json=body)), expected)

    async def test_success_requires_the_id_and_verified_role(self) -> None:
        bodies: tuple[object, ...] = (
            None, [], {}, {"role": "user"}, {"id": USER_ID},
            {"id": 12, "role": "user"}, {"id": "space in id", "role": "user"},
            {"id": USER_ID, "role": ["user"]}, {"id": USER_ID, "role": "viewer"},
        )
        for body in bodies:
            with self.subTest(body=body):
                response = httpx.Response(200, content=json.dumps(body).encode())
                self.assertEqual(await self._read_response(response), (None, "invalid"))

    async def test_server_error_keeps_its_status_without_reading_the_body(self) -> None:
        stream = ReplyStream((TOKEN.encode(),))
        self.assertEqual(await self._read_response(httpx.Response(503, stream=stream)), (None, 503))
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_cookie_is_never_stored_or_sent(self) -> None:
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"id": USER_ID, "role": "user", "token": TOKEN},
                # Cookie would be sent even over HTTP if the jar accepted it.
                headers={"Set-Cookie": f"token={TOKEN}; Path=/; HttpOnly; SameSite=Strict"},
            )

        next_token = "another.fictitious.session"
        with self.assertNoLogs("httpx", level=logging.DEBUG):
            async with frontend_client(transport=httpx.MockTransport(respond)) as client:
                self.assertFalse(client.trust_env)
                self.assertEqual(client.timeout, httpx.Timeout(None))
                for token in (TOKEN, next_token):
                    self.assertEqual(await read_session(client, token), (USER_ID, 200))
                    self.assertFalse(client.cookies)

        for request, token in zip(requests, (TOKEN, next_token), strict=True):
            self.assertEqual((request.method, request.url.path, request.url.query), ("GET", SESSION_ROUTE, b""))
            self.assertEqual(request.headers.get_list("Authorization"), [f"Bearer {token}"])
            self.assertNotIn("Cookie", request.headers)
            self.assertEqual(request.content, b"")

    async def test_redirect_is_returned_without_following_it(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, SESSION_ROUTE)
            return httpx.Response(307, headers={"Location": "/fictitious-redirect"})

        async with frontend_client(transport=httpx.MockTransport(respond)) as client:
            self.assertEqual(await read_session(client, TOKEN), (None, 307))

    async def test_streamed_reply_is_capped_before_reading_more(self) -> None:
        body = json.dumps({"id": USER_ID, "role": "user"}).encode().ljust(REPLY_MAX_BYTES)
        within = ReplyStream((body,))
        oversized = ReplyStream((body, b" ", TOKEN.encode()))
        replies = iter((within, oversized))
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=next(replies)))
        async with frontend_client(transport=transport) as client:
            self.assertEqual(await read_session(client, TOKEN), (USER_ID, 200))
            self.assertEqual(await read_session(client, TOKEN), (None, "invalid"))
        self.assertEqual(oversized.reads, 2)
        self.assertTrue(oversized.closed)

    async def test_transport_failures_before_a_reply_are_content_free(self) -> None:
        for error in (
            httpx.ConnectError(TOKEN), httpx.ReadTimeout(TOKEN),
            httpx.RemoteProtocolError(TOKEN), OSError(TOKEN),
        ):
            with self.subTest(error_type=type(error).__name__):
                def fail(_: httpx.Request, error: Exception = error) -> httpx.Response:
                    raise error

                with self.assertLogs(level=logging.DEBUG) as logs:
                    logging.getLogger(__name__).debug("Reading a fixture transport failure.")
                    async with frontend_client(transport=httpx.MockTransport(fail)) as client:
                        self.assertEqual(await read_session(client, TOKEN), (None, "none"))
                self.assertNotIn(TOKEN, "\n".join(logs.output))

    async def test_body_failures_are_content_free(self) -> None:
        for case, stream, headers, expected in (
            ("truncated", ReplyStream((b"{",), httpx.ReadError(TOKEN)), {}, "none"),
            ("gzip", ReplyStream((TOKEN.encode(),)), {"Content-Encoding": "gzip"}, "invalid"),
            ("unicode", ReplyStream((b'{"token":"' + TOKEN.encode() + b'\xff"}',)), {}, "invalid"),
            ("json", ReplyStream((b'{"token":"' + TOKEN.encode(),)), {}, "invalid"),
            ("nested json", ReplyStream((b"[" * 10_000 + b'"' + TOKEN.encode() + b'"' + b"]" * 10_000,)), {}, "invalid"),
        ):
            with self.subTest(case=case):
                response = httpx.Response(200, stream=stream, headers=headers)
                with self.assertLogs(level=logging.DEBUG) as logs:
                    logging.getLogger(__name__).debug("Reading a fixture body failure.")
                    self.assertEqual(await self._read_response(response), (None, expected))
                self.assertNotIn(TOKEN, "\n".join(logs.output))
                self.assertTrue(stream.closed)

    async def test_token_shape_refuses_locally(self) -> None:
        def unexpected(_: httpx.Request) -> httpx.Response:
            self.fail("A malformed session token reached the frontend.")

        async with frontend_client(transport=httpx.MockTransport(unexpected)) as client:
            self.assertTrue(session_token_shaped(TOKEN))
            for token in ("", "sk-fictitious", "a b", "a\t", "a\n", "a\r", "a\0", "a/+", "a=", "aé"):
                with self.subTest(token=token):
                    self.assertFalse(session_token_shaped(token))
                    self.assertEqual(await read_session(client, token), (None, "invalid"))


class Knowledge(_FrontendRead):
    async def _read_response(self, response: httpx.Response, *, kb_id: str = KB_ID) -> tuple[bool, ReplyWord]:
        async with frontend_client(transport=httpx.MockTransport(lambda _: response)) as client:
            return await read_knowledge(client, TOKEN, kb_id)

    async def test_knowledge_oracle_rows(self) -> None:
        # The knowledge column of the research note's table. The adapter uses the status and id alone.
        for case, status, permitted in (
            ("owner (a)", 200, True),
            ("read grant (a)", 200, True),
            ("no read access (b)", 401, False),
            ("missing knowledge base (c)", 404, False),
            ("no credentials (d)", 401, False),
            ("invalid JWT (e)", 401, False),
            ("expired JWT (f)", 401, False),
            ("deleted user (g)", 401, False),
            ("pending user (h)", 401, False),
            ("admin (i)", 200, True),
        ):
            with self.subTest(case=case):
                body = {"id": KB_ID} if permitted else {"detail": "Fictitious refusal detail"}
                response = httpx.Response(status, json=body)
                self.assertEqual(await self._read_response(response), (permitted, status))

    async def test_server_error_keeps_its_status_without_reading_the_body(self) -> None:
        stream = ReplyStream((TOKEN.encode(),))
        self.assertEqual(await self._read_response(httpx.Response(503, stream=stream)), (False, 503))
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_routed_200s_do_not_permit(self) -> None:
        for case, kb_id, response in (
            ("list", "", httpx.Response(200, json={"items": [{"id": KB_ID}], "total": 1})),
            ("search", "search", httpx.Response(200, json={"items": [{"id": "search"}], "total": 1})),
            ("SPA", KB_ID, httpx.Response(200, text="<html>Fictitious shell</html>")),
        ):
            with self.subTest(case=case):
                self.assertEqual(await self._read_response(response, kb_id=kb_id), (False, "invalid"))

    async def test_200_requires_the_exact_requested_id_in_a_json_object(self) -> None:
        for body in ({"id": "fixture-other-kb"}, [{"id": KB_ID}]):
            with self.subTest(body=body):
                self.assertEqual(await self._read_response(httpx.Response(200, json=body)), (False, "invalid"))

    async def test_path_and_credentials_stay_on_one_get(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(404, headers={"Set-Cookie": f"token={TOKEN}; Path=/; HttpOnly"})

        cases = (
            ("fixture/kb", b"fixture%2Fkb"),
            ("fixture%2Fkb", b"fixture%252Fkb"),
            (".", b"%2E"),
            ("..", b"%2E%2E"),
            ("fixture/../kb", b"fixture%2F..%2Fkb"),
            ("odd?#:+@[]é", b"odd%3F%23%3A%2B%40%5B%5D%C3%A9"),
        )
        async with frontend_client(transport=httpx.MockTransport(respond)) as client:
            for kb_id, _ in cases:
                self.assertEqual(await read_knowledge(client, TOKEN, kb_id), (False, 404))
                self.assertFalse(client.cookies)

        for (kb_id, expected_segment), request in zip(cases, requests, strict=True):
            with self.subTest(kb_id=kb_id):
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.raw_path, KNOWLEDGE_ROUTE.encode() + expected_segment)
                self.assertEqual(request.headers.get_list("Authorization"), [f"Bearer {TOKEN}"])
                self.assertNotIn("Cookie", request.headers)

    async def test_streamed_reply_is_capped(self) -> None:
        body = json.dumps({"id": KB_ID}).encode().ljust(REPLY_MAX_BYTES)
        within = ReplyStream((body,))
        oversized = ReplyStream((body, b" ", TOKEN.encode()))
        self.assertEqual(await self._read_response(httpx.Response(200, stream=within)), (True, 200))
        self.assertEqual(await self._read_response(httpx.Response(200, stream=oversized)), (False, "invalid"))
        self.assertEqual(oversized.reads, 2)
        self.assertTrue(oversized.closed)

    async def test_transport_and_decode_failures_are_content_free(self) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(TOKEN)

        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger(__name__).debug("Reading fixture knowledge failures.")
            async with frontend_client(transport=httpx.MockTransport(fail)) as client:
                self.assertEqual(await read_knowledge(client, TOKEN, KB_ID), (False, "none"))
            for stream, headers, expected in (
                (ReplyStream((b"{",), httpx.ReadError(TOKEN)), {}, "none"),
                (ReplyStream((TOKEN.encode(),)), {"Content-Encoding": "gzip"}, "invalid"),
                (ReplyStream((b'{"id":"' + TOKEN.encode() + b'\xff"}',)), {}, "invalid"),
                (ReplyStream((b'{"id":"' + TOKEN.encode(),)), {}, "invalid"),
            ):
                response = httpx.Response(200, stream=stream, headers=headers)
                self.assertEqual(await self._read_response(response), (False, expected))
                self.assertTrue(stream.closed)
        for record in logs.records:
            self.assertNotIn(TOKEN, record.getMessage())
            self.assertNotIn(TOKEN, repr(vars(record)))


class FrontendContract(unittest.TestCase):
    def test_reply_availability(self) -> None:
        self.assertTrue(unavailable("none"))
        self.assertFalse(unavailable("invalid"))
        for reply in (500, 503, 599, 600):
            self.assertTrue(unavailable(reply))
        for reply in (200, 401, 499):
            self.assertFalse(unavailable(reply))

    def test_address_matches_the_rendered_default_ingress_upstream(self) -> None:
        caddyfile = CaddyfileArtifact().emit(inputs())
        upstreams = re.findall(r"(?m)^    handle \{\n\s+reverse_proxy (\S+) \{", caddyfile)
        self.assertEqual(len(upstreams), 1)
        self.assertEqual(FRONTEND_URL, f"http://{upstreams[0]}")
