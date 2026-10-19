"""Promotion refusals, commit ordering, and recovery over a stateful fake host."""

import argparse
import ast
import contextlib
import copy
import io
import json
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

from derivedstores_fake import empty_store_secrets
from test_corpus_install import NOW
from test_derivedstores import os_reply, q_reply
from test_index_record import LABEL, NAME, RENDERED, SPACE, row_for
from test_index_report import report_for
from test_index_treatment import treatment_for

from gideon.host import backuplock, report, secrets, stack, worker
from gideon.host.index import buildreport, generations, promote, record, sample
from gideon.host.render.ci import CI_ROOT, CI_SECRETS_DIR, CI_STACK, PRODUCTION_STACK
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.render.worker import (
    CLUSTER_BUDGET_SECONDS,
    DENSE_LOCK_PREFIX,
    DUPES_LOCK_PREFIX,
)
from gideon.host.report import StageResult
from gideon.host.sysio import Command, PathLike, RealHost

CHECKOUT = Path("/fictitious-promote-checkout")
OLD = generations.generation_name("caselaw", "corpus-2099-01-01", SPACE)
STAGES = ("preconditions", "candidate", "aliases", "gates", "flip", "lexical", "dense", "next")


def built_row(name: str) -> record.GenerationRow:
    row = row_for()
    return replace(
        row, gen_id=name, state="built", dense_built_at=row.lexical_built_at,
        built_at=row.lexical_built_at, clustered_at=row.lexical_built_at,
        counts={**row.counts, "lexical_bytes": row.bytes, "dense_bytes": 2048},
        bytes=row.bytes + 2048,
    )


