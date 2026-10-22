"""Derived-store requests, replies, counts, cleanup, and refusal contracts."""

import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from gideon.host import derivedstores, report, secrets, stack
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REPOSITORY,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_SNAPSHOTS_MOUNT,
    OPENSEARCH_SNAPSHOTS_ROOT,
)
from gideon.host.render.qdrant import (
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
    QDRANT_SNAPSHOTS_TEMP_DIR,
)
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
Q_ROOT = "/data/fast/qdrant-snapshots"
NOW = datetime(2026, 10, 11, 12, 34, 56, tzinfo=UTC)
Q_KEY = "fictitious-qdrant-key"
OS_PASSWORD = 'fictitious\\"password'


def q_reply(data: object, status: int = 200, *, length: int | None = None) -> str:
    body = json.dumps(data, separators=(",", ":"))
    size = len(body.encode()) if length is None else length
    return f"HTTP/1.0 {status} OK\r\ncontent-length: {size}\r\n\r\n{body}"


def os_reply(data: object, status: int = 200) -> str:
    return json.dumps(data, separators=(",", ":")) + f"\ngideon-status:{status}\n"


@dataclass(frozen=True)
class Step:
    service: str
    method: str
    path: str
    stdout: str = ""
    returncode: int = 0
    stderr: str = ""
    error: Exception | None = None


class FakeHost:
    def __init__(
        self, steps: Sequence[Step], *, secrets_present: bool = True,
        files: Mapping[str, str] | None = None,
    ) -> None:
        self.steps = list(steps)
        self.files = (
            {
                os.fspath(secrets.secret_path(QDRANT_SECRET_NAME)): Q_KEY + "\n",
                os.fspath(
                    secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME)
                ): OS_PASSWORD + "\n",
            }
            if secrets_present
            else {}
        )
        self.files.update(files or {})
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.reads: list[str] = []

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, passthrough
        self.assert_no_environment(env)
        command = tuple(argv)
        self.calls.append((command, input, timeout))
        if not self.steps:
            raise AssertionError(f"unexpected child: {command}")
        step = self.steps.pop(0)
        if step.service in {QDRANT_SERVICE_NAME, OPENSEARCH_SERVICE_NAME}:
            assert command[:2] == ("docker", "compose")
            assert command == tuple(
                stack.exec_argv(
                    RENDERED,
                    step.service,
                    *(command[command.index(step.service) + 1 :]),
                )
            )
            assert input is not None
            if step.service == QDRANT_SERVICE_NAME:
                assert input.startswith(f"{step.method} {step.path} HTTP/1.0\r\n")
            else:
                assert f'request = "{step.method}"' in input
                assert f"127.0.0.1:9200{step.path}" in input
        elif step.service == "find":
            assert command[:2] == ("find", Q_ROOT)
            assert command[-2:] in (("-printf", "%f\\0"), ("-printf", "%P\\0"))
        elif step.service == "rm":
            assert command[:5] == ("rm", "-rf", "--one-file-system", "--", step.path)
        else:
            raise AssertionError(step.service)
        if step.error is not None:
            raise step.error
        return subprocess.CompletedProcess(
            list(command), step.returncode, step.stdout, step.stderr
        )

    @staticmethod
    def assert_no_environment(env: Mapping[str, str] | None) -> None:
        assert env is None

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644
    ) -> None:
        raise AssertionError("unexpected write")

    def exists(self, path: PathLike) -> bool:
        raise AssertionError("unexpected exists")

    def listdir(self, path: PathLike) -> list[str]:
        raise AssertionError("unexpected listdir")

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise AssertionError("unexpected unlink")

    def stat(self, path: PathLike) -> os.stat_result:
        raise AssertionError("unexpected stat")

    def chmod(self, path: PathLike, mode: int) -> None:
        raise AssertionError("unexpected chmod")

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        raise AssertionError("unexpected chown")

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        raise AssertionError("unexpected mkdir")

    def geteuid(self) -> int:
        return 0


def q_step(method: str, path: str, data: object, status: int = 200) -> Step:
    return Step(QDRANT_SERVICE_NAME, method, path, q_reply(data, status))


def os_step(method: str, path: str, data: object, status: int = 200) -> Step:
    return Step(OPENSEARCH_SERVICE_NAME, method, path, os_reply(data, status))


