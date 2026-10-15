"""Loopback numeric reads, failure reasons, and bounded range requests."""

import ast
import http.client
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gideon.host import prometheus
from gideon.host.prometheus import (
    PROMETHEUS_QUERY_PATH,
    PROMETHEUS_RANGE_CHUNK_SECONDS,
    PROMETHEUS_RANGE_QUERY_PATH,
    PROMETHEUS_SCRAPE_STEP_SECONDS,
    PROMETHEUS_TIMEOUT_SECONDS,
    QueryResult,
    instant_query,
    range_query,
)
from gideon.host.render.services.prometheus import PROMETHEUS_LOOPBACK_ADDRESS

ROOT = Path(__file__).resolve().parents[1]
EXPRESSION = 'example_gauge{job="example"}'


class PrometheusReads(unittest.TestCase):
    def setUp(self) -> None:
        self.response = mock.Mock(status=200)
        self.connection = mock.Mock()
        self.connection.getresponse.return_value = self.response
        patcher = mock.patch.object(prometheus.http.client, "HTTPConnection")
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.factory.return_value = self.connection

    def reply(self, result_type: str, result: object) -> None:
        self.response.read.return_value = json.dumps(
            {"status": "success", "data": {"resultType": result_type, "result": result}}
        ).encode()

    def read(
        self, result_type: str
    ) -> QueryResult[float] | QueryResult[dict[int, float]]:
        if result_type == "vector":
            return instant_query(EXPRESSION)
        return range_query(EXPRESSION, 100, 130, PROMETHEUS_SCRAPE_STEP_SECONDS)

    def assert_failure(self, result_type: str, phrase: str) -> None:
        result = self.read(result_type)
        self.assertEqual(
            result,
            QueryResult(None, f"Prometheus {phrase} at {PROMETHEUS_LOOPBACK_ADDRESS}"),
        )
        self.connection.close.assert_called_once_with()
        self.connection.close.reset_mock()

    def test_instant_value_uses_loopback_and_first_sample(self) -> None:
        self.reply("vector", [{"value": [0, "42.5"]}, {"value": [0, "100"]}])
        self.assertEqual(instant_query(EXPRESSION), QueryResult(42.5))
        self.factory.assert_called_once_with(
            PROMETHEUS_LOOPBACK_ADDRESS, timeout=PROMETHEUS_TIMEOUT_SECONDS
        )
        method, target = self.connection.request.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(urlsplit(target).path, PROMETHEUS_QUERY_PATH)
        self.assertEqual(parse_qs(urlsplit(target).query), {"query": [EXPRESSION]})
        self.connection.close.assert_called_once_with()

    def test_empty_instant_is_distinct_from_zero_and_failure(self) -> None:
        self.reply("vector", [])
        self.assertEqual(instant_query(EXPRESSION), QueryResult(None))
        self.reply("vector", [{"value": [0, "0"]}])
        self.assertEqual(instant_query(EXPRESSION), QueryResult(0.0))
        self.reply("vector", [{"value": [0, "-1"]}])
        result = instant_query(EXPRESSION)
        self.assertIsNone(result.samples)
        self.assertEqual(
            result.reason,
            f"Prometheus returned an unreadable value at {PROMETHEUS_LOOPBACK_ADDRESS}",
        )

    def test_transport_failures_close_the_connection_and_hide_diagnostics(self) -> None:
        for result_type in ("vector", "matrix"):
            for stage in (
                self.connection.request,
                self.connection.getresponse,
                self.response.read,
            ):
                for failure, phrase in (
                    (ConnectionRefusedError("private diagnostic"), "did not answer"),
                    (OSError("private diagnostic"), "did not answer"),
                    (TimeoutError("private diagnostic"), "timed out"),
                    (
                        http.client.HTTPException("private diagnostic"),
                        "returned an unreadable reply",
                    ),
                ):
                    with self.subTest(
                        result_type=result_type, stage=stage, failure=type(failure)
                    ):
                        stage.side_effect = failure
                        self.assert_failure(result_type, phrase)
                        stage.side_effect = None

    def test_connection_construction_failure_returns_a_reason(self) -> None:
        self.factory.side_effect = OSError("private diagnostic")
        self.assertEqual(
            instant_query(EXPRESSION),
            QueryResult(
                None, f"Prometheus did not answer at {PROMETHEUS_LOOPBACK_ADDRESS}"
            ),
        )
        self.connection.close.assert_not_called()

    def test_close_error_does_not_replace_the_read(self) -> None:
        self.reply("vector", [{"value": [0, "7"]}])
        self.connection.close.side_effect = OSError("private diagnostic")
        self.assertEqual(instant_query(EXPRESSION), QueryResult(7.0))

    def test_http_status_is_reported_without_reading_the_body(self) -> None:
        self.response.status = 503
        for result_type in ("vector", "matrix"):
            with self.subTest(result_type=result_type):
                self.assert_failure(result_type, "returned HTTP 503")
        self.response.read.assert_not_called()

    def test_unreadable_json_and_non_success_status(self) -> None:
        for result_type in ("vector", "matrix"):
            for body, phrase in (
                (b"private non-JSON reply", "returned unreadable JSON"),
                (b"\xff", "returned unreadable JSON"),
                (
                    b'{"status":"error","error":"private diagnostic"}',
                    "returned a non-success status",
                ),
            ):
                with self.subTest(result_type=result_type, body=body):
                    self.response.read.return_value = body
                    self.assert_failure(result_type, phrase)

    def test_unreadable_envelopes_and_results(self) -> None:
        for result_type in ("vector", "matrix"):
            documents: tuple[object, ...] = (
                [],
                {},
                {"status": 1},
                {"status": "success"},
                {"status": "success", "data": []},
                {"status": "success", "data": {"resultType": "scalar", "result": []}},
                {"status": "success", "data": {"resultType": result_type}},
                {
                    "status": "success",
                    "data": {"resultType": result_type, "result": {}},
                },
            )
            for document in documents:
                with self.subTest(result_type=result_type, document=document):
                    self.response.read.return_value = json.dumps(document).encode()
                    self.assert_failure(result_type, "returned an unreadable result")

    def test_unreadable_series_and_sample_shapes(self) -> None:
        for result_type in ("vector", "matrix"):
            malformed: list[object] = [None, {}, {"values": "private diagnostic"}]
            for sample in (None, [], [0], [0, "1", "2"], [0, 1]):
                malformed.append(
                    {"value": sample}
                    if result_type == "vector"
                    else {"values": [sample]}
                )
            for series in malformed:
                with self.subTest(result_type=result_type, series=series):
                    self.reply(result_type, [series])
                    self.assert_failure(result_type, "returned an unreadable result")

    def test_unreadable_numeric_values(self) -> None:
        for result_type in ("vector", "matrix"):
            for value in (
                "private diagnostic",
                "NaN",
                "Infinity",
                "-Infinity",
                "1e9999",
            ):
                with self.subTest(result_type=result_type, value=value):
                    sample = [100, value]
                    series = (
                        {"value": sample}
                        if result_type == "vector"
                        else {"values": [sample]}
                    )
                    self.reply(result_type, [series])
                    self.assert_failure(result_type, "returned an unreadable value")

    def test_range_sums_series_and_orders_whole_second_timestamps(self) -> None:
        self.reply(
            "matrix",
            [
                {"values": [[130.0, "2.5"], [100, "1"]]},
                {"values": [[115, "-2"], [130, "4.5"]]},
            ],
        )
        result = range_query(EXPRESSION, 100, 130, PROMETHEUS_SCRAPE_STEP_SECONDS)
        self.assertEqual(result, QueryResult({100: 1.0, 115: -2.0, 130: 7.0}))
        self.assertIsNotNone(result.samples)
        assert result.samples is not None
        self.assertEqual(list(result.samples), [100, 115, 130])
        self.connection.close.assert_called_once_with()

    def test_range_rejects_fractional_or_unreadable_timestamps_without_truncating(
        self,
    ) -> None:
        for stamp in (100.5, "100", None, True, float("nan"), float("inf")):
            with self.subTest(stamp=stamp):
                self.reply("matrix", [{"values": [[stamp, "1"]]}])
                self.assert_failure("matrix", "returned an unreadable result")

    def test_nonfinite_series_sum_is_unreadable(self) -> None:
        self.reply(
            "matrix", [{"values": [[100, "1e308"]]}, {"values": [[100, "1e308"]]}]
        )
        self.assert_failure("matrix", "returned an unreadable value")

    def test_empty_range_is_an_empty_mapping(self) -> None:
        self.reply("matrix", [])
        self.assertEqual(
            range_query(EXPRESSION, 100, 130, PROMETHEUS_SCRAPE_STEP_SECONDS),
            QueryResult({}),
        )

    def test_range_chunks_cover_the_grid_once_with_bounded_requests(self) -> None:
        day = int(timedelta(days=1).total_seconds())
        self.assertEqual(PROMETHEUS_RANGE_CHUNK_SECONDS, day)
        requests: list[tuple[int, int]] = []

        def request(method: str, target: str) -> None:
            self.assertEqual(method, "GET")
            parsed = urlsplit(target)
            self.assertEqual(parsed.path, PROMETHEUS_RANGE_QUERY_PATH)
            parameters = parse_qs(parsed.query)
            self.assertEqual(parameters["query"], [EXPRESSION])
            step = int(parameters["step"][0])
            start, end = int(parameters["start"][0]), int(parameters["end"][0])
            requests.append((start, end))
            self.assertLessEqual(end - start, day)
            self.reply(
                "matrix",
                [{"values": [[stamp, "3"] for stamp in range(start, end + 1, step)]}],
            )

        for step in (PROMETHEUS_SCRAPE_STEP_SECONDS, 17, day + 1):
            with self.subTest(step=step):
                self.connection.reset_mock()
                requests.clear()
                self.connection.request.side_effect = request
                start, end = 103, 103 + 2 * day + 37
                result = range_query(EXPRESSION, start, end, step)
                self.assertEqual(
                    result, QueryResult(dict.fromkeys(range(start, end + 1, step), 3.0))
                )
                self.assertGreater(len(requests), 1)
                self.assertEqual(requests[0], (start, start + (day // step) * step))
                self.assertEqual(self.connection.close.call_count, len(requests))
                for call in self.connection.request.call_args_list:
                    self.assertEqual(
                        parse_qs(urlsplit(call.args[1]).query)["step"], [str(step)]
                    )

    def test_one_point_range_still_queries(self) -> None:
        self.reply("matrix", [{"values": [[100, "3"]]}])
        self.assertEqual(
            range_query(EXPRESSION, 100, 100, PROMETHEUS_SCRAPE_STEP_SECONDS),
            QueryResult({100: 3.0}),
        )
        self.connection.request.assert_called_once()

    def test_range_arguments_are_validated_without_truncation_or_a_request(
        self,
    ) -> None:
        for start, end, step in (
            (100.5, 130, 15),
            (100, 130.5, 15),
            (100, 130, 1.5),
            (True, 130, 15),
            (100, 99, 15),
            (100, 130, 0),
            (100, 130, -1),
        ):
            with self.subTest(start=start, end=end, step=step):
                self.assertRaises(ValueError, range_query, EXPRESSION, start, end, step)
        self.factory.assert_not_called()

    def test_later_chunk_failure_discards_samples_and_stops_reading(self) -> None:
        self.reply("matrix", [{"values": [[100, "3"]]}])
        self.connection.getresponse.side_effect = [
            self.response,
            TimeoutError("private diagnostic"),
        ]
        result = range_query(
            EXPRESSION,
            100,
            100 + 3 * PROMETHEUS_RANGE_CHUNK_SECONDS,
            PROMETHEUS_SCRAPE_STEP_SECONDS,
        )
        self.assertEqual(
            result,
            QueryResult(None, f"Prometheus timed out at {PROMETHEUS_LOOPBACK_ADDRESS}"),
        )
        self.assertEqual(self.connection.request.call_count, 2)
        self.assertEqual(self.connection.close.call_count, 2)


class PrometheusContract(unittest.TestCase):
    def test_scrape_step_matches_the_template(self) -> None:
        template = (ROOT / "compose/prometheus/prometheus.yml.tmpl").read_text()
        interval = next(
            line.split(":", 1)[1].strip()
            for line in template.splitlines()
            if line.strip().startswith("scrape_interval:")
        )
        self.assertEqual(interval, f"{PROMETHEUS_SCRAPE_STEP_SECONDS}s")

    def test_imports_are_stdlib_except_for_the_address_owner(self) -> None:
        tree = ast.parse((ROOT / "gideon/host/prometheus.py").read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                if node.module == "gideon.host.render.services.prometheus":
                    self.assertEqual(
                        [alias.name for alias in node.names],
                        ["PROMETHEUS_LOOPBACK_ADDRESS"],
                    )
                else:
                    imports.append(node.module or "")
        self.assertTrue(imports)
        for name in imports:
            self.assertIn(name.split(".")[0], sys.stdlib_module_names)
            self.assertNotEqual(name, "urllib.request")
