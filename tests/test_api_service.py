"""Contracts for the GIDEON API service boundary."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from gideon.api.app import create_app
from gideon.api.settings import Settings, load_settings

API_KEY = "fixture-api-key"
ENGINE_KEY = "fixture-engine-key"
ENGINE_URL = "http://fixture-engine/v1"
SOURCE_HEADER = "X-Fixture-Source"
CHAT_HEADER = "X-Fixture-Chat"
EVAL_IDENTITY = "eval@example.invalid"


def response_body(response: httpx.Response) -> dict[str, Any]:
    parsed = response.json()
    assert isinstance(parsed, dict)
    return parsed


class ApiService(unittest.TestCase):
    def settings(self) -> Settings:
        return Settings(
            ENGINE_URL,
            ENGINE_KEY,
            API_KEY,
            8000,
            SOURCE_HEADER,
            CHAT_HEADER,
            EVAL_IDENTITY,
        )

    def request(
        self,
        handler: httpx.MockTransport,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        app = create_app(self.settings(), transport=handler)

        async def run() -> httpx.Response:
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://api",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def test_health_does_not_require_a_key_or_call_the_engine(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": []}, request=request)

        response = self.request(httpx.MockTransport(engine), "GET", "/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_body(response), {"status": "ok"})
        self.assertEqual(calls, [])

    def test_models_without_a_key_is_refused_before_the_engine(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": []}, request=request)

        response = self.request(httpx.MockTransport(engine), "GET", "/v1/models")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(response_body(response)["error"]["type"], "invalid_request_error")
        self.assertEqual(calls, [])

    def test_models_with_a_wrong_key_is_refused_before_the_engine(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": []}, request=request)

        response = self.request(
            httpx.MockTransport(engine),
            "GET",
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertIn("error", response_body(response))
        self.assertEqual(calls, [])

    def test_models_with_a_non_ascii_key_is_refused_not_an_error(self) -> None:
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": []}, request=request)

        response = self.request(
            httpx.MockTransport(engine),
            "GET",
            "/v1/models",
            headers={"Authorization": "Bearer clé-non-ascii".encode()},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])

    def test_models_passes_the_engine_list_and_error_status_through(self) -> None:
        replies = iter(
            (
                (200, {"object": "list", "data": [{"id": "fixture-model"}]}),
                (429, {"error": {"message": "fixture refusal"}}),
            )
        )
        calls: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            status, body = next(replies)
            return httpx.Response(status, json=body, request=request)

        transport = httpx.MockTransport(engine)
        first = self.request(
            transport,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        second = self.request(
            transport,
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(response_body(first)["data"][0]["id"], "fixture-model")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(response_body(second)["error"]["message"], "fixture refusal")
        self.assertEqual([request.url.path for request in calls], ["/v1/models", "/v1/models"])

    def test_unreachable_engine_is_a_fixed_502_error(self) -> None:
        def engine(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture transport refusal", request=request)

        response = self.request(
            httpx.MockTransport(engine),
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fixture transport refusal", response.text)
        self.assertEqual(response_body(response)["error"]["code"], "upstream_unavailable")

    def test_engine_timeout_is_a_fixed_502_error(self) -> None:
        def engine(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("fixture timeout", request=request)

        response = self.request(
            httpx.MockTransport(engine),
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fixture timeout", response.text)
        self.assertEqual(response_body(response)["error"]["type"], "server_error")

    def test_missing_or_empty_key_file_refuses_without_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing-key"
            empty = root / "empty-key"
            empty.write_text("\n", encoding="utf-8")
            common = {
                "GIDEON_ENGINE_URL": ENGINE_URL,
                "GIDEON_ENGINE_API_KEY_FILE": str(root / "engine-key"),
                "GIDEON_API_PORT": "8000",
                "GIDEON_SOURCE_HEADER": SOURCE_HEADER,
                "GIDEON_CHAT_HEADER": CHAT_HEADER,
                "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY,
            }
            (root / "engine-key").write_text(ENGINE_KEY, encoding="utf-8")
            for path in (missing, empty):
                with self.subTest(path=path):
                    environment = {**common, "GIDEON_API_KEY_FILE": str(path)}
                    with self.assertRaisesRegex(ValueError, str(path)) as context:
                        load_settings(environment)
                    self.assertNotIn(API_KEY, str(context.exception))

    def test_source_settings_are_loaded_from_required_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "engine-key").write_text(ENGINE_KEY, encoding="utf-8")
            (root / "api-key").write_text(API_KEY, encoding="utf-8")
            settings = load_settings(
                {
                    "GIDEON_ENGINE_URL": ENGINE_URL,
                    "GIDEON_ENGINE_API_KEY_FILE": str(root / "engine-key"),
                    "GIDEON_API_KEY_FILE": str(root / "api-key"),
                    "GIDEON_API_PORT": "8000",
                    "GIDEON_SOURCE_HEADER": SOURCE_HEADER,
                    "GIDEON_CHAT_HEADER": CHAT_HEADER,
                    "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY,
                }
            )

        self.assertEqual(settings.source_header, SOURCE_HEADER)
        self.assertEqual(settings.chat_header, CHAT_HEADER)
        self.assertEqual(settings.eval_identity, EVAL_IDENTITY)

    def test_missing_or_empty_source_settings_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "engine-key").write_text(ENGINE_KEY, encoding="utf-8")
            (root / "api-key").write_text(API_KEY, encoding="utf-8")
            common = {
                "GIDEON_ENGINE_URL": ENGINE_URL,
                "GIDEON_ENGINE_API_KEY_FILE": str(root / "engine-key"),
                "GIDEON_API_KEY_FILE": str(root / "api-key"),
                "GIDEON_API_PORT": "8000",
                "GIDEON_SOURCE_HEADER": SOURCE_HEADER,
                "GIDEON_CHAT_HEADER": CHAT_HEADER,
                "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY,
            }
            for variable in (
                "GIDEON_SOURCE_HEADER",
                "GIDEON_CHAT_HEADER",
                "GIDEON_EVAL_IDENTITY",
            ):
                for value in (None, " \t"):
                    with self.subTest(variable=variable, value=value):
                        environment = dict(common)
                        if value is None:
                            del environment[variable]
                        else:
                            environment[variable] = value
                        with self.assertRaisesRegex(ValueError, variable):
                            load_settings(environment)

    def test_request_log_has_no_query_header_or_body_values(self) -> None:
        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []}, request=request)

        with self.assertLogs("gideon.api.request", level="INFO") as captured:
            response = self.request(
                httpx.MockTransport(engine),
                "GET",
                "/v1/models?query=fixture-query-secret",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "X-Fixture-Header": "fixture-header-secret",
                },
                content=b"fixture-body-secret",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured.output), 1)
        line = captured.output[0]
        self.assertIn("GET /v1/models 200", line)
        for value in (
            "fixture-query-secret",
            API_KEY,
            "fixture-header-secret",
            "fixture-body-secret",
        ):
            self.assertNotIn(value, line)

    def test_request_log_names_no_unmatched_path(self) -> None:
        def engine(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []}, request=request)

        with self.assertLogs("gideon.api.request", level="INFO") as captured:
            response = self.request(httpx.MockTransport(engine), "GET", "/fixture-path-secret")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("GET - 401", captured.output[0])
        self.assertNotIn("fixture-path-secret", captured.output[0])