class Counts(unittest.TestCase):
    def test_qdrant_collection_names_are_sorted_unique_and_allow_an_empty_store(self) -> None:
        for names, expected in (([], ()), (["fictitious-z", "fictitious-a", "fictitious-z"], ("fictitious-a", "fictitious-z"))):
            with self.subTest(names=names):
                host = FakeHost([q_step("GET", "/collections", {"result": {"collections": [{"name": name} for name in names]}})])
                self.assertEqual(derivedstores.qdrant_collection_names(host, RENDERED, Q_KEY), expected)
                self.assertEqual(len(host.calls), 1)
                argv, request, _ = host.calls[0]
                assert request is not None
                self.assertIn(f"api-key: {Q_KEY}\r\n", request)
                self.assertNotIn(Q_KEY, str(argv))
                self.assertEqual(host.steps, [])

    def test_qdrant_collection_listing_refuses_off_shape_and_failed_replies(self) -> None:
        bodies: tuple[object, ...] = (None, [], {}, {"result": {}}, {"result": {"collections": None}},
                                     {"result": {"collections": [None]}}, {"result": {"collections": [{"name": "../unsafe"}]}})
        for body in bodies:
            with self.subTest(body=body):
                host = FakeHost([q_step("GET", "/collections", body)])
                result = derivedstores.qdrant_collection_names(host, RENDERED, Q_KEY)
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertEqual(result.fix, stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME))
                self.assertEqual(len(host.calls), 1)
        for code in (3, 127):
            with self.subTest(code=code):
                host = FakeHost([Step(QDRANT_SERVICE_NAME, "GET", "/collections", returncode=code)])
                result = derivedstores.qdrant_collection_names(host, RENDERED, Q_KEY)
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn(f"exit {code}", result.problem)
                self.assertEqual(len(host.calls), 1)

    def test_opensearch_count_request_and_password_on_stdin(self) -> None:
        for count in (0, 7):
            with self.subTest(count=count):
                host = FakeHost([os_step("GET", "/fictitious-index/_count", {"count": count})])
                self.assertEqual(derivedstores.opensearch_document_count(
                    host, RENDERED, OS_PASSWORD, "fictitious-index",
                ), count)
                argv, request, _ = host.calls[0]
                assert request is not None
                user_line = next(line for line in request.splitlines() if line.startswith("user = "))
                self.assertTrue(json.loads(user_line.removeprefix("user = ")).endswith(":" + OS_PASSWORD))
                self.assertNotIn("data =", request)
                self.assertNotIn(OS_PASSWORD, str(argv))
                self.assertEqual(len(host.calls), 1)
                self.assertEqual(host.steps, [])

    def test_qdrant_count_is_exact_and_key_is_on_stdin(self) -> None:
        for count in (0, 7):
            with self.subTest(count=count):
                host = FakeHost([q_step(
                    "POST", "/collections/fictitious-collection/points/count",
                    {"result": {"count": count}},
                )])
                self.assertEqual(derivedstores.qdrant_point_count(
                    host, RENDERED, Q_KEY, "fictitious-collection",
                ), count)
                argv, request, _ = host.calls[0]
                assert request is not None
                headers, _, body = request.partition("\r\n\r\n")
                self.assertEqual(json.loads(body), {"exact": True})
                self.assertIn(f"Content-Length: {len(body.encode())}", headers)
                self.assertIn(f"api-key: {Q_KEY}\r\n", request)
                self.assertNotIn(Q_KEY, str(argv))
                self.assertEqual(len(host.calls), 1)
                self.assertEqual(host.steps, [])

    def test_missing_names_are_absent_regardless_of_error_body(self) -> None:
        bodies: tuple[object, ...] = ({"error": "fictitious missing name"}, [], None)
        for body in bodies:
            for read, step, secret in (
                (derivedstores.opensearch_document_count,
                 os_step("GET", "/fictitious/_count", body, 404), OS_PASSWORD),
                (derivedstores.qdrant_point_count,
                 q_step("POST", "/collections/fictitious/points/count", body, 404), Q_KEY),
            ):
                with self.subTest(store=step.service, body=body):
                    host = FakeHost([step])
                    self.assertIsNone(read(host, RENDERED, secret, "fictitious"))
                    self.assertEqual(len(host.calls), 1)
                    self.assertEqual(host.steps, [])

    def test_malformed_counts_refuse_with_store_logs_fix(self) -> None:
        bad_counts: tuple[object, ...] = (None, True, False, -1, 1.5, "7", [], {})
        opensearch_bodies: tuple[object, ...] = (
            None, [], {}, *({"count": count} for count in bad_counts),
        )
        qdrant_bodies: tuple[object, ...] = (
            None, [], {}, {"result": []}, {"result": {}}, {"points_count": 7},
            *({"result": {"count": count}} for count in bad_counts),
        )
        for read, step_for, secret, bodies in (
            (derivedstores.opensearch_document_count,
             lambda body: os_step("GET", "/fictitious/_count", body), OS_PASSWORD,
             opensearch_bodies),
            (derivedstores.qdrant_point_count,
             lambda body: q_step("POST", "/collections/fictitious/points/count", body), Q_KEY,
             qdrant_bodies),
        ):
            for body in bodies:
                step = step_for(body)
                with self.subTest(store=step.service, body=body):
                    host = FakeHost([step])
                    result = read(host, RENDERED, secret, "fictitious")
                    assert isinstance(result, report.Problem)
                    self.assertIn("invalid", result.problem)
                    self.assertEqual(result.fix, stack.logs_fix(RENDERED, step.service))
                    self.assertNotIn(secret, repr(result))
                    self.assertEqual(len(host.calls), 1)

    def test_unsafe_names_refuse_before_any_request(self) -> None:
        for read, secret in (
            (derivedstores.opensearch_document_count, OS_PASSWORD),
            (derivedstores.qdrant_point_count, Q_KEY),
        ):
            for name in ("", ".", "..", "../escape", "a/b", "a?query", "a\nb"):
                with self.subTest(read=read.__name__, name=name):
                    host = FakeHost([])
                    result = read(host, RENDERED, secret, name)
                    assert isinstance(result, report.Problem)
                    self.assertIn("unsafe name", result.problem)
                    self.assertEqual(host.calls, [])

    def test_failed_requests_refuse_without_retry_even_with_valid_count(self) -> None:
        for read, secret, good, rejected in (
            (derivedstores.opensearch_document_count, OS_PASSWORD,
             os_step("GET", "/fictitious/_count", {"count": 7}),
             os_step("GET", "/fictitious/_count", {"count": 7}, 500)),
            (derivedstores.qdrant_point_count, Q_KEY,
             q_step("POST", "/collections/fictitious/points/count", {"result": {"count": 7}}),
             q_step("POST", "/collections/fictitious/points/count", {"result": {"count": 7}}, 500)),
        ):
            for step in (
                rejected,
                Step(good.service, good.method, good.path, good.stdout, returncode=7),
                Step(good.service, good.method, good.path, good.stdout, returncode=127),
                Step(good.service, good.method, good.path, error=FileNotFoundError()),
            ):
                with self.subTest(store=step.service, code=step.returncode, error=step.error):
                    host = FakeHost([step])
                    result = read(host, RENDERED, secret, "fictitious")
                    assert isinstance(result, report.Problem)
                    self.assertEqual(result.fix, stack.logs_fix(RENDERED, step.service))
                    self.assertEqual(len(host.calls), 1)
                    self.assertEqual(host.steps, [])