class FakeHost(RealHost):
    """Record, queue, store, credential, and lock state without external I/O."""

    def __init__(self, *, sibling: bool = False) -> None:
        super().__init__()
        self.directory = CI_ROOT if sibling else RENDERED
        self.name = sample.sample_name(NAME) if sibling else NAME
        self.old = sample.sample_name(OLD) if sibling else OLD
        candidate = built_row(self.name)
        self.rows = {
            self.name: candidate,
            self.old: replace(built_row(self.old), state="live", promoted_at=NOW.isoformat()),
        }
        self.build_report: buildreport.BuildReport | None = replace(
            report_for(), gen_id=self.name,
            gates={"cluster": buildreport.ClusterGate(3, CLUSTER_BUDGET_SECONDS, False, True)},
            treatment=treatment_for(),
        )
        self.targets: dict[str, tuple[str, ...]] = {"lexical": (self.old,), "dense": (self.old,)}
        self.jobs: dict[str, tuple[int, str]] = {}
        self.sibling_job: tuple[int, str] | None = None
        self.euid = 0
        self.files = dict(empty_store_secrets())
        self.files[str(Path(self.directory) / "compose.yaml")] = "fictitious compose"
        for secret_name in (QDRANT_SECRET_NAME, OPENSEARCH_PASSWORD_SECRET_NAME):
            self.files[str(Path(CI_SECRETS_DIR) / secret_name)] = f"fictitious-ci-{secret_name}\n"
        self.reads: list[str] = []
        self.locks: dict[str, str] = {}
        self.lock_paths: list[str] = []
        self.releases: list[str] = []
        self.lock_error = False
        self.calls: list[tuple[list[str], str | None]] = []
        self.events: list[str] = []
        self.requests: list[tuple[str, str, str, Any]] = []
        self.failures: dict[str, int] = {}
        self.interrupts: set[str] = set()

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

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        assert os.fspath(path) == backuplock.LOCK_DIR
        if self.lock_error:
            raise OSError("fictitious lock directory inaccessible")

    def take_lock(self, path: PathLike, text: str) -> str | None:
        key = os.fspath(path)
        self.lock_paths.append(key)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = text
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.releases.append(key)
        del self.locks[key]

    @staticmethod
    def bound(sql: str, name: str) -> str:
        prefix = f"\\set {name} '"
        return next(line[len(prefix):-1] for line in sql.splitlines() if line.startswith(prefix))

    def event(self, name: str) -> int:
        self.events.append(name)
        if name in self.interrupts:
            raise KeyboardInterrupt
        return self.failures.get(name, 0)

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert env is None and input is not None
        command = list(argv)
        self.calls.append((command, input))
        if command == worker.psql_argv(self.directory):
            return self.psql(command, input)
        if command == stack.exec_argv(self.directory, OPENSEARCH_SERVICE_NAME, "curl", "-s", "-S", "-K", "-"):
            config = dict(line.split(" = ", 1) for line in input.splitlines())
            method = json.loads(config["request"])
            path = json.loads(config["url"]).split("9200", 1)[1]
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
        data: object
        if "WHERE queue_name = :'v_queue'" in sql:
            event = "sibling-job"
            job = self.sibling_job
            data = job[0] if job is not None and job[1] in {"todo", "doing"} else ""
        elif "FROM procrastinate_jobs" in sql:
            lock = self.bound(sql, "v_lock")
            event = f"job:{lock}"
            job = self.jobs.get(lock)
            data = job[0] if job is not None and job[1] in {"todo", "doing"} else ""
        elif "row_to_json(generation)" in sql:
            event = "candidate"
            row = self.rows.get(self.bound(sql, "v_name"))
            data = asdict(row) if row is not None else None
        elif "FROM public.build_reports" in sql:
            event = "report"
            data = self.build_report.to_json() if self.build_report is not None else None
        elif "json_agg(promoted" in sql:
            event = "promoted"
            data = [asdict(record.PromotedRow(row.gen_id, row.collection, row.bytes))
                    for row in self.rows.values() if row.state == "live"]
        elif "DO $promotion$" in sql:
            code = self.event("flip")
            if code:
                return subprocess.CompletedProcess(argv, code, "", "fictitious database diagnostic")
            payload = json.loads(self.bound(sql, "v_payload"))
            candidate = self.rows.get(payload["name"])
            if candidate is None or candidate.state != "built":
                return subprocess.CompletedProcess(argv, 3, "", f"ERROR: {record._NOT_BUILT}")
            retired = tuple(sorted(row.gen_id for row in self.rows.values()
                                   if row.collection == payload["collection"] and row.state == "live"))
            for name in retired:
                self.rows[name] = replace(self.rows[name], state="retired", retired_at=NOW.isoformat())
            self.rows[candidate.gen_id] = replace(candidate, state="live", promoted_at=NOW.isoformat())
            return subprocess.CompletedProcess(argv, 0, json.dumps({"retired": retired, "promoted_at": NOW.isoformat()}), "")
        else:
            raise AssertionError("unexpected database statement")
        code = self.event(event)
        stdout = str(data) if event.startswith("job:") or event == "sibling-job" else "" if data is None else json.dumps(data)
        return subprocess.CompletedProcess(argv, code, stdout, "fictitious database diagnostic" if code else "")

    def store(self, argv: list[str], store: str, method: str, path: str, body: Any) -> subprocess.CompletedProcess[str]:
        data: object
        self.requests.append((store, method, path, body))
        code = self.event(f"{store}-{'read' if method == 'GET' else 'write'}")
        if code:
            return subprocess.CompletedProcess(argv, code, "", "fictitious store diagnostic")
        if method == "GET":
            if store == "lexical":
                assert path == "/_alias/caselaw"
                data = {name: {"aliases": {"caselaw": {}}} for name in self.targets[store]}
                return subprocess.CompletedProcess(argv, 0, os_reply(data, 200 if data else 404), "")
            assert path == "/aliases"
            data = {"result": {"aliases": [
                {"alias_name": "caselaw", "collection_name": name} for name in self.targets[store]
            ]}}
            return subprocess.CompletedProcess(argv, 0, q_reply(data), "")
        assert method == "POST"
        if store == "lexical":
            assert path == "/_aliases"
            targets = list(self.targets[store])
            for action in body["actions"]:
                if "remove" in action:
                    assert action["remove"]["alias"] == "caselaw"
                    targets.remove(action["remove"]["index"])
                else:
                    assert action["add"]["alias"] == "caselaw"
                    targets.append(action["add"]["index"])
            self.targets[store] = tuple(targets)
            return subprocess.CompletedProcess(argv, 0, os_reply({"acknowledged": True}), "")
        assert path == "/collections/aliases"
        for action in body["actions"]:
            if "delete_alias" in action:
                assert action["delete_alias"]["alias_name"] == "caselaw"
                self.targets[store] = ()
            else:
                assert action["create_alias"]["alias_name"] == "caselaw"
                self.targets[store] = (action["create_alias"]["collection_name"],)
        return subprocess.CompletedProcess(argv, 0, q_reply({"result": True}), "")


