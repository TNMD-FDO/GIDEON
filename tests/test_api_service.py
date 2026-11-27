"""Contracts for the GIDEON API service boundary."""

import asyncio
import re
import tempfile
import unittest
from itertools import combinations
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from gideon.api import __main__ as api_main
from gideon.api.app import create_app
from gideon.api.settings import RecordCoordinates, Settings, load_settings
from gideon.turn.scope import CourtFileError

API_KEY = "fixture-api-key"
ENGINE_KEY = "fixture-engine-key"
ENGINE_URL = "http://fixture-engine/v1"
SOURCE_HEADER = "X-Fixture-Source"
CHAT_HEADER = "X-Fixture-Chat"
USER_HEADER = "X-Fixture-User"
EVAL_IDENTITY = "eval@example.invalid"
INSTRUCTION = "Fictitious General instruction."
MODEL_ID = "fixture-general"
ENGINE_MODEL = "fixture-model"
READER_PASSWORD = "fixture-reader-password"
CORPUS_READER_PASSWORD = "fixture-corpus-reader-password"
QDRANT_KEY = "fixture-vector-key"
EMBED_KEY = "fixture-embedding-key"
OPENSEARCH_PASSWORD = "fixture-lexical-password"
QDRANT_URL = "http://fixture-vector:7001"
EMBED_URL = "http://fixture-embedding:7002/"
EMBED_MODEL = "fixture-embed"
EMBED_REPOSITORY = "fixture-owner/fixture-embed"
EMBED_SPACE = "fixture-space"
EMBED_DIMENSIONS = 7
OPENSEARCH_URL = "http://fixture-lexical:7003"
COURTS_FILE = Path(__file__).with_name("fixtures") / "turn" / "courts.json"
COURT_ENV = {
    "GIDEON_API_COURTS_SCOTUS": "fixturescotus",
    "GIDEON_API_COURTS_CIRCUIT": "fixturecircuit",
    "GIDEON_API_COURTS_DISTRICTS": "fixturedistrict2,fixturedistrict3",
    "GIDEON_API_COURTS_STATES": "fx-state13A",
}
RECORD = RecordCoordinates(
    "fixture-postgres", 5432, "fixture-db", "fixture-reader", Path("/fixture/reader-password"),
)


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
            USER_HEADER,
            EVAL_IDENTITY,
            INSTRUCTION,
            MODEL_ID,
            ENGINE_MODEL,
            record=RECORD,
            corpus_record=RECORD,
            cas_root=Path("/fixture/cas"),
            qdrant_url=QDRANT_URL,
            qdrant_key_file=Path("/fixture/qdrant-key"),
            embed_url=EMBED_URL,
            embed_key_file=Path("/fixture/embed-key"),
            opensearch_url=OPENSEARCH_URL,
            opensearch_user="fixture-lexical-reader",
            opensearch_password_file=Path("/fixture/lexical-password"),
            embed_model=EMBED_MODEL,
            embed_repository=EMBED_REPOSITORY,
            embed_space=EMBED_SPACE,
            embed_dimensions=EMBED_DIMENSIONS,
            courts_scotus=("fixturescotus",),
            courts_circuit=("fixturecircuit",),
            courts_districts=("fixturedistrict2", "fixturedistrict3"),
            courts_states=("fx-state13A",),
            courts_file=COURTS_FILE,
        )

    def loaded_settings_environment(self, root: Path) -> dict[str, str]:
        (root / "engine-key").write_text(ENGINE_KEY, encoding="utf-8")
        (root / "api-key").write_text(API_KEY, encoding="utf-8")
        (root / "instruction.txt").write_text(f" \n{INSTRUCTION}\n ", encoding="utf-8")
        (root / "reader-password").write_text(READER_PASSWORD, encoding="utf-8")
        (root / "corpus-reader-password").write_text(CORPUS_READER_PASSWORD, encoding="utf-8")
        (root / "qdrant-key").write_text(QDRANT_KEY, encoding="utf-8")
        (root / "embed-key").write_text(EMBED_KEY, encoding="utf-8")
        (root / "lexical-password").write_text(OPENSEARCH_PASSWORD, encoding="utf-8")
        return {
            **COURT_ENV,
            "GIDEON_API_COURTS_FILE": str(COURTS_FILE),
            "GIDEON_API_DB_HOST": "fixture-postgres",
            "GIDEON_API_DB_PORT": "5432",
            "GIDEON_API_DB_NAME": "fixture-db",
            "GIDEON_API_DB_ROLE": "fixture-reader",
            "GIDEON_API_PASSWORD_FILE": str(root / "reader-password"),
            "GIDEON_API_CORPUS_DB_HOST": "fixture-record-relay",
            "GIDEON_API_CORPUS_DB_PORT": "5433",
            "GIDEON_API_CORPUS_DB_NAME": "fixture-corpus-db",
            "GIDEON_API_CORPUS_DB_ROLE": "fixture-corpus-reader",
            "GIDEON_API_CORPUS_PASSWORD_FILE": str(root / "corpus-reader-password"),
            "GIDEON_API_CAS_ROOT": str(root / "cas"),
            "GIDEON_API_QDRANT_URL": QDRANT_URL,
            "GIDEON_API_QDRANT_KEY_FILE": str(root / "qdrant-key"),
            "GIDEON_API_EMBED_URL": EMBED_URL,
            "GIDEON_API_EMBED_KEY_FILE": str(root / "embed-key"),
            "GIDEON_API_EMBED_MODEL": EMBED_MODEL,
            "GIDEON_API_EMBED_REPOSITORY": EMBED_REPOSITORY,
            "GIDEON_API_EMBED_SPACE": EMBED_SPACE,
            "GIDEON_API_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
            "GIDEON_API_OPENSEARCH_URL": OPENSEARCH_URL,
            "GIDEON_API_OPENSEARCH_USER": "fixture-lexical-reader",
            "GIDEON_API_OPENSEARCH_PASSWORD_FILE": str(root / "lexical-password"),
            "GIDEON_ENGINE_URL": ENGINE_URL,
            "GIDEON_ENGINE_API_KEY_FILE": str(root / "engine-key"),
            "GIDEON_API_KEY_FILE": str(root / "api-key"),
            "GIDEON_API_PORT": "8000",
            "GIDEON_SOURCE_HEADER": SOURCE_HEADER,
            "GIDEON_CHAT_HEADER": CHAT_HEADER,
            "GIDEON_USER_HEADER": USER_HEADER,
            "GIDEON_EVAL_IDENTITY": EVAL_IDENTITY,
            "GIDEON_INSTRUCTION_FILE": str(root / "instruction.txt"),
            "GIDEON_MODEL_ID": MODEL_ID,
            "GIDEON_ENGINE_MODEL": ENGINE_MODEL,
        }

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

    def test_models_lists_general_and_passes_engine_error_through(self) -> None:
        replies = iter(
            (
                (200, {"object": "list", "data": [{"id": ENGINE_MODEL, "created": 42,
                     "owned_by": "engine-secret", "extra": "secret"}]}),
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
        self.assertEqual(response_body(first), {"object": "list", "data": [{
            "id": MODEL_ID, "object": "model", "created": 42, "owned_by": "gideon",
        }]})
        self.assertNotIn(ENGINE_MODEL, first.text)
        self.assertNotIn("engine-secret", first.text)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(response_body(second)["error"]["message"], "fixture refusal")
        self.assertEqual([request.url.path for request in calls], ["/v1/models", "/v1/models"])

    def test_engine_listing_without_served_name_is_fixed_502_and_content_free_log(self) -> None:
        for body in ({"data": [{"id": "engine-secret"}]}, {"data": "unreadable"}):
            with self.subTest(body=body):
                def engine(request: httpx.Request, listing: dict[str, Any] = body) -> httpx.Response:
                    return httpx.Response(200, json=listing, request=request)

                with self.assertLogs("gideon.api.app", level="WARNING") as captured:
                    response = self.request(
                        httpx.MockTransport(engine), "GET", "/v1/models",
                        headers={"Authorization": f"Bearer {API_KEY}"},
                    )
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response_body(response)["error"]["code"], "upstream_unavailable")
                self.assertEqual(len(captured.records), 1)
                self.assertNotIn("engine-secret", captured.output[0])
                self.assertNotIn(ENGINE_MODEL, captured.output[0])

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
            common = self.loaded_settings_environment(root)
            missing = root / "missing-key"
            empty = root / "empty-key"
            empty.write_text("\n", encoding="utf-8")
            for path in (missing, empty):
                with self.subTest(path=path):
                    environment = {**common, "GIDEON_API_KEY_FILE": str(path)}
                    with self.assertRaisesRegex(ValueError, str(path)) as context:
                        load_settings(environment)
                    self.assertNotIn(API_KEY, str(context.exception))

    def test_source_settings_are_loaded_from_required_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(self.loaded_settings_environment(Path(directory)))

        self.assertEqual(settings.source_header, SOURCE_HEADER)
        self.assertEqual(settings.chat_header, CHAT_HEADER)
        self.assertEqual(settings.user_header, USER_HEADER)
        self.assertEqual(settings.eval_identity, EVAL_IDENTITY)
        self.assertEqual(settings.instruction, INSTRUCTION)
        self.assertEqual(settings.model_id, MODEL_ID)
        self.assertEqual(settings.engine_model, ENGINE_MODEL)
        self.assertEqual(settings.embed_model, EMBED_MODEL)
        self.assertEqual(settings.embed_repository, EMBED_REPOSITORY)
        self.assertEqual(settings.embed_space, EMBED_SPACE)
        self.assertEqual(settings.embed_dimensions, EMBED_DIMENSIONS)

    def test_reranker_settings_accept_both_or_neither(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.loaded_settings_environment(root)
            without_reranker = load_settings(base)
            self.assertIsNone(without_reranker.rerank_url)
            self.assertIsNone(without_reranker.rerank_key_file)

            url = "http://fictitious-reranker:7003"
            key_file = root / "reranker-key"
            key_file.write_text("fictitious-reranker-key\n", encoding="utf-8")
            with_reranker = load_settings({
                **base,
                "GIDEON_API_RERANK_URL": url,
                "GIDEON_API_RERANK_KEY_FILE": str(key_file),
            })
            self.assertEqual(with_reranker.rerank_url, url)
            self.assertEqual(with_reranker.rerank_key_file, key_file)

    def test_court_settings_read_groups_and_accept_an_empty_states_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            settings = load_settings(environment)
            expected = self.settings()
            self.assertEqual(settings.courts_scotus, expected.courts_scotus)
            self.assertEqual(settings.courts_circuit, expected.courts_circuit)
            self.assertEqual(settings.courts_districts, expected.courts_districts)
            self.assertEqual(settings.courts_states, expected.courts_states)
            self.assertEqual(settings.courts_file, COURTS_FILE)
            environment["GIDEON_API_COURTS_STATES"] = ""
            self.assertEqual(load_settings(environment).courts_states, ())

    def test_court_settings_refuse_missing_empty_or_malformed_groups_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            for name in COURT_ENV:
                malformed: tuple[str, ...] = ("fixture_court", "-fixture", "fixture court", "fixture,", "f" * 33)
                if name != "GIDEON_API_COURTS_STATES":
                    malformed += ("",)
                if name == "GIDEON_API_COURTS_CIRCUIT":
                    malformed += ("fixtureone,fixturetwo",)
                missing = {key: value for key, value in environment.items() if key != name}
                with self.subTest(name=name, case="missing"), self.assertRaisesRegex(ValueError, name):
                    load_settings(missing)
                for value in malformed:
                    with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                        load_settings({**environment, name: value})

    def test_court_settings_refuse_each_overlapping_pair_by_later_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            for first, later in combinations(COURT_ENV, 2):
                repeated = environment[first].split(",")[0]
                with self.subTest(first=first, later=later), self.assertRaisesRegex(ValueError, later) as caught:
                    load_settings({**environment, later: repeated})
                self.assertNotIn(first, str(caught.exception))
                self.assertNotIn(repeated, str(caught.exception))

    def test_court_file_setting_refuses_missing_or_empty_variable_and_unusable_paths(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = self.loaded_settings_environment(root)
        variable = "GIDEON_API_COURTS_FILE"
        del environment[variable]
        with self.assertRaisesRegex(ValueError, variable):
            load_settings(environment)
        with self.assertRaisesRegex(ValueError, variable):
            load_settings({**environment, variable: " \t"})

        empty = root / "empty-courts.json"
        empty.write_text(" \n\t", encoding="utf-8")
        undecodable = root / "invalid-utf8-courts.json"
        undecodable.write_bytes(b"\xff")
        for path in (root / "missing-courts.json", empty, undecodable):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, re.escape(str(path))):
                load_settings({**environment, variable: str(path)})

    def test_malformed_court_file_refuses_startup_without_quoting_contents(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / "malformed-courts.json"
        content = "Fictitious malformed court file sentinel"
        path.write_text(content, encoding="utf-8")
        settings = load_settings({**self.loaded_settings_environment(root), "GIDEON_API_COURTS_FILE": str(path)})
        with self.assertRaisesRegex(CourtFileError, re.escape(str(path))) as app_failure:
            create_app(settings)
        self.assertNotIn(content, str(app_failure.exception))

        with (
            patch.object(api_main, "load_settings", return_value=settings),
            patch.object(api_main.uvicorn, "run") as serve,
            self.assertRaisesRegex(SystemExit, re.escape(str(path))) as process_failure,
        ):
            api_main.main()
        self.assertNotIn(content, str(process_failure.exception))
        serve.assert_not_called()

    def test_reranker_settings_refuse_a_partial_pair_by_missing_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.loaded_settings_environment(root)
            key_file = root / "reranker-key"
            key_file.write_text("fictitious-reranker-key\n", encoding="utf-8")
            for supplied, missing in (
                ({"GIDEON_API_RERANK_URL": "http://fictitious-reranker:7003"}, "GIDEON_API_RERANK_KEY_FILE"),
                ({"GIDEON_API_RERANK_KEY_FILE": str(key_file)}, "GIDEON_API_RERANK_URL"),
            ):
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    ValueError, f"Environment variable {missing} is missing"
                ):
                    load_settings({**base, **supplied})

    def test_reranker_settings_validate_the_url_and_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.loaded_settings_environment(root)
            key_file = root / "reranker-key"
            key_file.write_text("fictitious-reranker-key\n", encoding="utf-8")
            pair = {
                "GIDEON_API_RERANK_URL": "http://fictitious-reranker:7003/v1",
                "GIDEON_API_RERANK_KEY_FILE": str(key_file),
            }
            with self.assertRaisesRegex(ValueError, "GIDEON_API_RERANK_URL must be an HTTP URL"):
                load_settings({**base, **pair})
            key_file.unlink()
            pair["GIDEON_API_RERANK_URL"] = "http://fictitious-reranker:7003"
            with self.assertRaises(ValueError) as caught:
                load_settings({**base, **pair})
            self.assertIn(f"Secret file {key_file} is missing or empty", str(caught.exception))

    def test_record_and_store_settings_refuse_missing_or_empty_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            for variable in (
                "GIDEON_API_DB_HOST",
                "GIDEON_API_DB_PORT",
                "GIDEON_API_DB_NAME",
                "GIDEON_API_DB_ROLE",
                "GIDEON_API_PASSWORD_FILE",
                "GIDEON_API_CORPUS_DB_HOST",
                "GIDEON_API_CORPUS_DB_PORT",
                "GIDEON_API_CORPUS_DB_NAME",
                "GIDEON_API_CORPUS_DB_ROLE",
                "GIDEON_API_CORPUS_PASSWORD_FILE",
                "GIDEON_API_CAS_ROOT",
                "GIDEON_API_QDRANT_URL",
                "GIDEON_API_QDRANT_KEY_FILE",
                "GIDEON_API_EMBED_URL",
                "GIDEON_API_EMBED_KEY_FILE",
                "GIDEON_API_EMBED_MODEL",
                "GIDEON_API_EMBED_REPOSITORY",
                "GIDEON_API_EMBED_SPACE",
                "GIDEON_API_EMBED_DIMENSIONS",
                "GIDEON_API_OPENSEARCH_URL",
                "GIDEON_API_OPENSEARCH_USER",
                "GIDEON_API_OPENSEARCH_PASSWORD_FILE",
            ):
                missing = {name: value for name, value in environment.items() if name != variable}
                for case, values in (("missing", missing), ("empty", {**environment, variable: ""})):
                    with self.subTest(variable=variable, case=case), self.assertRaisesRegex(
                        ValueError, f"Environment variable {variable} is missing or empty",
                    ):
                        load_settings(values)

    def test_record_and_store_secret_files_refuse_missing_or_empty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = self.loaded_settings_environment(root)
            empty = root / "empty-secret"
            empty.write_text(" \n\t", encoding="utf-8")
            for variable in (
                "GIDEON_API_PASSWORD_FILE",
                "GIDEON_API_CORPUS_PASSWORD_FILE",
                "GIDEON_API_QDRANT_KEY_FILE",
                "GIDEON_API_EMBED_KEY_FILE",
                "GIDEON_API_OPENSEARCH_PASSWORD_FILE",
            ):
                value = Path(environment[variable]).read_text(encoding="utf-8")
                for path in (root / "missing-secret", empty):
                    with self.subTest(variable=variable, path=path):
                        with self.assertRaisesRegex(ValueError, f"Secret file {path} is missing or empty") as context:
                            load_settings({**environment, variable: str(path)})
                        self.assertNotIn(value, str(context.exception))

    def test_store_urls_require_http_with_a_port_and_no_api_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            for variable in ("GIDEON_API_QDRANT_URL", "GIDEON_API_EMBED_URL", "GIDEON_API_OPENSEARCH_URL"):
                for url in ("https://fixture-server:7001", "http://fixture-server", "http://fixture-server:7001/v1"):
                    with self.subTest(variable=variable, url=url), self.assertRaisesRegex(
                        ValueError, f"Environment variable {variable} must be an HTTP URL",
                    ):
                        load_settings({**environment, variable: url})

    def test_cas_root_must_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            environment["GIDEON_API_CAS_ROOT"] = "relative/cas"
            with self.assertRaisesRegex(ValueError, "GIDEON_API_CAS_ROOT must be an absolute path"):
                load_settings(environment)

    def test_loaded_settings_repr_and_str_withhold_keys_and_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = self.loaded_settings_environment(Path(directory))
            settings = load_settings(environment)

        settings_repr = repr(settings)
        settings_str = str(settings)
        for text in (settings_repr, settings_str):
            for value in (
                ENGINE_KEY, API_KEY, QDRANT_KEY, EMBED_KEY,
                READER_PASSWORD, CORPUS_READER_PASSWORD, OPENSEARCH_PASSWORD,
            ):
                self.assertNotIn(value, text)
        self.assertIn(f"port={environment['GIDEON_API_PORT']}", settings_repr)
        self.assertIn(MODEL_ID, settings_repr)

    def test_instruction_file_refuses_missing_variable_missing_file_and_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty-instruction"
            empty.write_text(" \n\t", encoding="utf-8")
            environment = self.loaded_settings_environment(root)
            del environment["GIDEON_INSTRUCTION_FILE"]
            with self.assertRaisesRegex(ValueError, "GIDEON_INSTRUCTION_FILE"):
                load_settings(environment)
            for path in (root / "missing-instruction", empty):
                with self.subTest(path=path), self.assertRaisesRegex(
                    ValueError, f"Instruction file {path} is missing or empty"
                ):
                    load_settings({**environment, "GIDEON_INSTRUCTION_FILE": str(path)})

    def test_missing_or_empty_source_settings_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common = self.loaded_settings_environment(Path(directory))
            for variable in (
                "GIDEON_SOURCE_HEADER",
                "GIDEON_CHAT_HEADER",
                "GIDEON_USER_HEADER",
                "GIDEON_EVAL_IDENTITY",
                "GIDEON_MODEL_ID",
                "GIDEON_ENGINE_MODEL",
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
            return httpx.Response(200, json={"data": [{"id": ENGINE_MODEL}]}, request=request)

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