class Clients(unittest.TestCase):
    def test_qdrant_request_uses_http10_on_stdin(self) -> None:
        host = FakeHost(
            [
                q_step(
                    "POST",
                    "/collections/a/snapshots?wait=true",
                    {"result": {"name": "a.snapshot", "size": 7}},
                )
            ]
        )
        reply = derivedstores.qdrant_request(
            host,
            RENDERED,
            Q_KEY,
            "POST",
            "/collections/a/snapshots?wait=true",
            body={"fixture": "text"},
            bound=derivedstores.STORE_SNAPSHOT_WAIT_SECONDS,
        )
        self.assertNotIsInstance(reply, report.Problem)
        argv, request, timeout = host.calls[0]
        self.assertEqual(
            argv,
            tuple(
                stack.exec_argv(
                    RENDERED,
                    QDRANT_SERVICE_NAME,
                    "bash",
                    "-c",
                    derivedstores._QDRANT_TCP_SCRIPT,
                    derivedstores._QDRANT_TCP_LABEL,
                    "6333",
                )
            ),
        )
        assert request is not None
        self.assertIn(f"api-key: {Q_KEY}\r\n", request)
        self.assertIn("Content-Length: 18\r\n", request)
        self.assertIn("Connection: close\r\n", request)
        self.assertIn("Content-Type: application/json\r\n", request)
        self.assertTrue(request.endswith('\r\n\r\n{"fixture":"text"}'))
        self.assertEqual(
            timeout,
            derivedstores.STORE_SNAPSHOT_WAIT_SECONDS
            + derivedstores.STORE_REQUEST_MARGIN_SECONDS,
        )
        self.assertNotIn(Q_KEY, str(argv))

        listing = FakeHost(
            [q_step("GET", "/collections", {"result": {"collections": []}})]
        )
        derivedstores.qdrant_request(listing, RENDERED, Q_KEY, "GET", "/collections")
        no_body = listing.calls[0][1]
        assert no_body is not None
        self.assertIn("Content-Length: 0\r\n", no_body)
        self.assertNotIn("Content-Type", no_body)

    def test_opensearch_request_quotes_credential_and_body_on_stdin(self) -> None:
        host = FakeHost([os_step("PUT", "/_snapshot/gideon", {"acknowledged": True})])
        reply = derivedstores.opensearch_request(
            host,
            RENDERED,
            OS_PASSWORD,
            "PUT",
            "/_snapshot/gideon",
            body={"location": 'fixture\\"path'},
        )
        self.assertNotIsInstance(reply, report.Problem)
        argv, request, timeout = host.calls[0]
        self.assertEqual(
            argv,
            tuple(
                stack.exec_argv(
                    RENDERED, OPENSEARCH_SERVICE_NAME, "curl", "-s", "-S", "-K", "-"
                )
            ),
        )
        assert request is not None
        self.assertIn('user = "gideon:fictitious', request)
        self.assertIn('url = "http://127.0.0.1:9200/_snapshot/gideon"', request)
        self.assertIn('request = "PUT"', request)
        self.assertIn(f"max-time = {derivedstores.STORE_LIST_WAIT_SECONDS}", request)
        self.assertIn('write-out = "', request)
        self.assertIn("gideon-status:%{http_code}", request)
        self.assertIn('header = "Content-Type: application/json"', request)
        self.assertIn('data = "', request)
        self.assertEqual(
            timeout,
            derivedstores.STORE_LIST_WAIT_SECONDS
            + derivedstores.STORE_REQUEST_MARGIN_SECONDS,
        )
        self.assertNotIn(OS_PASSWORD, str(argv))

    def test_parsers_refuse_invalid_replies_and_include_bounded_server_errors(
        self,
    ) -> None:
        q_cases = (
            (q_reply({"result": None}, 202), "HTTP 202"),
            (q_reply({"status": {"error": "forbidden"}}, 403), "forbidden"),
            ("HTTP/1.0 200 OK\r\n\r\nnot-json", "not JSON"),
            (q_reply({"result": []}, length=100), "truncated"),
        )
        for raw, words in q_cases:
            with self.subTest(words=words):
                host = FakeHost([Step(QDRANT_SERVICE_NAME, "GET", "/collections", raw)])
                reply = derivedstores.qdrant_request(
                    host, RENDERED, Q_KEY, "GET", "/collections"
                )
                self.assertIsInstance(reply, report.Problem)
                assert isinstance(reply, report.Problem)
                self.assertIn(words, reply.problem)
                self.assertEqual(
                    reply.fix, stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME)
                )
                self.assertNotIn(Q_KEY, repr(reply))

        normalized = q_reply({"result": {"collections": []}}).replace("\r\n", "\n")
        host = FakeHost([Step(QDRANT_SERVICE_NAME, "GET", "/collections", normalized)])
        reply = derivedstores.qdrant_request(host, RENDERED, Q_KEY, "GET", "/collections")
        self.assertNotIsInstance(reply, report.Problem)

        os_cases = (
            ('{"snapshots":[]}', "status trailer"),
            ("not-json\ngideon-status:200\n", "not JSON"),
            (
                os_reply(
                    {
                        "error": {
                            "type": "outer",
                            "reason": "bad",
                            "caused_by": {"type": "inner", "reason": "denied"},
                        }
                    },
                    500,
                ),
                "inner: denied",
            ),
        )
        for raw, words in os_cases:
            with self.subTest(words=words):
                host = FakeHost(
                    [
                        Step(
                            OPENSEARCH_SERVICE_NAME,
                            "GET",
                            "/_snapshot/gideon/_all",
                            raw,
                        )
                    ]
                )
                reply = derivedstores.opensearch_request(
                    host, RENDERED, OS_PASSWORD, "GET", "/_snapshot/gideon/_all"
                )
                self.assertIsInstance(reply, report.Problem)
                assert isinstance(reply, report.Problem)
                self.assertIn(words, reply.problem)
                self.assertEqual(
                    reply.fix, stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME)
                )
                self.assertNotIn(OS_PASSWORD, repr(reply))

    def test_child_failures_are_refusals_without_credentials(self) -> None:
        cases = (
            (subprocess.TimeoutExpired(["curl"], 2), report.Timeout, "timed out"),
            (OSError("fixture executable absent"), report.Problem, "could not run"),
        )
        for error, kind, words in cases:
            with self.subTest(error=type(error).__name__):
                host = FakeHost(
                    [
                        Step(
                            OPENSEARCH_SERVICE_NAME,
                            "GET",
                            "/_snapshot/gideon/_all",
                            error=error,
                        )
                    ]
                )
                reply = derivedstores.opensearch_request(
                    host, RENDERED, OS_PASSWORD, "GET", "/_snapshot/gideon/_all"
                )
                self.assertIsInstance(reply, kind)
                assert isinstance(reply, report.Problem)
                self.assertIn(words, reply.problem)
                self.assertEqual(
                    reply.fix, stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME)
                )
                self.assertNotIn(OS_PASSWORD, repr(reply))
        host = FakeHost(
            [
                Step(
                    QDRANT_SERVICE_NAME,
                    "GET",
                    "/collections",
                    returncode=7,
                    stderr=f"first\nlast {Q_KEY}\n",
                )
            ]
        )
        reply = derivedstores.qdrant_request(
            host, RENDERED, Q_KEY, "GET", "/collections"
        )
        self.assertIsInstance(reply, report.Problem)
        assert isinstance(reply, report.Problem)
        self.assertIn("last [redacted]", reply.problem)
        self.assertNotIn("first", reply.problem)
        self.assertNotIn(Q_KEY, repr(reply))


