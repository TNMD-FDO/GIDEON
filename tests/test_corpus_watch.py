"""Corpus watch stages over the cut's fictitious host and source operations."""

import argparse
import ast
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from test_corpus_cut import CUT_AT, RENDERED, ROOT, SNAPSHOT_DATE, FakeHost

from gideon.cli import main
from gideon.host import backuplock, fetch, report, stack, worker
from gideon.host.corpus import record, resolve, watch
from gideon.host.corpus.sources import (
    IndexRequest,
    SourceDefinition,
    SourceEntry,
    SourceResolution,
)
from gideon.host.egress import load_egress_allowlist
from gideon.host.render import worker as worker_identity
from gideon.host.sysio import Command, PathLike


class WatchHost(FakeHost):
    """The cut host with an append-only observation record and its state read."""

    def __init__(self) -> None:
        super().__init__()
        self.observations: list[record.Observation] = []
        self.bindings: dict[str, tuple[str, str]] = {}
        self.watch_writes = 0
        self.watch_reads = 0
        self.watch_write_code = 0
        self.watch_read_code = 0
        self.state_stdout: str | None = None
        self.invalid_first_seen = False
        self.stack_has_worker = True
        self.failure_reason = "refused-host"
        self.missing_index = False
        self.unreadable_file = False
        self.interrupt_on: str | None = None
        self.lockfile_rows: list[dict[str, object]] = []
        self.lockfile_reads = 0
        self.lockfile_read_code = 0
        self.lockfile_stdout: str | None = None

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        if str(path) == f"{RENDERED}/compose.yaml" and not self.stack_has_worker:
            return "services: {}\n"
        return super().read_text(path, encoding=encoding)

    def read_bytes(self, path: PathLike) -> bytes:
        if self.unreadable_file and "/resolve/" in str(path):
            raise OSError("fictitious index read failure")
        return super().read_bytes(path)

    def _write_fetch(self, job: int, destination: str, url: str, form: str) -> None:
        if self.missing_index and form == worker_identity.FRESH_FORM:
            return
        super()._write_fetch(job, destination, url, form)
        if destination == self.failed_destination:
            path = self.snapshots / f"{destination}{worker_identity.FAILURE_SUFFIX}"
            value = json.loads(path.read_text())
            value["reason"] = self.failure_reason
            path.write_text(json.dumps(value))

    def _watch_rows(self) -> str:
        rows: list[dict[str, object]] = []
        for source in sorted({item.source for item in self.observations}):
            items = sorted(
                (item for item in self.observations if item.source == source),
                key=lambda item: item.observed_at,
            )
            newest = items[-1]
            answers = [item for item in items if item.outcome == "observed"]
            answered = answers[-1] if answers else None
            pinned = self.bindings.get(source)
            first_seen = (
                min(item.observed_at for item in answers if item.latest_label == answered.latest_label)
                if answered is not None else None
            )
            unanswered_since = (
                min(
                    item.observed_at for item in items
                    if item.outcome == "unanswered"
                    and (answered is None or item.observed_at > answered.observed_at)
                )
                if newest.outcome == "unanswered" else None
            )
            row: dict[str, object] = {
                "source": source,
                "first_seen": "invalid" if self.invalid_first_seen else first_seen,
                "open": answered is not None and (
                    pinned is None or pinned[1] < (answered.latest_label or "")
                ),
                "pinned_label": pinned[0] if pinned is not None else None,
                "pinned_date": pinned[1] if pinned is not None else None,
                "unanswered_since": unanswered_since,
            }
            for prefix, item in (("newest", newest), ("answered", answered)):
                row.update({
                    f"{prefix}_at": item.observed_at if item is not None else None,
                    f"{prefix}_outcome": item.outcome if item is not None else None,
                    f"{prefix}_latest_label": item.latest_label if item is not None else None,
                    f"{prefix}_effective_date": item.effective_date if item is not None else None,
                    f"{prefix}_url": item.url if item is not None else None,
                    f"{prefix}_detail": item.detail if item is not None else None,
                })
            rows.append(row)
        return "".join(json.dumps(row) + "\n" for row in rows)

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
        command = list(argv)
        if command == worker.psql_argv(RENDERED) and input is not None:
            if "ORDER BY c.label" in input:
                self.calls.append(command)
                self.lockfile_reads += 1
                output = ("".join(json.dumps(row) + "\n" for row in self.lockfile_rows)
                          if self.lockfile_stdout is None else self.lockfile_stdout)
                return subprocess.CompletedProcess(
                    command, self.lockfile_read_code, output, "fictitious record read failure",
                )
            if self.interrupt_on == "observe" and "procrastinate_defer_jobs_v1" in input:
                raise KeyboardInterrupt
            if "INSERT INTO public.upstream_observations" in input:
                self.calls.append(command)
                if self.interrupt_on == "record":
                    raise KeyboardInterrupt
                self.watch_writes += 1
                if self.watch_write_code:
                    return subprocess.CompletedProcess(command, self.watch_write_code, "", "write failed")
                payload = json.loads(self._bound(input, "v_payload"))
                self.observations.extend(record.Observation(**item) for item in payload)
                return subprocess.CompletedProcess(command, 0, "", "")
            if "row_to_json(state)" in input:
                self.calls.append(command)
                self.watch_reads += 1
                if self.watch_read_code:
                    return subprocess.CompletedProcess(command, self.watch_read_code, "", "read failed")
                output = self._watch_rows() if self.state_stdout is None else self.state_stdout
                return subprocess.CompletedProcess(command, 0, output, "")
        return super().run(
            argv, check=check, input=input, cwd=cwd, env=env,
            timeout=timeout, passthrough=passthrough,
        )


