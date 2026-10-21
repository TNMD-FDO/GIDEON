"""Retired-store retention, refusals, and convergent sweeps."""

import ast
import contextlib
import copy
import io
import json
import os
import re
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from derivedstores_fake import empty_store_secrets
from test_derivedstores import os_reply, q_reply
from test_index_record import RENDERED, SPACE, row_for

from gideon.cli import build_parser
from gideon.host import backuplock, report, secrets, stack, worker
from gideon.host.index import gc, generations, lexical, record, sample
from gideon.host.render.ci import CI_ROOT, CI_SECRETS_DIR, CI_STACK, PRODUCTION_STACK
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REST_PORT,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.report import StageResult
from gideon.host.sysio import Command, PathLike, RealHost

NOW = datetime(2099, 1, 20, 12, tzinfo=UTC)
STAGES = ("preconditions", "candidates", "aliases", "lexical", "dense", "summary")


def generation_row(
    day: int, state: str, *, sibling: bool = False, retired_at: datetime | None = None,
) -> record.GenerationRow:
    """Make a complete lifecycle row over the record test's fictitious inputs."""

    row = row_for()
    label = f"corpus-2099-01-{day:02}"
    name = generations.generation_name(row.collection, label, SPACE)
    if sibling:
        name = sample.sample_name(name)
    return replace(
        row, gen_id=name, corpus_lockfile=label, lockfiles=(label,), state=state,
        dense_built_at=row.lexical_built_at, built_at=row.lexical_built_at,
        promoted_at=(NOW - timedelta(days=10)).isoformat() if state in {"live", "retired"} else None,
        retired_at=retired_at.isoformat() if retired_at is not None else None,
        failure_reason="store" if state == "failed" else None,
    )


