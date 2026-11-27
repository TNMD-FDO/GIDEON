"""Session-token log suppression over the HTTP stack pinned in images.lock."""

import asyncio
import json
import logging
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from unittest.mock import patch

from test_toolchain_pins import gideon_pin
from test_turn_frontend import SESSION_ROUTE, TOKEN, USER_ID

from gideon.turn.frontend import TRANSPORT_LOGGERS, frontend_client, read_session

_WAIT_SECONDS = 3.0


class _SessionHandler(BaseHTTPRequestHandler):
    """Echo the bearer in the session body and cookie as the frontend does."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path != SESSION_ROUTE:
            self.send_error(404)
            return
        if self.headers.get_all("Authorization") != [f"Bearer {TOKEN}"]:
            self.send_error(401)
            return
        body = json.dumps({"id": USER_ID, "role": "user", "token": TOKEN}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"token={TOKEN}; Path=/; Secure; HttpOnly; SameSite=Strict")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class FrontendSockets(unittest.IsolatedAsyncioTestCase):
    def test_installed_http_stack_matches_the_image_pins(self) -> None:
        build_args = gideon_pin().build_args
        for package in ("httpx", "httpcore"):
            with self.subTest(package=package):
                argument = f"{package.upper()}_VERSION"
                self.assertEqual(
                    version(package), build_args[argument],
                    f"Fix: run the socket proof with {argument} from images.lock.",
                )

    async def test_real_transport_filter_drops_the_token_trace(self) -> None:
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), _SessionHandler))
        thread = threading.Thread(target=server.serve_forever, name="fixture-frontend")
        thread.start()
        self.addCleanup(thread.join, _WAIT_SECONDS)
        self.addCleanup(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        capture = _Records()
        transport_loggers = [logging.getLogger(name) for name in TRANSPORT_LOGGERS]
        root = logging.getLogger()
        loggers = [root, *(logger for logger in root.manager.loggerDict.values() if isinstance(logger, logging.Logger))]
        with ExitStack() as logging_state:
            logging_state.callback(logging.disable, root.manager.disable)
            logging.disable(logging.NOTSET)
            for logger in loggers:
                logging_state.callback(logger.setLevel, logger.level)
                logger.setLevel(logging.DEBUG)
                logging_state.enter_context(patch.object(logger, "disabled", False))
                logging_state.enter_context(patch.object(logger, "propagate", True))
                logging_state.enter_context(patch.object(logger, "handlers", [capture] if logger is root else []))
            for logger in transport_loggers:
                logging_state.enter_context(patch.object(logger, "filters", []))

            async with frontend_client(base_url=base_url) as client:
                async with asyncio.timeout(_WAIT_SECONDS):
                    self.assertEqual(await read_session(client, TOKEN), (USER_ID, 200))
                self.assertFalse(
                    any(TOKEN in record.getMessage() or TOKEN in repr(vars(record)) for record in capture.records),
                    "The filtered transport exposed the fixture token.",
                )

                capture.records.clear()
                for logger in transport_loggers:
                    logger.filters.clear()
                async with asyncio.timeout(_WAIT_SECONDS):
                    self.assertEqual(await read_session(client, TOKEN), (USER_ID, 200))
                self.assertTrue(
                    any(
                        TOKEN in record.getMessage() and "set-cookie" in record.getMessage().lower()
                        for record in capture.records if record.name in TRANSPORT_LOGGERS
                    ),
                    "The unfiltered transport must trace the fixture's Set-Cookie header.",
                )