class Promote(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_secrets = secrets.current_directory()
        secrets.select_directory(Path("/fictitious-promote-secrets"))
        self.host = FakeHost()
        self.gate_code = 0
        self.gate_interrupt = False
        self.gates: list[argparse.Namespace] = []
        self.run_ids: list[str] = []
        self.gate_directories: list[Path] = []
        self.snapshots: list[tuple[dict[str, record.GenerationRow], dict[str, tuple[str, ...]]]] = []
        self.after_gates: Callable[[], None] | None = None

    def tearDown(self) -> None:
        secrets.select_directory(self.previous_secrets)

    def run_gates(
        self, args: argparse.Namespace, *, host: FakeHost, checkout_root: PathLike,
        run_id_factory: Callable[[], str],
    ) -> int:
        self.assertIs(host, self.host)
        self.assertEqual(checkout_root, CHECKOUT)
        self.gates.append(args)
        self.run_ids.append(run_id_factory())
        self.assertEqual(run_id_factory(), self.run_ids[-1])
        self.gate_directories.append(secrets.current_directory())
        self.snapshots.append((copy.deepcopy(host.rows), copy.deepcopy(host.targets)))
        self.host.events.append("gates")
        if self.gate_interrupt:
            raise KeyboardInterrupt
        report.print_stage(StageResult("suite-part", self.gate_code == 0, "fictitious gate verdict", ""))
        if self.after_gates is not None:
            self.after_gates()
        return self.gate_code

    def run_promote(self, name: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        before_locks = dict(self.host.locks)
        attempts = len(self.host.lock_paths)
        releases = len(self.host.releases)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = promote.run_index_promote(
                argparse.Namespace(gen=self.host.name if name is None else name), run_gates=self.run_gates,
                host=self.host, rendered_dir=RENDERED, checkout=CHECKOUT, clock=lambda: NOW,
            )
        # Every return, successful or refused, must leave only pre-existing holders.
        self.assertEqual(self.host.locks, before_locks)
        expected = [path for path in self.host.lock_paths[attempts:] if path not in before_locks]
        self.assertEqual(self.host.releases[releases:], expected)
        return code, out.getvalue(), err.getvalue()

    def refuse(self, stage: str, phrase: str, *, name: str | None = None) -> str:
        code, out, err = self.run_promote(name)
        self.assertEqual((code, err), (1, ""), out)
        line = next(line for line in out.splitlines() if line.startswith(f"{stage}: refuse"))
        problem, fix = line.split(" Fix: ", 1)
        self.assertIn(phrase, problem)
        self.assertIn(report.command(f"index promote {self.host.name if name is None else name}"), fix)
        return out

    def assert_untouched(self, before: tuple[dict[str, record.GenerationRow], dict[str, tuple[str, ...]]]) -> None:
        self.assertEqual((self.host.rows, self.host.targets), before)
        self.assertNotIn("flip", self.host.events)
        self.assertFalse(any(method == "POST" for _, method, _, _ in self.host.requests))
        self.assertEqual(self.gates, [])

    def test_root_and_generation_grammar_refuse_without_any_io(self) -> None:
        self.host.euid = 1000
        code, out, err = self.run_promote()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("root privileges are required", err)
        self.assertIn(report.command(f"index promote {NAME}"), err.split("Fix: ", 1)[1])
        self.host.euid = 0
        long_name = f"caselaw__{LABEL}__" + "x" * 255
        for name in ("invalid", "caselaw__bad__space", NAME + "__unknown", long_name):
            with self.subTest(name=name):
                out = self.refuse("preconditions", "generation name is invalid", name=name)
                self.assertIn(report.command("index report"), out)
        self.assertEqual((self.host.calls, self.host.lock_paths, self.gates), ([], [], []))

    def test_absent_building_failed_and_retired_candidates_have_their_fixes(self) -> None:
        for sibling in (False, True):
            for state in (None, "building", "failed", "retired"):
                with self.subTest(sibling=sibling, state=state):
                    self.host = FakeHost(sibling=sibling)
                    row = self.host.rows[self.host.name]
                    if state is None:
                        del self.host.rows[self.host.name]
                    else:
                        self.host.rows[self.host.name] = replace(
                            row, state=state, failure_reason="store" if state == "failed" else None,
                            promoted_at=NOW.isoformat() if state == "retired" else None,
                            retired_at=NOW.isoformat() if state == "retired" else None,
                        )
                    before = copy.deepcopy((self.host.rows, self.host.targets))
                    out = self.refuse("candidate", "not recorded" if state is None else f"generation is {state}")
                    if state is None:
                        self.assertIn(report.command("index report"), out)
                    elif state == "retired":
                        self.assertIn(report.command("corpus cut"), out)
                        self.assertIn(report.command("corpus install <label>"), out)
                    else:
                        build = "index build caselaw" + (" --sample" if sibling else "") + f" --lockfile {row.corpus_lockfile}"
                        self.assertIn(report.command(build), out)
                    self.assert_untouched(before)

    def test_built_candidate_requires_footprint_newest_passing_report_and_treatment(self) -> None:
        for missing in ("footprint", "report", "failed_stage", "cluster", "cluster-failed", "treatment"):
            with self.subTest(missing=missing):
                self.host = FakeHost()
                row = self.host.rows[NAME]
                completed = self.host.build_report
                assert completed is not None
                if missing == "footprint":
                    self.host.rows[NAME] = replace(row, counts={key: count for key, count in row.counts.items() if key != "dense_bytes"})
                elif missing == "report":
                    self.host.build_report = None
                elif missing == "failed_stage":
                    self.host.build_report = replace(completed, failed_stage="dense")
                elif missing == "cluster":
                    self.host.build_report = replace(completed, gates={})
                elif missing == "cluster-failed":
                    self.host.build_report = replace(completed, gates={"cluster": buildreport.ClusterGate(
                        CLUSTER_BUDGET_SECONDS + 1, CLUSTER_BUDGET_SECONDS, False, False,
                    )})
                else:
                    self.host.build_report = replace(completed, treatment=None)
                before = copy.deepcopy((self.host.rows, self.host.targets))
                out = self.refuse("candidate", {
                    "footprint": "dense footprint is not recorded", "report": "no build report",
                    "failed_stage": "failed at dense", "cluster": "no cluster gate",
                    "cluster-failed": "cluster gate failed", "treatment": "no treatment",
                }[missing])
                self.assertIn(report.command(f"index build {row.collection} --lockfile {row.corpus_lockfile}"), out)
                if missing.startswith("cluster"):
                    self.assertIn(f"budget {CLUSTER_BUDGET_SECONDS} s", out)
                self.assert_untouched(before)

    def test_multiple_live_rows_refuse_on_both_stacks(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                extra = self.host.old.replace("2099-01-01", "2098-01-01")
                self.host.rows[extra] = replace(self.host.rows[self.host.old], gen_id=extra)
                before = copy.deepcopy((self.host.rows, self.host.targets))
                out = self.refuse("candidate", "multiple live generations")
                self.assertIn(stack.logs_fix(self.host.directory, "postgres"), out)
                self.assert_untouched(before)

    def test_interrupted_rebuild_under_each_lock_refuses_before_candidate_on_production(self) -> None:
        for prefix, subject in (("index-", "lexical"), (DENSE_LOCK_PREFIX, "dense"), (DUPES_LOCK_PREFIX, "clustering")):
            for status in ("todo", "doing"):
                with self.subTest(prefix=prefix, status=status):
                    self.host = FakeHost()
                    self.host.jobs[prefix + NAME] = (73, status)
                    before = copy.deepcopy((self.host.rows, self.host.targets))
                    out = self.refuse("preconditions", f"{subject} job 73 is queued or running")
                    self.assertIn(report.command("index build caselaw"), out)
                    self.assertEqual(self.host.rows[NAME].state, "built")
                    self.assertIsNotNone(self.host.build_report)
                    self.assertNotIn("candidate", self.host.events)
                    self.assertNotIn("candidate:", out)
                    self.assert_untouched(before)

    def test_sibling_live_job_refuses_before_candidate(self) -> None:
        for status in ("todo", "doing"):
            with self.subTest(status=status):
                self.host = FakeHost(sibling=True)
                self.host.sibling_job = (81, status)
                before = copy.deepcopy((self.host.rows, self.host.targets))
                out = self.refuse("preconditions", "sample build is running on the sibling (job 81)")
                self.assertIn(report.command("index build caselaw --sample"), out)
                self.assertEqual(self.host.events, ["sibling-job"])
                self.assert_untouched(before)

    def test_lock_holders_and_unavailable_lock_refuse_without_releasing_anothers_lock(self) -> None:
        for sibling in (False, True):
            for holder in ("foreign", "unreadable", "nested", "inaccessible"):
                with self.subTest(sibling=sibling, holder=holder):
                    self.host = FakeHost(sibling=sibling)
                    lock = backuplock.ENGINE_LOCK if sibling else backuplock.CORPUS_LOCK
                    if holder == "inaccessible":
                        self.host.lock_error = True
                    else:
                        self.host.locks[lock.path] = "unreadable" if holder == "unreadable" else backuplock.Record(
                            "fictitious lock holder", os.getpid() if holder == "nested" else os.getpid() + 1000, NOW,
                        ).to_json()
                    if sibling and holder == "nested":
                        code, out, err = self.run_promote()
                        self.assertEqual((code, err), (0, ""), out)
                        self.assertIn("engine lock held by this process", out)
                        self.gates.clear()
                        continue
                    before = copy.deepcopy((self.host.rows, self.host.targets))
                    phrase = "could not be taken" if holder == "inaccessible" else "already running in this process" if holder == "nested" else "record unreadable" if holder == "unreadable" else "fictitious lock holder"
                    self.refuse("preconditions", phrase)
                    self.assertEqual(self.host.releases, [])
                    self.assert_untouched(before)

    def test_record_and_alias_read_failures_stop_before_gates(self) -> None:
        for event, stage in (
            ("candidate", "candidate"), ("report", "candidate"), ("promoted", "candidate"),
            ("lexical-read", "aliases"), ("dense-read", "aliases"),
            (f"job:index-{NAME}", "preconditions"), ("sibling-job", "preconditions"),
        ):
            with self.subTest(event=event):
                self.host = FakeHost(sibling=event == "sibling-job")
                self.host.failures[event] = 3
                before = copy.deepcopy((self.host.rows, self.host.targets))
                self.refuse(stage, "exit 3")
                self.assert_untouched(before)

    def test_failing_suite_prints_its_row_and_changes_nothing(self) -> None:
        self.gate_code = 9
        before = copy.deepcopy((self.host.rows, self.host.targets))
        out = self.refuse("gates", "failed (exit 9)")
        self.assertIn("suite-part: refuse — fictitious gate verdict", out)
        self.assertLess(out.index("suite-part:"), out.index("gates: refuse"))
        self.assertEqual((self.host.rows, self.host.targets), before)
        self.assertNotIn("flip", self.host.events)
        self.assertFalse(any(method == "POST" for _, method, _, _ in self.host.requests))
        self.assertIn(self.run_ids[0], out)
        self.assertNotIn("Next:", out)

    def test_passing_suite_flips_record_before_both_aliases_and_names_backup(self) -> None:
        before = copy.deepcopy((self.host.rows, self.host.targets))
        minted = UUID(int=731)
        with patch.object(promote, "uuid4", return_value=minted) as mint:
            code, out, err = self.run_promote()
        self.assertEqual((code, err), (0, ""), out)
        mint.assert_called_once_with()
        self.assertEqual(self.run_ids, [str(minted)])
        self.assertIn(f"gates: ok — build-gates run {minted} passed", out)
        self.assertEqual(self.snapshots, [before])
        rows = [line.split(":", 1)[0] for line in out.splitlines() if not line.startswith("Next:")]
        self.assertEqual(rows, [*STAGES[:3], "suite-part", *STAGES[3:]])
        self.assertEqual(self.host.rows[NAME].state, "live")
        self.assertEqual(self.host.rows[NAME].promoted_at, NOW.isoformat())
        self.assertEqual(self.host.rows[OLD].state, "retired")
        self.assertEqual(self.host.rows[OLD].retired_at, NOW.isoformat())
        self.assertEqual(self.host.targets, {"lexical": (NAME,), "dense": (NAME,)})
        self.assertEqual([event for event in self.host.events if event in {"gates", "flip", "lexical-write", "dense-write"}],
                         ["gates", "flip", "lexical-write", "dense-write"])
        candidate = next(line for line in out.splitlines() if line.startswith("candidate:"))
        self.assertIn(f"retiring {OLD}", candidate)
        self.assertIn(f"bytes {before[0][NAME].bytes}", candidate)
        self.assertIn(f"ready {before[0][NAME].counts['ready']}", candidate)
        assert self.host.build_report is not None
        self.assertIn(self.host.build_report.written_at, candidate)
        self.assertIn(f"flip: ok — {NAME}: promoted at {NOW.isoformat()}; retired {OLD}", out)
        self.assertIn("kept seven days, dropped by index gc", out)
        self.assertIn(f"Next: run {report.command('backup run --full')} to snapshot both stores with the promoted generation.", out)
        self.assertEqual(self.host.lock_paths, [backuplock.CORPUS_LOCK.path])
        self.assertEqual(self.gates[0].stack, PRODUCTION_STACK)
        self.assertEqual(vars(self.gates[0]), {
            "slice": "build-gates", "stack": PRODUCTION_STACK, "generation": NAME, "kind": "manual",
            "set": None, "ranked": None, "decision": False, "against": None, "force": False, "challenger": False,
        })

    def test_sibling_route_uses_its_record_secrets_lock_and_namespace_without_next_line(self) -> None:
        self.host = FakeHost(sibling=True)
        code, out, err = self.run_promote()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.lock_paths, [backuplock.ENGINE_LOCK.path])
        self.assertEqual(self.host.releases, [backuplock.ENGINE_LOCK.path])
        self.assertEqual(self.gates[0].stack, CI_STACK)
        self.assertEqual(self.gates[0].generation, self.host.name)
        self.assertEqual(self.gate_directories, [Path(CI_SECRETS_DIR)])
        self.assertTrue(self.host.reads)
        self.assertTrue(all(Path(path).parent == Path(CI_SECRETS_DIR) for path in self.host.reads))
        self.assertTrue(all(str(CI_ROOT) in " ".join(argv) for argv, _ in self.host.calls))
        self.assertIn(f"retiring {self.host.old}", out)
        self.assertEqual(self.host.rows[self.host.old].state, "retired")
        self.assertIn("outside every backup set", out)
        self.assertNotIn("Next:", out)

    def test_first_promotion_with_no_live_row_and_absent_aliases(self) -> None:
        del self.host.rows[OLD]
        self.host.targets = {"lexical": (), "dense": ()}
        code, out, err = self.run_promote()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("retiring none live", out)
        self.assertIn("retired none", out)
        self.assertEqual(self.host.targets, {"lexical": (NAME,), "dense": (NAME,)})

    def test_failure_after_lexical_then_rerun_converges_dense_without_gates(self) -> None:
        self.host.failures["dense-write"] = 7
        self.refuse("dense", "exit 7")
        self.assertEqual(self.host.rows[NAME].state, "live")
        self.assertEqual(self.host.rows[OLD].state, "retired")
        self.assertEqual(self.host.targets, {"lexical": (NAME,), "dense": (OLD,)})
        self.assertEqual(len(self.gates), 1)
        self.host.failures.clear()
        first_events = len(self.host.events)
        code, out, err = self.run_promote()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.gates), 1)
        self.assertEqual(self.host.targets, {"lexical": (NAME,), "dense": (NAME,)})
        self.assertNotIn("flip", self.host.events[first_events:])
        self.assertNotIn("lexical-write", self.host.events[first_events:])
        self.assertIn("dense-write", self.host.events[first_events:])
        self.assertIn("gates: ok — already live; skipped build-gates", out)
        self.assertIn("flip: ok — already live; skipped record promotion", out)
        self.assertIn("lexical: ok — unchanged", out)

    def test_live_candidate_converges_unchanged_without_reading_report_or_gates(self) -> None:
        del self.host.rows[OLD]
        self.host.rows[NAME] = replace(self.host.rows[NAME], state="live", promoted_at=NOW.isoformat())
        self.host.targets = {"lexical": (NAME,), "dense": (NAME,)}
        self.host.build_report = None
        code, out, err = self.run_promote()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.gates, [])
        self.assertNotIn("report", self.host.events)
        self.assertNotIn("flip", self.host.events)
        self.assertEqual([method for _, method, _, _ in self.host.requests], ["GET", "GET"])
        self.assertIn(f"already live since {NOW.isoformat()}; the aliases converged", out)
        self.assertNotIn("retiring", out)
        for store in ("lexical", "dense"):
            self.assertIn(f"{store}: ok — unchanged", out)

    def test_flip_failure_changes_no_alias_and_reports_concurrent_candidate_change(self) -> None:
        self.host.failures["flip"] = 3
        before = copy.deepcopy((self.host.rows, self.host.targets))
        self.refuse("flip", "promotion failed (exit 3)")
        self.assertEqual((self.host.rows, self.host.targets), before)
        self.assertFalse(any(method == "POST" for _, method, _, _ in self.host.requests))
        self.host.failures.clear()

        def change_candidate() -> None:
            self.host.rows[NAME] = replace(self.host.rows[NAME], state="failed", failure_reason="store")

        self.after_gates = change_candidate
        out = self.refuse("flip", "nothing was promoted or retired")
        self.assertEqual(self.host.rows[OLD].state, "live")
        self.assertIn(report.command(f"index report {NAME}"), out)
        self.assertEqual(self.host.targets, before[1])

    def test_each_alias_write_refusal_leaves_live_record_for_recovery(self) -> None:
        self.host.failures["lexical-write"] = 7
        self.refuse("lexical", "exit 7")
        self.assertEqual(self.host.rows[NAME].state, "live")
        self.assertNotIn("dense-write", self.host.events)
        self.assertEqual(self.host.targets, {"lexical": (OLD,), "dense": (OLD,)})

    def test_interrupts_release_the_lock_at_each_stage_on_both_stacks(self) -> None:
        for sibling in (False, True):
            for event in ("candidate", "report", "lexical-read", "gates", "flip", "lexical-write", "dense-write"):
                with self.subTest(sibling=sibling, event=event):
                    self.host = FakeHost(sibling=sibling)
                    self.gates.clear()
                    self.gate_interrupt = event == "gates"
                    if event != "gates":
                        self.host.interrupts.add(event)
                    code, out, err = self.run_promote()
                    self.assertEqual((code, err), (1, ""), out)
                    self.assertIn("interrupted", out)
                    self.assertIn(report.command(f"index promote {self.host.name}"), out)
                    expected = "live" if event in {"lexical-write", "dense-write"} else "built"
                    self.assertEqual(self.host.rows[self.host.name].state, expected)

    def test_credentials_and_sql_values_travel_only_on_stdin(self) -> None:
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.host = FakeHost(sibling=sibling)
                code, out, err = self.run_promote()
                self.assertEqual((code, err), (0, ""), out)
                values = [value.strip() for path, value in self.host.files.items() if "compose.yaml" not in path]
                for argv, _ in self.host.calls:
                    for value in (*values, self.host.name, "DO $promotion$"):
                        self.assertNotIn(value, " ".join(argv))
                for value in values:
                    self.assertNotIn(value, out + err)
                for store, secret_name in (("lexical", OPENSEARCH_PASSWORD_SECRET_NAME), ("dense", QDRANT_SECRET_NAME)):
                    credential = self.host.files[str(secrets.secret_path(secret_name))].strip()
                    service = OPENSEARCH_SERVICE_NAME if store == "lexical" else QDRANT_SERVICE_NAME
                    requests = [request for argv, request in self.host.calls if service in argv]
                    self.assertTrue(requests)
                    for request in requests:
                        assert request is not None
                        self.assertIn(credential, request)

    def test_module_has_no_evaluation_import_at_any_depth(self) -> None:
        tree = ast.parse(Path(promote.__file__).read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                imports.extend((base, *(f"{base}.{alias.name}" for alias in node.names)))
        self.assertFalse(any(name == "gideon.evaluation" or name.startswith("gideon.evaluation.") for name in imports), imports)