class FakeHost(RealHost):
    """Keep the record, queue, independent stores, secrets, and locks in memory."""

    def __init__(self, *, sibling: bool = False) -> None:
        super().__init__()
        self.sibling = sibling
        self.directory = CI_ROOT if sibling else RENDERED
        expired = generation_row(1, "retired", sibling=sibling, retired_at=NOW - timedelta(days=8))
        held = generation_row(2, "retired", sibling=sibling, retired_at=NOW - timedelta(days=6))
        live = generation_row(3, "live", sibling=sibling)
        built = generation_row(4, "built", sibling=sibling)
        self.expired, self.held, self.live, self.built = (row.gen_id for row in (expired, held, live, built))
        self.rows = {row.gen_id: row for row in (live, built, held, expired)}
        self.objects = {store: set(self.rows) for store in ("lexical", "dense")}
        self.targets = dict.fromkeys(("lexical", "dense"), (live.gen_id,))
        self.jobs: dict[str, tuple[int, str]] = {}
        self.sibling_job: tuple[int, str] | None = None
        self.files = dict(empty_store_secrets())
        self.files[str(Path(self.directory) / "compose.yaml")] = "fictitious compose"
        for name in (QDRANT_SECRET_NAME, OPENSEARCH_PASSWORD_SECRET_NAME):
            self.files[str(Path(CI_SECRETS_DIR) / name)] = f"fictitious-ci-{name}\n"
        self.euid = 0
        self.reads: list[str] = []
        self.locks: dict[str, str] = {}
        self.lock_paths: list[str] = []
        self.lock_errors: set[str] = set()
        self.releases: list[str] = []
        self.calls: list[tuple[list[str], str]] = []
        self.lock_snapshots: list[set[str]] = []
        self.events: list[str] = []
        self.requests: list[tuple[str, str, str, object]] = []
        self.faults: dict[str, int | BaseException] = {}
        self.replies: dict[str, tuple[int, object]] = {}
        self.record_reply: str | None = None

    def geteuid(self) -> int:
        return self.euid

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise AssertionError("unexpected file write")

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        assert os.fspath(path) == backuplock.LOCK_DIR

    def take_lock(self, path: PathLike, text: str) -> str | None:
        key = os.fspath(path)
        self.lock_paths.append(key)
        if key in self.lock_errors:
            raise OSError("fictitious lock inaccessible")
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = text
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.releases.append(key)
        del self.locks[key]

    def event(self, event: str) -> int:
        self.events.append(event)
        fault = self.faults.get(event, 0)
        if isinstance(fault, BaseException):
            raise fault
        return fault

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert env is None and input is not None
        command = list(argv)
        self.calls.append((command, input))
        self.lock_snapshots.append(set(self.locks))
        if command == worker.psql_argv(self.directory):
            return self.psql(command, input)
        if command == stack.exec_argv(self.directory, OPENSEARCH_SERVICE_NAME, "curl", "-s", "-S", "-K", "-"):
            config = dict(line.split(" = ", 1) for line in input.splitlines())
            method = json.loads(config["request"])
            path = json.loads(config["url"]).split(f":{OPENSEARCH_REST_PORT}", 1)[1]
            body = json.loads(json.loads(config["data"])) if "data" in config else None
            return self.store(command, "lexical", method, path, body)
        prefix = stack.exec_argv(self.directory, QDRANT_SERVICE_NAME)
        if command[:len(prefix)] == prefix:
            assert command[len(prefix):len(prefix) + 2] == ["bash", "-c"]
            head, _, payload = input.partition("\r\n\r\n")
            method, path, _ = head.split("\r\n", 1)[0].split()
            return self.store(command, "dense", method, path, json.loads(payload) if payload else None)
        raise AssertionError(f"unexpected child: {command}")

    def psql(self, argv: list[str], sql: str) -> subprocess.CompletedProcess[str]:
        if "WHERE queue_name = :'v_queue'" in sql:
            event, job = "sibling-job", self.sibling_job
            stdout = str(job[0]) if job is not None and job[1] in {"todo", "doing"} else ""
        elif "FROM procrastinate_jobs" in sql:
            prefix = "\\set v_lock '"
            lock = next(line[len(prefix):-1] for line in sql.splitlines() if line.startswith(prefix))
            event, job = f"job:{lock}", self.jobs.get(lock)
            stdout = str(job[0]) if job is not None and job[1] in {"todo", "doing"} else ""
        else:
            assert sql == (
                "SELECT row_to_json(generation) FROM public.index_generations AS generation "
                "ORDER BY collection, created_at, gen_id;\n"
            )
            event = "record"
            rows = sorted(self.rows.values(), key=lambda row: (row.collection, row.created_at, row.gen_id))
            stdout = self.record_reply if self.record_reply is not None else "\n".join(json.dumps(asdict(row)) for row in rows)
        code = self.event(event)
        return subprocess.CompletedProcess(argv, code, stdout, "fictitious database diagnostic" if code else "")

    def store(
        self, argv: list[str], store: str, method: str, path: str, body: object,
    ) -> subprocess.CompletedProcess[str]:
        self.requests.append((store, method, path, body))
        event = f"{store}-read" if method == "GET" else f"{store}-delete:{path}"
        code = self.event(event)
        if code:
            return subprocess.CompletedProcess(argv, code, "", "fictitious store diagnostic")
        data: object
        status = 200
        if event in self.replies:
            status, data = self.replies[event]
        elif method == "GET":
            if store == "lexical":
                assert path == "/_alias/caselaw"
                data = {name: {"aliases": {"caselaw": {}}} for name in self.targets[store]}
                status = 200 if data else 404
            else:
                assert path == "/aliases"
                data = {"result": {"aliases": [
                    {"alias_name": "caselaw", "collection_name": name} for name in self.targets[store]
                ]}}
        else:
            assert method == "DELETE" and body is None
            if store == "lexical":
                name = path.removeprefix("/")
            else:
                assert path.startswith("/collections/")
                name = path.removeprefix("/collections/")
            if name in self.objects[store]:
                self.objects[store].remove(name)
                data = {"acknowledged": True} if store == "lexical" else {"result": True}
            elif store == "lexical":
                status, data = 404, {"error": "fictitious missing index"}
            else:
                data = {"result": False, "status": "ok"}
        reply = os_reply(data, status) if store == "lexical" else q_reply(data, status)
        return subprocess.CompletedProcess(argv, 0, reply, "")