class SnapshotAll(unittest.TestCase):
    def test_full_sequence_keeps_new_snapshots_and_removes_stale_directory(
        self,
    ) -> None:
        created_name = "gideon-20261011t123456z"
        steps = [
            q_step(
                "GET",
                "/collections",
                {"result": {"collections": [{"name": "z"}, {"name": "a"}]}},
            ),
            q_step(
                "POST",
                "/collections/a/snapshots?wait=true",
                {"result": {"name": "new-a.snapshot", "size": 7}},
            ),
            q_step(
                "GET",
                "/collections/a/snapshots",
                {"result": [{"name": "old-a.snapshot"}, {"name": "new-a.snapshot"}]},
            ),
            q_step(
                "DELETE",
                "/collections/a/snapshots/old-a.snapshot?wait=true",
                {"result": True},
            ),
            q_step(
                "POST",
                "/collections/z/snapshots?wait=true",
                {"result": {"name": "new-z.snapshot", "size": 11}},
            ),
            q_step(
                "GET",
                "/collections/z/snapshots",
                {"result": [{"name": "new-z.snapshot"}]},
            ),
            Step("find", "", "", f"old\0a\0z\0{QDRANT_SNAPSHOTS_TEMP_DIR}\0"),
            Step("rm", "", f"{Q_ROOT}/old"),
            os_step(
                "PUT", f"/_snapshot/{OPENSEARCH_REPOSITORY}", {"acknowledged": True}
            ),
            os_step(
                "GET",
                "/_cat/indices/*,-.*?format=json&expand_wildcards=open,hidden",
                [{"index": "z"}, {"index": "a"}],
            ),
            os_step(
                "PUT",
                f"/_snapshot/{OPENSEARCH_REPOSITORY}/{created_name}?wait_for_completion=true",
                {
                    "snapshot": {
                        "state": "SUCCESS",
                        "shards": {"failed": 0},
                        "indices": ["z", "a"],
                    }
                },
            ),
            os_step(
                "GET",
                f"/_snapshot/{OPENSEARCH_REPOSITORY}/_all",
                {"snapshots": [{"snapshot": "old"}, {"snapshot": created_name}]},
            ),
            os_step(
                "DELETE",
                f"/_snapshot/{OPENSEARCH_REPOSITORY}/old",
                {"acknowledged": True},
            ),
        ]
        host = FakeHost(steps)
        times = iter((10.0, 13.25, 20.0, 25.5))

        result = derivedstores.snapshot_all(
            host, RENDERED, now=NOW, qdrant_root=Q_ROOT, clock=lambda: next(times)
        )

        self.assertEqual(
            result,
            (
                derivedstores.StoreSnapshot(QDRANT_SERVICE_NAME, ("a", "z"), 3.25, 18),
                derivedstores.StoreSnapshot(
                    OPENSEARCH_SERVICE_NAME, ("a", "z"), 5.5, None
                ),
            ),
        )
        assert isinstance(result, tuple)
        self.assertEqual(
            derivedstores.snapshot_detail(result),
            "qdrant: 2 collection(s) in 3.2 s; opensearch: 2 index(es) in 5.5 s",
        )
        self.assertEqual(
            derivedstores.snapshot_audit(result),
            {
                "qdrant": {"names": ["a", "z"], "seconds": 3.25, "bytes": 18},
                "opensearch": {"names": ["a", "z"], "seconds": 5.5, "bytes": None},
            },
        )
        self.assertEqual(host.steps, [])
        self.assertEqual(
            host.reads.count(os.fspath(secrets.secret_path(QDRANT_SECRET_NAME))), 1
        )
        self.assertEqual(
            host.reads.count(
                os.fspath(secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME))
            ),
            1,
        )
        self.assertEqual(
            host.calls[7][0], ("rm", "-rf", "--one-file-system", "--", f"{Q_ROOT}/old")
        )
        self.assertTrue(
            all(
                Q_KEY not in str(argv) and OS_PASSWORD not in str(argv)
                for argv, _, _ in host.calls
            )
        )
        self.assertNotIn(Q_KEY, derivedstores.snapshot_detail(result))
        self.assertNotIn(OS_PASSWORD, derivedstores.snapshot_detail(result))
        register_input = host.calls[8][1]
        assert register_input is not None
        self.assertIn(OPENSEARCH_SNAPSHOTS_MOUNT, register_input)
        self.assertIn(derivedstores.OPENSEARCH_SNAPSHOT_RATE, register_input)
        self.assertIn('\\"compress\\":false', register_input)
        create_input = host.calls[10][1]
        assert create_input is not None
        self.assertIn('\\"indices\\":\\"*,-.*\\"', create_input)

    def test_refused_qdrant_snapshot_never_runs_cleanup_or_next_store(self) -> None:
        host = FakeHost(
            [
                q_step(
                    "GET", "/collections", {"result": {"collections": [{"name": "a"}]}}
                ),
                q_step(
                    "POST",
                    "/collections/a/snapshots?wait=true",
                    {"status": {"error": "snapshot refused"}},
                    403,
                ),
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("snapshot refused", result.problem)
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.steps, [])


    def test_repository_verification_refuses_with_provision_fix(self) -> None:
        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", ""),
                os_step(
                    "PUT",
                    f"/_snapshot/{OPENSEARCH_REPOSITORY}",
                    {
                        "error": {
                            "type": "repository_exception",
                            "reason": "bad mount",
                            "caused_by": {
                                "type": "repository_verification_exception",
                                "reason": "unwritable",
                            },
                        }
                    },
                    500,
                ),
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("repository_verification_exception", result.problem)
        self.assertEqual(
            result.fix,
            f"Run {report.command('host provision --only disk-layout')}, then retry.",
        )
        self.assertEqual(host.steps, [])

    def test_missing_secret_refuses_before_any_child_with_apply_fix(self) -> None:
        host = FakeHost([], secrets_present=False)
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn(
            os.fspath(secrets.secret_path(QDRANT_SECRET_NAME)), result.problem
        )
        self.assertEqual(result.fix, f"Run {report.command('apply')}, then retry.")
        self.assertEqual(host.calls, [])

        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", ""),
            ]
        )
        del host.files[os.fspath(secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME))]
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn(
            os.fspath(secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME)),
            result.problem,
        )
        self.assertEqual(result.fix, f"Run {report.command('apply')}, then retry.")
        self.assertEqual(len(host.calls), 2)

    def test_empty_or_multiline_secret_refuses_before_a_child(self) -> None:
        for value, words in ((" \n", "empty"), ("first\nsecond", "invalid character")):
            with self.subTest(words=words):
                host = FakeHost([])
                host.files[os.fspath(secrets.secret_path(QDRANT_SECRET_NAME))] = value
                result = derivedstores.snapshot_all(
                    host, RENDERED, now=NOW, qdrant_root=Q_ROOT
                )
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn(words, result.problem)
                self.assertEqual(host.calls, [])

    def test_empty_stores_retire_old_snapshots_and_return_named_zeros(self) -> None:
        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", f"{QDRANT_SNAPSHOTS_TEMP_DIR}\0old\0"),
                Step("rm", "", f"{Q_ROOT}/old"),
                os_step(
                    "PUT", f"/_snapshot/{OPENSEARCH_REPOSITORY}", {"acknowledged": True}
                ),
                os_step(
                    "GET",
                    "/_cat/indices/*,-.*?format=json&expand_wildcards=open,hidden",
                    [],
                ),
                os_step(
                    "GET",
                    f"/_snapshot/{OPENSEARCH_REPOSITORY}/_all",
                    {"snapshots": [{"snapshot": "old"}]},
                ),
                os_step(
                    "DELETE",
                    f"/_snapshot/{OPENSEARCH_REPOSITORY}/old",
                    {"acknowledged": True},
                ),
            ]
        )
        times = iter((1.0, 2.0, 3.0, 4.0))
        result = derivedstores.snapshot_all(
            host, RENDERED, now=NOW, qdrant_root=Q_ROOT, clock=lambda: next(times)
        )
        self.assertEqual(
            result,
            (
                derivedstores.StoreSnapshot(QDRANT_SERVICE_NAME, (), 1.0, 0),
                derivedstores.StoreSnapshot(OPENSEARCH_SERVICE_NAME, (), 1.0, None),
            ),
        )
        self.assertEqual(host.steps, [])

    def test_unsafe_name_refuses_before_it_enters_a_url_or_host_command(self) -> None:
        host = FakeHost(
            [
                q_step(
                    "GET",
                    "/collections",
                    {"result": {"collections": [{"name": "../escape"}]}},
                )
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("unsafe name", result.problem)
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.steps, [])

        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", "../escape\0"),
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.steps, [])

        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", ""),
                os_step(
                    "PUT", f"/_snapshot/{OPENSEARCH_REPOSITORY}", {"acknowledged": True}
                ),
                os_step(
                    "GET",
                    "/_cat/indices/*,-.*?format=json&expand_wildcards=open,hidden",
                    [{"index": "bad/name"}],
                ),
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        self.assertEqual(len(host.calls), 4)
        self.assertEqual(host.steps, [])

        host = FakeHost(
            [
                q_step("GET", "/collections", {"result": {"collections": []}}),
                Step("find", "", "", "one\ntwo\0"),
            ]
        )
        result = derivedstores.snapshot_all(host, RENDERED, now=NOW, qdrant_root=Q_ROOT)
        self.assertIsInstance(result, report.Problem)
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.steps, [])