class Watch(unittest.TestCase):
    """The command observes every source, keeps failures, and never cuts."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.checkout = root / "checkout"
        self.snapshots = root / "snapshots"
        (self.checkout / "config").mkdir(parents=True)
        shutil.copy2(ROOT / "config/egress.yaml", self.checkout / "config/egress.yaml")
        loaded = load_egress_allowlist(self.checkout / "config/egress.yaml")
        assert loaded.allowlist is not None
        group = loaded.allowlist.group("corpus")
        assert group is not None
        self.hostname = group.hosts[0].host
        self._reset_case()

    def _reset_case(self) -> None:
        shutil.copy2(ROOT / "config/egress.yaml", self.checkout / "config/egress.yaml")
        hostname = self.hostname
        self.source = SourceDefinition(
            "example", f"https://{hostname}/archive/", False, (),
            lambda: (IndexRequest("listing.xml", f"https://{hostname}/listing"),),
            lambda _documents: SourceResolution(
                SNAPSHOT_DATE,
                (SourceEntry("objects/example.txt", f"https://{hostname}/archive/example"),),
            ),
        )
        self.sources: tuple[SourceDefinition, ...] = (self.source,)
        self.host = WatchHost()
        self.host.snapshots = self.snapshots
        self.host.checkout = self.checkout
        self.now = CUT_AT

    def run_watch(self) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        elapsed = [0.0]

        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = watch.run_corpus_watch(
                argparse.Namespace(), host=self.host, rendered_dir=RENDERED,
                checkout=self.checkout, snapshots_root=self.snapshots,
                clock=lambda: self.now, sleep=advance, monotonic=lambda: elapsed[0],
                sources=self.sources,
            )
        return code, output.getvalue(), errors.getvalue()

    def _invalidate_egress(self) -> None:
        (self.checkout / "config/egress.yaml").write_text("bad: [")

    def _captured_source(self, name: str = "saved-example") -> SourceDefinition:
        def no_index_reader(_documents: Mapping[str, bytes]) -> SourceResolution:
            self.fail("A captured source must never resolve through a fetched index.")

        return SourceDefinition(
            name, "https://capture.example.invalid/table", False, (),
            lambda: (), no_index_reader, captured_file="saved.html",
        )

    def _record_binding(
        self, source: SourceDefinition, snapshot_date: str, label_date: str, *, state: str = "cut",
    ) -> None:
        """Keep the fake's lockfile reader and watch state on the same bindings."""
        label = f"corpus-{label_date}"
        self.host.lockfile_rows.append({
            "label": label, "schema": 1, "pipeline": worker_identity.PIPELINE_VERSION,
            "cut_at": f"{label_date}T00:00:00Z", "reason": "instrument", "base": None,
            "installed_at": None, "state": state,
            "sources": [{"source": source.name, "snapshot_date": snapshot_date}],
        })
        held = self.host.bindings.get(source.name)
        if held is None or held[1] < snapshot_date:
            self.host.bindings[source.name] = (label, snapshot_date)

    def test_captured_source_observed_from_newest_binding_without_an_open_notice(self) -> None:
        source = self._captured_source()
        self.sources = (self.source, source)
        self._record_binding(source, "2098-12-01", "2098-12-02")
        self._record_binding(source, "2099-01-02", "2099-01-02", state="superseded")
        self._record_binding(source, "2099-01-01", "2099-01-03")
        self._record_binding(self._captured_source("other"), "2099-02-01", "2099-02-02")
        expected_date = self.host.bindings[source.name][1]
        code, out, err = self.run_watch()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"observe: ok — {source.name}: {expected_date}", out)
        observed = next(item for item in self.host.observations if item.source == source.name)
        self.assertEqual((observed.outcome, observed.latest_label, observed.url, observed.detail),
                         ("observed", expected_date, source.base_url, None))
        self.assertEqual(self.host.lockfile_reads, 1)
        self.assertEqual(len(self.host.observations), 2)
        self.assertNotIn(f"Next: a cut is due for {source.name}", out)
        self.assertIn("2 observations written, 1 new notices, 1 open notices", out)
        self.assertIn(f"Next: a cut is due for {self.source.name}", out)
        self.assertEqual(self.host.calls, [
            stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"),
            worker.psql_argv(RENDERED), worker.psql_argv(RENDERED),
            worker.psql_argv(RENDERED), worker.psql_argv(RENDERED),
            worker.psql_argv(RENDERED),
        ])
        self.assertEqual(tuple(self.host.job_destinations.values()),
                         (fetch.resolve_destination("example/listing.xml"),))
        self.assertEqual(self.host.writes, [])
        self.assertEqual(self.host.record_writes, 0)

    def test_captured_sources_share_one_record_read_and_need_no_worker(self) -> None:
        first, second = self._captured_source(), self._captured_source("second-saved")
        self.sources = (first, second)
        self._record_binding(first, "2099-01-01", "2099-01-02")
        self._record_binding(second, "2099-01-02", "2099-01-03")
        self.host.health = "unhealthy"
        code, out, err = self.run_watch()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.host.lockfile_reads, 1)
        self.assertEqual([item.outcome for item in self.host.observations], ["observed", "observed"])
        self.assertIn("2 observations written, 0 new notices, 0 open notices", out)
        self.assertNotIn("Next:", out)
        self.assertEqual(self.host.job_destinations, {})
        self.assertEqual(self.host.calls, [worker.psql_argv(RENDERED)] * 3)

    def test_captured_source_not_yet_bound_prints_ok_and_writes_no_observation(self) -> None:
        source = self._captured_source()
        self.sources = (source,)
        code, out, err = self.run_watch()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"observe: ok — {source.name}: not yet cut or installed here", out)
        self.assertIn("0 observations written, 0 new notices, 0 open notices", out)
        self.assertEqual(self.host.observations, [])
        self.assertEqual(self.host.lockfile_reads, 1)
        self.assertEqual(self.host.job_destinations, {})
        self.assertNotIn("Next:", out)
        self.assertEqual(self.host.writes, [])

    def test_worker_outage_keeps_captured_observation(self) -> None:
        source = self._captured_source()
        self.sources = (self.source, source)
        self._record_binding(source, "2099-01-02", "2099-01-03")
        self.host.health = "unhealthy"
        code, out, err = self.run_watch()
        self.assertEqual(code, 1, out + err)
        self.assertEqual([(item.source, item.outcome, item.detail) for item in self.host.observations], [
            (self.source.name, "unanswered", "worker-unavailable"),
            (source.name, "observed", None),
        ])
        self.assertIn(f"observe: ok — {source.name}: 2099-01-02", out)
        self.assertIn("2 observations written, 0 new notices, 0 open notices", out)
        self.assertNotIn("Next:", out)
        self.assertEqual(self.host.job_destinations, {})
        self.assertEqual(self.host.lockfile_reads, 1)

    def test_record_read_failure_makes_captured_sources_unanswered_and_continues(self) -> None:
        for code, stdout, detail in ((127, None, "exit 127"), (0, "not json\n", "row is invalid")):
            with self.subTest(code=code, stdout=stdout):
                self._reset_case()
                first, second = self._captured_source(), self._captured_source("second-saved")
                self.sources = (first, self.source, second)
                self.host.lockfile_read_code = code
                self.host.lockfile_stdout = stdout
                issue = record.read_lockfiles(self.host, RENDERED, command_path="corpus watch")
                assert isinstance(issue, report.Problem)
                self.host.calls.clear()
                self.host.lockfile_reads = 0
                result, out, err = self.run_watch()
                self.assertEqual(result, 1, out + err)
                self.assertEqual([(item.source, item.outcome, item.detail)
                                  for item in self.host.observations], [
                    (first.name, "unanswered", "local"),
                    (self.source.name, "observed", None),
                    (second.name, "unanswered", "local"),
                ])
                self.assertIn(detail, out)
                self.assertIn(issue.fix, out)
                self.assertIn("3 observations written", out)
                self.assertEqual(self.host.lockfile_reads, 1)
                self.assertEqual(len(self.host.job_destinations), 1)

    def test_captured_provenance_https_checked_without_index_checks(self) -> None:
        requests = mock.Mock(return_value=())
        source = replace(self._captured_source(), index_documents=requests)
        self.sources = (source,)
        self.assertEqual(self.run_watch()[0], 0)
        requests.assert_not_called()
        for url in ("http://capture.example.invalid/table", "https://capture.example.invalid:444/table",
                    "https:///missing-host", "https://capture.example.invalid:invalid/table"):
            with self.subTest(url=url):
                self._reset_case()
                self.sources = (replace(source, base_url=url),)
                code, out, err = self.run_watch()
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("invalid HTTPS URL", err)
                self.assertIn(report.command("corpus watch"), err)
                self.assertEqual(self.host.lockfile_reads, 0)
                self.assertEqual(self.host.calls, [])
        requests.assert_not_called()

    def test_answered_source_writes_a_new_open_notice_with_exact_argv(self) -> None:
        code, out, err = self.run_watch()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("preconditions: ok", out)
        self.assertIn("observe: ok — example: 2099-01-02", out)
        self.assertIn("record: ok — 1 observations written, 1 new notices, 1 open notices", out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertEqual(len(self.host.observations), 1)
        observation = self.host.observations[0]
        self.assertEqual((observation.outcome, observation.latest_label, observation.detail),
                         ("observed", SNAPSHOT_DATE, None))
        self.assertEqual(observation.url, self.source.index_documents()[0].url)
        self.assertEqual(self.host.calls, [
            stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"),
            worker.psql_argv(RENDERED),
            worker.psql_argv(RENDERED),
            worker.psql_argv(RENDERED),
            worker.psql_argv(RENDERED),
        ])
        self.assertEqual(self.host.job_destinations[100],
                         fetch.resolve_destination("example/listing.xml"))
        self.assertEqual(self.host.writes, [])
        self.assertEqual(self.host.record_writes, 0)
        self.assertEqual(self.host.lock_releases, 1)

    def test_pre_run_refusals_name_a_fix_on_stderr(self) -> None:
        cases: Sequence[tuple[str, Callable[[], None]]] = (
            ("root privileges", lambda: setattr(self.host, "euid", 1000)),
            ("no worker", lambda: setattr(self.host, "stack_has_worker", False)),
            ("source registry is empty", lambda: setattr(self, "sources", ())),
            ("egress allowlist", self._invalidate_egress),
            ("outside the corpus allowlist", lambda: setattr(
                self, "sources", (replace(
                    self.source,
                    index_documents=lambda: (IndexRequest("listing.xml", "https://outside.example.test/listing"),),
                ),),
            )),
        )
        for detail, change in cases:
            with self.subTest(detail=detail):
                self._reset_case()
                change()
                code, out, err = self.run_watch()
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(detail, err)
                self.assertIn("Fix:", err)
                self.assertIn(report.command("corpus watch"), err)
                self.assertEqual(self.host.calls, [])

    def test_held_lock_refuses_then_a_run_releases_it(self) -> None:
        self.host.lock_holder = backuplock.Record("gideon corpus cut", 123, CUT_AT).to_json()
        code, out, err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("gideon corpus cut", err)
        self.assertIn(report.command("corpus watch"), err)
        self.assertEqual(self.host.lock_releases, 0)
        self.host.lock_holder = None
        self.assertEqual(self.run_watch()[0], 0)
        self.assertEqual(self.host.lock_releases, 1)

    def test_worker_outage_records_every_source_without_defer(self) -> None:
        self.sources = (self.source, replace(self.source, name="second"))
        self.host.health = "unhealthy"
        code, out, err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(out.count("observe: refuse"), 2)
        self.assertIn("record: ok — 2 observations written", out)
        self.assertEqual([item.detail for item in self.host.observations],
                         ["worker-unavailable", "worker-unavailable"])
        self.assertIn(report.command("corpus watch"), out)
        self.assertEqual(self.host.job_destinations, {})
        self.assertEqual(self.host.calls, [
            stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"),
            worker.psql_argv(RENDERED), worker.psql_argv(RENDERED),
        ])

    def test_unanswered_source_does_not_stop_the_walk_or_record(self) -> None:
        self.sources = (self.source, replace(self.source, name="second"))
        self.host.failed_destination = fetch.resolve_destination("second/listing.xml")
        code, out, err = self.run_watch()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out.count("observe: ok"), 1)
        self.assertEqual(out.count("observe: refuse"), 1)
        self.assertIn("record: ok — 2 observations written", out)
        self.assertEqual([(item.source, item.outcome, item.detail) for item in self.host.observations], [
            ("example", "observed", None), ("second", "unanswered", "refused-host"),
        ])
        self.assertIn("corpus group", out)
        self.assertIn(report.command("corpus watch"), out)

    def test_recorded_fetch_reasons_and_resolve_reasons(self) -> None:
        for reason in worker_identity.FAILURE_REASONS:
            with self.subTest(reason=reason):
                self._reset_case()
                self.host.failed_destination = fetch.resolve_destination("example/listing.xml")
                self.host.failure_reason = reason
                code, out, _err = self.run_watch()
                self.assertEqual(code, 1)
                self.assertEqual(self.host.observations[0].detail, reason)
                self.assertIn(f"unanswered ({reason})", out)
        self._reset_case()
        self.host.resolve_wait_polls = resolve.RESOLVE_TIMEOUT_SECONDS + 1
        self.assertEqual(self.run_watch()[0], 1)
        self.assertEqual(self.host.observations[0].detail, "timeout")
        self._reset_case()
        self.host.missing_index = True
        self.assertEqual(self.run_watch()[0], 1)
        self.assertEqual(self.host.observations[0].detail, "missing-index")
        self._reset_case()
        self.sources = (replace(
            self.source,
            read_index=lambda _documents: report.Problem(
                "fictitious index unreadable", "Check the upstream index, then retry."
            ),
        ),)
        code, out, _err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(self.host.observations[0].detail, "unreadable-index")
        self.assertIn(report.command("corpus watch"), out)
        self._reset_case()
        self.host.unreadable_file = True
        self.assertEqual(self.run_watch()[0], 1)
        self.assertEqual(self.host.observations[0].detail, "local")

    def test_only_a_new_open_date_prints_a_next_step(self) -> None:
        self.assertIn("Next: a cut is due", self.run_watch()[1])
        self.now += timedelta(days=1)
        code, out, _err = self.run_watch()
        self.assertEqual(code, 0)
        self.assertIn("0 new notices, 1 open notices", out)
        self.assertNotIn("Next:", out)
        self._reset_case()
        self.host.bindings["example"] = ("corpus-2099-01-03", SNAPSHOT_DATE)
        code, out, _err = self.run_watch()
        self.assertEqual(code, 0)
        self.assertIn("0 new notices, 0 open notices", out)
        self.assertNotIn("Next:", out)

    def test_record_write_and_read_failures_refuse(self) -> None:
        self.host.watch_write_code = 3
        code, out, _err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", out)
        self.assertIn("exit 3", out)
        self.assertIn(report.command("corpus watch"), out)
        self.assertEqual(self.host.watch_reads, 0)
        self._reset_case()
        self.host.watch_read_code = 4
        code, out, _err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", out)
        self.assertIn("exit 4", out)
        self.assertIn(report.command("corpus watch"), out)
        self.assertEqual(self.host.watch_writes, 1)

    def test_missing_or_invalid_readback_refuses(self) -> None:
        self.host.state_stdout = ""
        code, out, _err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertIn("watch state is missing sources", out)
        self.assertIn(report.command("corpus watch"), out)
        self._reset_case()
        self.host.invalid_first_seen = True
        code, out, _err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertIn("record: refuse — upstream observation row is invalid", out)

    def test_interrupt_releases_lock_and_prints_one_line(self) -> None:
        self.host.interrupt_on = "observe"
        code, out, err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(out.count("interrupted"), 1)
        self.assertIn("nothing was recorded", out)
        self.assertEqual(self.host.lock_releases, 1)
        self._reset_case()
        self.host.interrupt_on = "record"
        code, out, err = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(out.count("interrupted"), 1)
        self.assertIn("record outcome is unknown", out)
        self.assertEqual(self.host.lock_releases, 1)

    def test_cli_route_and_no_cut_import(self) -> None:
        with mock.patch.object(watch, "run_corpus_watch", return_value=0) as run:
            self.assertEqual(main(["corpus", "watch"]), 0)
        self.assertEqual(run.call_args.args[0].command_path, "corpus watch")
        tree = ast.parse(Path(watch.__file__).read_text())
        imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        self.assertNotIn("cut", imports)
        self.assertNotIn("gideon.host.corpus.cut", imports)