class Sweep(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_secrets = secrets.current_directory()
        secrets.select_directory(Path("/fictitious-gc-secrets"))
        self.host = FakeHost()
        self.now = NOW

    def tearDown(self) -> None:
        secrets.select_directory(self.previous_secrets)

    def retry(self) -> str:
        return report.command("index gc --stack ci" if self.host.sibling else "index gc")

    def run_gc(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        before_locks, before_rows = dict(self.host.locks), copy.deepcopy(self.host.rows)
        attempts, releases = len(self.host.lock_paths), len(self.host.releases)
        flags = ["--stack", CI_STACK] if self.host.sibling else []
        args = build_parser().parse_args(["index", "gc", *flags])
        self.assertEqual(args.stack, CI_STACK if self.host.sibling else PRODUCTION_STACK)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gc.run_index_gc(args, host=self.host, rendered_dir=RENDERED, clock=lambda: self.now)
        self.assertEqual(self.host.rows, before_rows, "gc must never change the record")
        self.assertEqual(self.host.locks, before_locks)
        taken = [path for path in self.host.lock_paths[attempts:]
                 if path not in before_locks and path not in self.host.lock_errors]
        self.assertEqual(self.host.releases[releases:], list(reversed(taken)))
        for line in (out.getvalue() + err.getvalue()).splitlines():
            if " Fix: " in line:
                self.assertIn(self.retry(), line.split(" Fix: ", 1)[1])
        for argv, sql in self.host.calls:
            if argv != worker.psql_argv(self.host.directory):
                continue
            statements = "\n".join(line for line in sql.splitlines() if not line.startswith("\\set "))
            self.assertEqual(re.findall(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|DO|BEGIN|COMMIT)\b", statements, re.I), ["SELECT"])
            self.assertNotIn(statements, " ".join(argv))
        return code, out.getvalue(), err.getvalue()

    def refuse(self, stage: str, phrase: str) -> str:
        code, out, err = self.run_gc()
        self.assertEqual((code, err), (1, ""), out)
        line = next(line for line in out.splitlines() if line.startswith(f"{stage}: refuse"))
        self.assertIn(phrase, line.split(" Fix: ", 1)[0])
        self.assertIn(self.retry(), line.split(" Fix: ", 1)[1])
        return out

    def assert_no_deletes(self) -> None:
        self.assertFalse(any(method == "DELETE" for _, method, _, _ in self.host.requests))

    def test_clock_drops_only_expired_retired_rows_and_reports_the_partition(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                for day, state in ((5, "building"), (6, "failed")):
                    row = generation_row(day, state, sibling=sibling)
                    self.host.rows[row.gen_id] = row
                    for objects in self.host.objects.values():
                        objects.add(row.gen_id)
                before = copy.deepcopy(self.host.objects)
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()], list(STAGES))
                for store in before:
                    self.assertEqual(self.host.objects[store], before[store] - {self.host.expired})
                self.assertIn(f"drop 1: {self.host.expired}", out)
                self.assertIn(f"held 1: {self.host.held} (1 days left)", out)
                self.assertIn(f"live: {self.host.live} (never dropped)", out)
                self.assertIn("building 1; built 1; failed 1", out)
                self.assertIn("summary: ok — lexical dropped 1, absent 0; dense dropped 1, absent 0; held 1; live 1", out)
                self.assertNotIn("Next:", out)

    def test_seven_day_boundary_offsets_and_fractional_days_left(self) -> None:
        for age, dropped, days in (
            (timedelta(days=7), True, None),
            (timedelta(days=6, hours=12), False, 1),
            (timedelta(days=6, hours=23, minutes=59, seconds=59), False, 1),
            (timedelta(days=5, hours=12), False, 2),
        ):
            with self.subTest(age=age):
                self.host = FakeHost()
                row = self.host.rows[self.host.expired]
                retired_at = (NOW - age).astimezone(timezone(timedelta(hours=3)))
                self.host.rows = {row.gen_id: replace(row, retired_at=retired_at.isoformat())}
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                for objects in self.host.objects.values():
                    self.assertEqual(row.gen_id not in objects, dropped)
                if not dropped:
                    self.assertIn(f"{row.gen_id} ({days} days left)", out)
                    self.assertEqual(self.host.requests, [])
                else:
                    self.assertIn(f"drop 1: {row.gen_id}", out)

    def test_advancing_the_injected_clock_expires_a_previously_held_row(self) -> None:
        self.host.rows = {self.host.held: self.host.rows[self.host.held]}
        self.assertEqual(self.run_gc()[0], 0)
        self.assertEqual(self.host.requests, [])
        self.now += timedelta(days=1)
        self.assertEqual(self.run_gc()[0], 0)
        for objects in self.host.objects.values():
            self.assertNotIn(self.host.held, objects)

    def test_deletes_are_bodyless_in_name_order_and_credentials_only_travel_on_stdin(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                extra = generation_row(7, "retired", sibling=sibling, retired_at=NOW - timedelta(days=8))
                self.host.rows[extra.gen_id] = extra
                for objects in self.host.objects.values():
                    objects.add(extra.gen_id)
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                names = sorted((self.host.expired, extra.gen_id))
                deletes = [request for request in self.host.requests if request[1] == "DELETE"]
                self.assertEqual(deletes, [
                    *(("lexical", "DELETE", f"/{name}", None) for name in names),
                    *(("dense", "DELETE", f"/collections/{name}", None) for name in names),
                ])
                for service, secret_name in (
                    (OPENSEARCH_SERVICE_NAME, OPENSEARCH_PASSWORD_SECRET_NAME),
                    (QDRANT_SERVICE_NAME, QDRANT_SECRET_NAME),
                ):
                    credential = self.host.files[str(secrets.secret_path(secret_name))].strip()
                    requests = [(argv, request) for argv, request in self.host.calls if service in argv]
                    self.assertTrue(requests)
                    for argv, request in requests:
                        self.assertIn(credential, request)
                        self.assertNotIn(credential, " ".join(argv))
                        self.assertFalse(any(name in " ".join(argv) for name in names))
                        if service == QDRANT_SERVICE_NAME and request.startswith("DELETE "):
                            self.assertEqual(request.partition("\r\n\r\n")[2], "")
                            self.assertIn("Content-Length: 0\r\n", request)
                        elif service == OPENSEARCH_SERVICE_NAME:
                            self.assertNotIn("data =", request)
                    self.assertNotIn(credential, out + err)

    def test_missing_objects_and_repeated_sweeps_report_absent(self) -> None:
        for missing in (("lexical",), ("dense",), ("lexical", "dense")):
            with self.subTest(missing=missing):
                self.host = FakeHost()
                for store in missing:
                    self.host.objects[store].remove(self.host.expired)
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                for store in missing:
                    self.assertIn(f"{store}: ok — dropped: none; absent: {self.host.expired}", out)
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                for store in self.host.objects:
                    self.assertIn(f"{store}: ok — dropped: none; absent: {self.host.expired}", out)

    def test_off_shape_success_replies_refuse_with_store_logs(self) -> None:
        for sibling in (False, True):
            for store, field, service in (
                ("lexical", "acknowledged", OPENSEARCH_SERVICE_NAME),
                ("dense", "result", QDRANT_SERVICE_NAME),
            ):
                replies: tuple[object, ...] = (None, [], {}, {field: 1}, {field: "true"})
                if store == "lexical":
                    replies += ({field: False},)
                for data in replies:
                    with self.subTest(sibling=sibling, store=store, data=data):
                        self.host = FakeHost(sibling=sibling)
                        path = f"/{self.host.expired}" if store == "lexical" else f"/collections/{self.host.expired}"
                        self.host.replies[f"{store}-delete:{path}"] = (200, data)
                        out = self.refuse(store, "invalid delete reply")
                        self.assertIn(stack.logs_fix(self.host.directory, service), out)
                        self.assertIn(self.host.expired, self.host.objects[store])
                        if store == "lexical":
                            self.assertFalse(any(request[0] == "dense" and request[1] == "DELETE" for request in self.host.requests))

    def test_alias_targets_guard_both_stores_before_any_delete(self) -> None:
        for sibling in (False, True):
            for store in ("lexical", "dense"):
                for has_live in (False, True):
                    with self.subTest(sibling=sibling, store=store, has_live=has_live):
                        self.host = FakeHost(sibling=sibling)
                        if not has_live:
                            del self.host.rows[self.host.live]
                        self.host.targets[store] = (self.host.expired,)
                        before = copy.deepcopy((self.host.targets, self.host.objects))
                        out = self.refuse("aliases", "retired generation still served")
                        fix = f"index promote {self.host.live}" if has_live else "index report"
                        self.assertIn(report.command(fix), out)
                        self.assert_no_deletes()
                        self.assertEqual((self.host.targets, self.host.objects), before)

    def test_mid_store_failure_preserves_earlier_drops_and_rerun_converges(self) -> None:
        for sibling in (False, True):
            for store in ("lexical", "dense"):
                with self.subTest(sibling=sibling, store=store):
                    self.host = FakeHost(sibling=sibling)
                    extra = generation_row(7, "retired", sibling=sibling, retired_at=NOW - timedelta(days=8))
                    self.host.rows[extra.gen_id] = extra
                    for objects in self.host.objects.values():
                        objects.add(extra.gen_id)
                    path = f"/{extra.gen_id}" if store == "lexical" else f"/collections/{extra.gen_id}"
                    self.host.faults[f"{store}-delete:{path}"] = 7
                    out = self.refuse(store, "exit 7")
                    service = OPENSEARCH_SERVICE_NAME if store == "lexical" else QDRANT_SERVICE_NAME
                    self.assertIn(stack.logs_fix(self.host.directory, service), out)
                    self.assertNotIn(self.host.expired, self.host.objects[store])
                    self.assertIn(extra.gen_id, self.host.objects[store])
                    if store == "lexical":
                        self.assertIn(self.host.expired, self.host.objects["dense"])
                    else:
                        self.assertNotIn(extra.gen_id, self.host.objects["lexical"])
                    self.host.faults.clear()
                    code, out, err = self.run_gc()
                    self.assertEqual((code, err), (0, ""), out)
                    self.assertIn(f"{store}: ok — dropped: {extra.gen_id}; absent: {self.host.expired}", out)
                    for objects in self.host.objects.values():
                        self.assertTrue(objects.isdisjoint((self.host.expired, extra.gen_id)))

    def test_stack_routes_record_stores_secrets_locks_and_sibling_job_read(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                code, out, err = self.run_gc()
                self.assertEqual((code, err), (0, ""), out)
                expected = [backuplock.ENGINE_LOCK.path] if sibling else [backuplock.CORPUS_LOCK.path, backuplock.BACKUP_LOCK.path]
                self.assertEqual(self.host.lock_paths, expected)
                self.assertEqual(self.host.releases, list(reversed(expected)))
                self.assertTrue(all(snapshot == set(expected) for snapshot in self.host.lock_snapshots))
                self.assertTrue(all(str(self.host.directory) in " ".join(argv) for argv, _ in self.host.calls))
                directory = Path(CI_SECRETS_DIR) if sibling else Path("/fictitious-gc-secrets")
                self.assertTrue(self.host.reads)
                self.assertTrue(all(Path(path).parent == directory for path in self.host.reads))
                if sibling:
                    self.assertEqual(self.host.events[0], "sibling-job")
                    self.assertLess(self.host.events.index("sibling-job"), self.host.events.index("record"))
                    self.assertIn("ci; engine lock taken", out)
                else:
                    self.assertNotIn("sibling-job", self.host.events)
                    self.assertIn("production; corpus lock taken; backup lock taken", out)

    def test_root_refusal_is_stderr_exit_two_without_io_on_either_stack(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                self.host.euid = 1000
                code, out, err = self.run_gc()
                self.assertEqual((code, out), (2, ""))
                self.assertIn("root privileges are required", err)
                self.assertEqual((self.host.calls, self.host.reads, self.host.lock_paths), ([], [], []))

    def test_foreign_unreadable_nested_and_inaccessible_locks_preserve_the_holder(self) -> None:
        for lock in (backuplock.CORPUS_LOCK, backuplock.BACKUP_LOCK, backuplock.ENGINE_LOCK):
            for holder in ("foreign", "unreadable", "nested", "inaccessible"):
                with self.subTest(lock=lock.noun, holder=holder):
                    self.host = FakeHost(sibling=lock == backuplock.ENGINE_LOCK)
                    if holder == "inaccessible":
                        self.host.lock_errors.add(lock.path)
                    else:
                        self.host.locks[lock.path] = "unreadable" if holder == "unreadable" else backuplock.Record(
                            "fictitious lock holder", os.getpid() if holder == "nested" else os.getpid() + 1000, NOW,
                        ).to_json()
                    if holder == "nested" and lock != backuplock.CORPUS_LOCK:
                        code, out, err = self.run_gc()
                        self.assertEqual((code, err), (0, ""), out)
                        self.assertIn(f"{lock.noun} lock held by this process", out)
                        self.assertNotIn(lock.path, self.host.releases)
                    else:
                        phrase = "could not be taken" if holder == "inaccessible" else "already running in this process" if holder == "nested" else "record unreadable" if holder == "unreadable" else "fictitious lock holder"
                        out = self.refuse("preconditions", phrase)
                        if holder == "unreadable":
                            self.assertIn(lock.wait_fix, out)
                        self.assertEqual(self.host.calls, [])
                        self.assert_no_deletes()

    def test_each_candidate_job_lock_refuses_queued_and_running_work_before_store_reads(self) -> None:
        for sibling in (False, True):
            for prefix, subject in lexical.JOB_SUBJECTS.items():
                for status in ("todo", "doing"):
                    with self.subTest(sibling=sibling, prefix=prefix, status=status):
                        self.host = FakeHost(sibling=sibling)
                        self.host.jobs[prefix + self.host.expired] = (73, status)
                        self.refuse("candidates", f"{subject} job 73 is queued or running")
                        self.assertEqual(self.host.requests, [])

    def test_sibling_build_refuses_before_record_or_store_reads(self) -> None:
        for status in ("todo", "doing"):
            with self.subTest(status=status):
                self.host = FakeHost(sibling=True)
                self.host.sibling_job = (81, status)
                self.refuse("preconditions", "sample build is running on the sibling (job 81)")
                self.assertEqual(self.host.events, ["sibling-job"])
                self.assertEqual(self.host.requests, [])

    def test_invalid_retirement_timestamps_and_unsafe_names_refuse_before_store_requests(self) -> None:
        for sibling in (False, True):
            for value in ("not-a-timestamp", "2099-01-01T12:00:00", ""):
                with self.subTest(sibling=sibling, retired_at=value):
                    self.host = FakeHost(sibling=sibling)
                    self.host.rows[self.host.expired] = replace(self.host.rows[self.host.expired], retired_at=value)
                    out = self.refuse("candidates", "retired_at is invalid")
                    self.assertIn(stack.logs_fix(self.host.directory, "postgres"), out)
                    self.assertEqual(self.host.requests, [])
            for name in ("../../fictitious", "caselaw__invalid", generations.generation_name("caselaw", "corpus-2099-01-01", SPACE) + "x" * 255):
                with self.subTest(sibling=sibling, name=name):
                    self.host = FakeHost(sibling=sibling)
                    self.host.rows[self.host.expired] = replace(self.host.rows[self.host.expired], gen_id=name)
                    self.refuse("candidates", "generation record name is invalid")
                    self.assertEqual(self.host.requests, [])

    def test_empty_or_unexpired_records_print_nothing_to_drop_without_store_requests(self) -> None:
        for sibling in (False, True):
            for empty in (False, True):
                with self.subTest(sibling=sibling, empty=empty):
                    self.host = FakeHost(sibling=sibling)
                    self.host.rows = {} if empty else {name: row for name, row in self.host.rows.items() if name != self.host.expired}
                    before = copy.deepcopy(self.host.objects)
                    code, out, err = self.run_gc()
                    self.assertEqual((code, err), (0, ""), out)
                    self.assertIn("drop 0: none (nothing to drop)", out)
                    for stage in ("aliases", "lexical", "dense"):
                        self.assertIn(f"{stage}: ok — nothing to drop", out)
                    self.assertEqual(self.host.objects, before)
                    self.assertEqual((self.host.requests, self.host.reads), ([], []))

    def test_failed_or_unavailable_reads_and_deletes_refuse_without_silent_retry(self) -> None:
        for sibling in (False, True):
            for event in ("record", "job", "lexical-read", "dense-read", "lexical-delete", "dense-delete", "sibling-job"):
                if event == "sibling-job" and not sibling:
                    continue
                for fault in (3, 127, OSError("fictitious tool unavailable")):
                    with self.subTest(sibling=sibling, event=event, fault=fault):
                        self.host = FakeHost(sibling=sibling)
                        key = f"job:index-{self.host.expired}" if event == "job" else event
                        if event.endswith("-delete"):
                            path = f"/{self.host.expired}" if event == "lexical-delete" else f"/collections/{self.host.expired}"
                            key += f":{path}"
                        self.host.faults[key] = fault
                        stage = "preconditions" if event == "sibling-job" else "candidates" if event in {"record", "job"} else "aliases" if event.endswith("-read") else event.removesuffix("-delete")
                        self.refuse(stage, f"exit {fault}" if isinstance(fault, int) else "could not run")
                        self.assertEqual(self.host.events.count(key), 1)
                        if stage in {"preconditions", "candidates", "aliases"}:
                            self.assert_no_deletes()

    def test_invalid_record_listing_refuses_before_store_requests(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                self.host.record_reply = "{fictitious invalid JSON"
                self.refuse("candidates", "generation record row is invalid")
                self.assertEqual(self.host.requests, [])

    def test_interrupt_at_each_stage_releases_locks_and_reports_whether_drops_started(self) -> None:
        print_stage = report.print_stage
        for sibling in (False, True):
            for stage in STAGES:
                with self.subTest(sibling=sibling, stage=stage):
                    self.host = FakeHost(sibling=sibling)

                    def interrupt(row: StageResult, stage: str = stage) -> None:
                        if row.name == stage and row.ok:
                            raise KeyboardInterrupt
                        print_stage(row)

                    with patch.object(report, "print_stage", side_effect=interrupt):
                        out = self.refuse(stage, "interrupted")
                    if stage in STAGES[:3]:
                        self.assertIn("nothing was dropped", out)
                        self.assert_no_deletes()
                    else:
                        self.assertIn("rerun to finish the store deletes", out)
                        self.assertNotIn(self.host.expired, self.host.objects["lexical"])
                    code, out, err = self.run_gc()
                    self.assertEqual((code, err), (0, ""), out)
                    for objects in self.host.objects.values():
                        self.assertNotIn(self.host.expired, objects)

    def test_interrupts_during_record_job_alias_and_delete_io_release_locks(self) -> None:
        for sibling in (False, True):
            for event, stage in (
                ("record", "candidates"), ("job", "candidates"),
                ("lexical-read", "aliases"), ("dense-read", "aliases"),
                ("lexical-delete", "lexical"), ("dense-delete", "dense"),
                ("sibling-job", "preconditions"),
            ):
                if event == "sibling-job" and not sibling:
                    continue
                with self.subTest(sibling=sibling, event=event):
                    self.host = FakeHost(sibling=sibling)
                    key = f"job:index-{self.host.expired}" if event == "job" else event
                    if event.endswith("-delete"):
                        path = f"/{self.host.expired}" if event == "lexical-delete" else f"/collections/{self.host.expired}"
                        key += f":{path}"
                    self.host.faults[key] = KeyboardInterrupt()
                    self.refuse(stage, "interrupted")
                    if stage in {"preconditions", "candidates", "aliases"}:
                        self.assert_no_deletes()
                    elif stage == "dense":
                        self.assertNotIn(self.host.expired, self.host.objects["lexical"])
                        self.assertIn(self.host.expired, self.host.objects["dense"])

    def test_module_imports_no_evaluation_or_worker_implementation_at_any_depth(self) -> None:
        tree = ast.parse(Path(gc.__file__).read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                imports.extend((base, *(f"{base}.{alias.name}" for alias in node.names)))
        forbidden = ("gideon.evaluation", "gideon.worker", "gideon.host.worker")
        self.assertFalse(any(name == base or name.startswith(base + ".") for name in imports for base in forbidden), imports)