class RecoverAll(unittest.TestCase):
    def test_recovers_newest_files_and_snapshot_after_deleting_indices(self) -> None:
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        snapshots = [
            {"snapshot": "new", "start_time_in_millis": 200, "state": "SUCCESS", "shards": {"failed": 0}, "indices": ["z", "a"]},
            {"snapshot": "old", "start_time_in_millis": 100, "state": "SUCCESS", "shards": {"failed": 0}, "indices": ["old-index"]},
        ]
        steps = [
            Step("find", "", "", "tmp/junk.snapshot\0a/a-20260901t120000z.snapshot\0a/a-20260902t120000z.snapshot\0b/only.snapshot\0"),
            q_step("PUT", "/collections/a/snapshots/recover?wait=true", {"result": True}),
            q_step("PUT", "/collections/b/snapshots/recover?wait=true", {"result": True}),
            os_step("DELETE", repository, {"error": "missing"}, 404),
            os_step("PUT", repository, {"acknowledged": True}),
            os_step("GET", f"{repository}/_all", {"snapshots": snapshots}),
            os_step("DELETE", "/a", {"error": "missing"}, 404),
            os_step("DELETE", "/z", {"acknowledged": True}),
            os_step("POST", f"{repository}/new/_restore?wait_for_completion=true", {"snapshot": {"shards": {"failed": 0}}}),
        ]
        checksum = "a" * 64
        host = FakeHost(steps, files={f"{Q_ROOT}/a/a-20260902t120000z.snapshot.checksum": checksum})
        times = iter((10.0, 12.25, 20.0, 23.5))

        result = derivedstores.recover_all(
            host, RENDERED,
            present_sources={Q_ROOT, OPENSEARCH_SNAPSHOTS_ROOT},
            qdrant_root=Q_ROOT, clock=lambda: next(times),
        )

        self.assertEqual(result, (
            derivedstores.StoreRecovery("qdrant", ("a", "b"), 2.25, False),
            derivedstores.StoreRecovery("opensearch", ("a", "z"), 3.5, False),
        ))
        assert isinstance(result, tuple)
        self.assertEqual(host.steps, [])
        self.assertEqual(host.calls[0][0], (
            "find", Q_ROOT, "-mindepth", "2", "-maxdepth", "2", "-type", "f",
            "-name", "*.snapshot", "-printf", "%P\\0",
        ))
        a_request = host.calls[1][1]
        b_request = host.calls[2][1]
        assert a_request is not None and b_request is not None
        self.assertIn("file:///qdrant/snapshots/a/a-20260902t120000z.snapshot", a_request)
        self.assertNotIn("a-20260901t120000z.snapshot", a_request)
        self.assertIn(f'"checksum":"{checksum}"', a_request)
        self.assertNotIn('"checksum"', b_request)
        self.assertIn('request = "DELETE"', host.calls[6][1] or "")
        self.assertIn('request = "DELETE"', host.calls[7][1] or "")
        self.assertIn('request = "POST"', host.calls[8][1] or "")
        restore_request = host.calls[-1][1]
        assert restore_request is not None
        self.assertIn(r'\"indices\":\"a,z\"', restore_request)
        self.assertIn(r'\"include_global_state\":false', restore_request)
        self.assertEqual(
            tuple(derivedstores.recovery_clause(item) for item in result),
            ("qdrant: 2 collection(s) recovered in 2.2 s", "opensearch: 2 index(es) restored in 3.5 s"),
        )
        for pattern, item in zip(derivedstores.recovery_clause_patterns(), result, strict=True):
            self.assertIsNotNone(pattern.fullmatch(derivedstores.recovery_clause(item)))
        self.assertEqual(derivedstores.recovery_audit(result), {
            "qdrant": {"names": ["a", "b"], "seconds": 2.25},
            "opensearch": {"names": ["a", "z"], "seconds": 3.5},
        })

    def test_an_unusable_newer_snapshot_never_costs_a_live_index(self) -> None:
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        good = {"snapshot": "good", "start_time_in_millis": 100, "state": "SUCCESS",
                "shards": {"failed": 0}, "indices": ["a"]}
        unusable = (
            {"snapshot": "failed", "start_time_in_millis": 200, "state": "FAILED",
             "shards": {"failed": 0}, "indices": ["a", "b"]},
            {"snapshot": "partial", "start_time_in_millis": 300, "state": "SUCCESS",
             "shards": {"failed": 1}, "indices": ["a", "b"]},
        )
        for newer in unusable:
            with self.subTest(newer=newer["snapshot"]):
                host = FakeHost([
                    os_step("DELETE", repository, {"acknowledged": True}),
                    os_step("PUT", repository, {"acknowledged": True}),
                    os_step("GET", f"{repository}/_all", {"snapshots": [newer, good]}),
                    os_step("DELETE", "/a", {"acknowledged": True}),
                    os_step("POST", f"{repository}/good/_restore?wait_for_completion=true",
                            {"snapshot": {"shards": {"failed": 0}}}),
                ])
                result = derivedstores.recover_all(
                    host, RENDERED, present_sources={OPENSEARCH_SNAPSHOTS_ROOT},
                )
                assert isinstance(result, tuple)
                self.assertEqual(result[1].names, ("a",))
                self.assertEqual(host.steps, [])
                self.assertFalse(any("/b" in (request or "") for _, request, _ in host.calls))
        host = FakeHost([
            os_step("DELETE", repository, {"acknowledged": True}),
            os_step("PUT", repository, {"acknowledged": True}),
            os_step("GET", f"{repository}/_all", {"snapshots": list(unusable)}),
        ])
        result = derivedstores.recover_all(
            host, RENDERED, present_sources={OPENSEARCH_SNAPSHOTS_ROOT},
        )
        assert isinstance(result, tuple)
        self.assertTrue(result[1].skipped)
        self.assertEqual(len(host.calls), 3)

    def test_empty_and_absent_roots_skip_without_store_requests(self) -> None:
        absent = FakeHost([])
        result = derivedstores.recover_all(
            absent, RENDERED, present_sources=(), qdrant_root=Q_ROOT,
        )
        self.assertEqual(result, (
            derivedstores.StoreRecovery("qdrant", (), 0.0, True),
            derivedstores.StoreRecovery("opensearch", (), 0.0, True),
        ))
        self.assertEqual(absent.calls, [])
        assert isinstance(result, tuple)
        self.assertEqual(tuple(derivedstores.recovery_clause(item) for item in result), (
            "qdrant: no snapshot in the set", "opensearch: no snapshot in the set",
        ))
        self.assertEqual(derivedstores.recovery_audit(result), {
            "qdrant": {"skipped": "no snapshot in the set"},
            "opensearch": {"skipped": "no snapshot in the set"},
        })

        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        host = FakeHost([
            Step("find", "", "", "tmp/temporary.snapshot\0"),
            os_step("DELETE", repository, {"acknowledged": True}),
            os_step("PUT", repository, {"acknowledged": True}),
            os_step("GET", f"{repository}/_all", {"snapshots": []}),
        ])
        result = derivedstores.recover_all(
            host, RENDERED, present_sources={Q_ROOT, OPENSEARCH_SNAPSHOTS_ROOT},
            qdrant_root=Q_ROOT,
        )
        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertTrue(result[0].skipped)
        self.assertTrue(result[1].skipped)
        self.assertEqual(host.steps, [])
        self.assertFalse(any(QDRANT_SERVICE_NAME in argv for argv, _, _ in host.calls))

    def test_malformed_sidecar_and_one_absent_store(self) -> None:
        for content in ("A" * 64, "a" * 64 + "\n"):
            with self.subTest(content=content):
                host = FakeHost([
                    Step("find", "", "", "a/one.snapshot\0a/two.snapshot\0"),
                    q_step("PUT", "/collections/a/snapshots/recover?wait=true", {"result": True}),
                ], files={f"{Q_ROOT}/a/two.snapshot.checksum": content})
                result = derivedstores.recover_all(
                    host, RENDERED, present_sources={Q_ROOT}, qdrant_root=Q_ROOT,
                )
                self.assertIsInstance(result, tuple)
                assert isinstance(result, tuple)
                self.assertEqual(result[0].names, ("a",))
                self.assertTrue(result[1].skipped)
                request = host.calls[1][1]
                assert request is not None
                self.assertNotIn('"checksum"', request)
                self.assertEqual(host.steps, [])

    def test_empty_directory_and_snapshot_without_indices_skip(self) -> None:
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        host = FakeHost([
            Step("find", "", "", ""),
            os_step("DELETE", repository, {"error": "missing"}, 404),
            os_step("PUT", repository, {"acknowledged": True}),
            os_step("GET", f"{repository}/_all", {"snapshots": [
                {"snapshot": "empty", "start_time_in_millis": 10, "state": "SUCCESS", "shards": {"failed": 0}, "indices": []},
            ]}),
        ])

        result = derivedstores.recover_all(
            host, RENDERED,
            present_sources={Q_ROOT, OPENSEARCH_SNAPSHOTS_ROOT}, qdrant_root=Q_ROOT,
        )

        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertTrue(all(item.skipped for item in result))
        self.assertEqual(host.steps, [])
        self.assertEqual(len(host.calls), 4)

    def test_unsafe_qdrant_name_refuses_before_a_request(self) -> None:
        host = FakeHost([Step("find", "", "", "../escape.snapshot\0")])
        result = derivedstores.recover_all(
            host, RENDERED, present_sources={Q_ROOT}, qdrant_root=Q_ROOT,
        )
        self.assertIsInstance(result, report.Problem)
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.steps, [])

    def test_qdrant_false_result_stops_before_opensearch(self) -> None:
        host = FakeHost([
            Step("find", "", "", "a/one.snapshot\0"),
            q_step("PUT", "/collections/a/snapshots/recover?wait=true", {"result": False}),
        ])
        result = derivedstores.recover_all(
            host, RENDERED,
            present_sources={Q_ROOT, OPENSEARCH_SNAPSHOTS_ROOT}, qdrant_root=Q_ROOT,
        )
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("did not recover collection a", result.problem)
        self.assertEqual(result.fix, stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME))
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.steps, [])

    def test_restore_shard_failure_and_verification_refuse(self) -> None:
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        listing = {"snapshots": [
            {"snapshot": "one", "start_time_in_millis": 100, "state": "SUCCESS", "shards": {"failed": 0}, "indices": ["a"]},
        ]}
        host = FakeHost([
            os_step("DELETE", repository, {"error": "missing"}, 404),
            os_step("PUT", repository, {"acknowledged": True}),
            os_step("GET", f"{repository}/_all", listing),
            os_step("DELETE", "/a", {"error": "missing"}, 404),
            os_step("POST", f"{repository}/one/_restore?wait_for_completion=true", {"snapshot": {"shards": {"failed": 1}}}),
        ])
        result = derivedstores.recover_all(
            host, RENDERED, present_sources={OPENSEARCH_SNAPSHOTS_ROOT},
        )
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("failed shards 1", result.problem)
        self.assertEqual(result.fix, stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME))
        self.assertEqual(host.steps, [])

        verification = FakeHost([
            os_step("DELETE", repository, {"acknowledged": True}),
            os_step("PUT", repository, {
                "error": {"type": "outer", "caused_by": {
                    "type": "repository_verification_exception", "reason": "permission denied",
                }},
            }, 500),
        ])
        refused = derivedstores.recover_all(
            verification, RENDERED, present_sources={OPENSEARCH_SNAPSHOTS_ROOT},
        )
        self.assertIsInstance(refused, report.Problem)
        assert isinstance(refused, report.Problem)
        self.assertIn("verification", refused.problem)
        self.assertEqual(refused.fix, derivedstores._provision_fix())
        self.assertEqual(verification.steps, [])

    def test_malformed_snapshot_names_and_times_refuse(self) -> None:
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        for candidate in (
            {"snapshot": "one", "start_time_in_millis": "bad", "state": "SUCCESS", "shards": {"failed": 0}, "indices": ["a"]},
            {"snapshot": "one", "start_time_in_millis": 1, "state": "SUCCESS", "shards": {"failed": 0}, "indices": [".security"]},
        ):
            with self.subTest(candidate=candidate):
                host = FakeHost([
                    os_step("DELETE", repository, {"acknowledged": True}),
                    os_step("PUT", repository, {"acknowledged": True}),
                    os_step("GET", f"{repository}/_all", {"snapshots": [candidate]}),
                ])
                result = derivedstores.recover_all(
                    host, RENDERED, present_sources={OPENSEARCH_SNAPSHOTS_ROOT},
                )
                self.assertIsInstance(result, report.Problem)
                self.assertEqual(host.steps, [])
        self.assertNotIn(Q_KEY, repr(result))
