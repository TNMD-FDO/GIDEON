"""The read-only box status report."""

import argparse
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from unittest.mock import patch

import gideon
from gideon import cli
from gideon.host import backupset, grafana, nogpu, owui, report, secrets, stack
from gideon.host.checks.capacity import DATA_DF_ARGV
from gideon.host.render.ci import CI_ROOT
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike, RealHost
from gideon.improvement import feedback, ratings, trips
from gideon.improvement.sections import Context, Row, Scope, Section, SectionReport
from gideon.status import command, glance

ROOT = Path(__file__).resolve().parent.parent
SITE_PATH = Path("/tmp/gideon-status/site.yaml")
BAD_SITE_PATH = Path("/tmp/gideon-status/invalid-site.yaml")
RENDERED = Path("/tmp/gideon-status/rendered")
CHECKOUT = Path("/tmp/gideon-status/checkout")
TRIGGERS_PATH = CHECKOUT / "config/triggers.yaml"
STAGING = Path("/tmp/gideon-status/staging")
NOW = datetime(2099, 1, 3, 12, tzinfo=UTC)
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nFICTITIOUS\n-----END CERTIFICATE-----\n"
SITE_TEXT = (ROOT / "config/site.example.yaml").read_text(encoding="utf-8")
TRIGGERS_TEXT = (ROOT / "config/triggers.yaml").read_text(encoding="utf-8")
COMPOSE_TEXT = "services:\n  grafana: {}\n  postgres: {}\n"
SIBLING_COMPOSE_TEXT = "services:\n  sibling-a: {}\n  sibling-b: {}\n  sibling-c: {}\n"
SET_LABEL = "20990103T090000Z"


def _manifest(finished: datetime) -> str:
    started = finished - timedelta(minutes=1)
    document = {
        "archive_through": finished.isoformat(),
        "checkout": "/fictitious/checkout",
        "commit": "fictitious-commit",
        "finished": finished.isoformat(),
        "hard_links": {"linked": 0, "sampled": 0},
        "hostname": "gideon.example.org",
        "inventory": {},
        "kind": "nightly",
        "label": SET_LABEL,
        "pgbackrest_label": "fictitious-backup-label",
        "pgbackrest_type": "full",
        "previous_label": None,
        "recipient": "age1fictitiousrecipient",
        "recipients": ["age1fictitiousrecipient"],
        "release": "v1000.0.0",
        "row_counts": {},
        "secrets_fingerprint": "f" * 64,
        "started": started.isoformat(),
        "tarball_sha256": "b" * 64,
        "version": 1,
    }
    return json.dumps(document)


def _newest_run(**overrides: object) -> str:
    row: dict[str, object] = {
        "run_id": "11111111-2222-4333-8444-555555555555",
        "slice": "fixture-slice",
        "kind": "smoke",
        "stack": "ci",
        "verdict": "pass",
        "partial": False,
        "finished_at": (NOW - timedelta(hours=3)).isoformat(),
    }
    row.update(overrides)
    return json.dumps(row)


def _developer_lines(stdout: str) -> tuple[str, ...]:
    block = stdout.split("developer\n", 1)[1].split("\nstatus:", 1)[0]
    return tuple(block.splitlines())


def _alert(
    title: str,
    started_at: str,
    *,
    alert_class: str = "page",
    heartbeat: bool = False,
    nudge: str | None = None,
    state: str = "active",
    summary: str = "Fictitious summary",
    runbook: str = "docs/runbooks/observability.md#example",
    silenced: bool = False,
) -> grafana.Alert:
    labels = {"alertname": title, "class": "dashboard" if heartbeat else alert_class}
    if heartbeat:
        labels["heartbeat"] = "true"
    if nudge is not None:
        labels["nudge"] = nudge
    return grafana.Alert(
        labels=labels,
        annotations={"summary": summary, "runbook": runbook},
        starts_at=started_at,
        state=state,
        silenced=silenced,
    )


class ReadOnlyHost:
    """Dict-backed Host that answers status reads and refuses every write."""

    def __init__(
        self,
        *,
        euid: int = 0,
        files: Mapping[str, str] | None = None,
        directories: Sequence[str] = (),
        compose_rc: int = 0,
        compose_stdout: str | None = None,
        sibling_compose_rc: int = 0,
        sibling_compose_stdout: str | None = None,
        psql_rc: int = 0,
        psql_stdout: str = "",
        eval_run_rc: int = 0,
        eval_run_stdout: str = "null",
        guardrail_rc: int = 0,
        guardrail_stdout: str = "",
        df_rc: int = 0,
        df_stdout: str = "Size Avail\n2000000000 500000000\n",
        handshake_rc: int = 0,
        handshake_stdout: str | None = None,
        x509_rc: int = 0,
        x509_stdout: str = "notAfter=Jan 13 12:00:00 2099 GMT\n",
        set_names: Sequence[str] = (SET_LABEL,),
    ) -> None:
        self.euid = euid
        self.files = dict(files or {})
        self.directories = set(directories)
        self.set_names = tuple(set_names)
        self.compose_rc = compose_rc
        self.compose_stdout = compose_stdout or json.dumps(
            [
                {"Service": "grafana", "State": "running"},
                {"Service": "postgres", "State": "exited"},
            ]
        )
        self.sibling_compose_rc = sibling_compose_rc
        self.sibling_compose_stdout = (
            sibling_compose_stdout
            if sibling_compose_stdout is not None
            else json.dumps(
                [
                    {"Service": name, "State": "running"}
                    for name in ("sibling-a", "sibling-b", "sibling-c")
                ]
            )
        )
        self.psql_rc = psql_rc
        self.psql_stdout = psql_stdout
        self.eval_run_rc = eval_run_rc
        self.eval_run_stdout = eval_run_stdout
        self.guardrail_rc = guardrail_rc
        self.guardrail_stdout = guardrail_stdout
        self.df_rc = df_rc
        self.df_stdout = df_stdout
        self.handshake_rc = handshake_rc
        self.handshake_stdout = (
            handshake_stdout
            if handshake_stdout is not None
            else f"CONNECTED\n{CERTIFICATE}---\n"
        )
        self.x509_rc = x509_rc
        self.x509_stdout = x509_stdout
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.write_attempted = False
        for path in (*self.files, *self.directories):
            self.directories.update(parent.as_posix() for parent in Path(path).parents)

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
        del check, cwd, env, timeout, passthrough
        command_argv = tuple(argv)
        self.calls.append((command_argv, input))
        if command_argv[:2] == ("docker", "compose"):
            if "psql" in command_argv:
                if input is not None and "FROM eval_runs" in input:
                    return self._completed(
                        command_argv, self.eval_run_rc, self.eval_run_stdout
                    )
                if input is not None and "FROM guardrail_trips" in input:
                    return self._completed(
                        command_argv, self.guardrail_rc, self.guardrail_stdout
                    )
                if input is not None and "FROM public.upstream_observations" in input:
                    return self._completed(command_argv, self.psql_rc)
                return self._completed(command_argv, self.psql_rc, self.psql_stdout)
            if "ps" in command_argv:
                project = command_argv[command_argv.index("--project-directory") + 1]
                if project == os.fspath(RENDERED):
                    return self._completed(command_argv, self.compose_rc, self.compose_stdout)
                if project == os.fspath(CI_ROOT):
                    return self._completed(
                        command_argv, self.sibling_compose_rc, self.sibling_compose_stdout
                    )
                raise AssertionError(f"unexpected Compose project: {project}")
            raise AssertionError(f"unexpected Compose command: {command_argv}")
        if command_argv == DATA_DF_ARGV:
            return self._completed(command_argv, self.df_rc, self.df_stdout)
        if command_argv[:2] == ("openssl", "s_client"):
            return self._completed(
                command_argv,
                self.handshake_rc,
                self.handshake_stdout,
                "fictitious handshake failure" if self.handshake_rc else "",
            )
        if command_argv[:2] == ("openssl", "x509") and "-enddate" in command_argv:
            return self._completed(command_argv, self.x509_rc, self.x509_stdout)
        raise AssertionError(f"unexpected host command: {command_argv}")

    @staticmethod
    def _completed(
        argv: tuple[str, ...], rc: int, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del path, text, encoding, mode
        self._refuse_write()

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path).rstrip("/")
        if key == os.fspath(STAGING / "sets"):
            return list(self.set_names)
        if key not in self.directories:
            raise FileNotFoundError(key)
        prefix = f"{key}/"
        children = {
            child[len(prefix) :].split("/", 1)[0]
            for child in (*self.files, *self.directories)
            if child.startswith(prefix) and child != key
        }
        return sorted(children)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok
        self._refuse_write()

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode
        self._refuse_write()

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid
        self._refuse_write()

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok
        self._refuse_write()

    def geteuid(self) -> int:
        return self.euid

    def take_lock(self, path: PathLike, record: str) -> str | None:
        del path, record
        return self._refuse_write()

    def release_lock(self, path: PathLike) -> None:
        del path
        self._refuse_write()

    def _refuse_write(self) -> NoReturn:
        self.write_attempted = True
        raise AssertionError("status attempted a write")


def _host(
    *,
    psql_stdout: str | None = None,
    set_names: Sequence[str] = (SET_LABEL,),
    build_box: bool = False,
    **overrides: Any,
) -> ReadOnlyHost:
    files = {
        os.fspath(SITE_PATH): SITE_TEXT,
        os.fspath(TRIGGERS_PATH): TRIGGERS_TEXT,
        os.fspath(RENDERED / "compose.yaml"): COMPOSE_TEXT,
        os.fspath(secrets.secret_path("grafana_admin_password")): "fictitious-grafana-secret\n",
    }
    if build_box:
        files[os.fspath(nogpu.BUILD_BOX_PATH)] = "fictitious build-box marker\n"
        files[os.fspath(Path(CI_ROOT) / "compose.yaml")] = SIBLING_COMPOSE_TEXT
    directories = [os.fspath(STAGING / "sets")]
    if set_names:
        directories.append(os.fspath(STAGING / "sets" / SET_LABEL))
        files[os.fspath(STAGING / "sets" / SET_LABEL / backupset.MANIFEST_NAME)] = _manifest(
            NOW - timedelta(hours=3)
        )
    if psql_stdout is None:
        psql_stdout = (
            f"backup_push|{(NOW - timedelta(hours=1)).isoformat()}|recorded-set\n"
            f"backup_drill|{(NOW - timedelta(hours=2)).isoformat()}|passed\n"
        )
    return ReadOnlyHost(
        files=files,
        directories=directories,
        psql_stdout=psql_stdout,
        set_names=set_names,
        **overrides,
    )


class FakeGrafana:
    def __init__(self, alerts: Sequence[grafana.Alert] = (), error: Exception | None = None) -> None:
        self.alerts = tuple(alerts)
        self.error = error
        self.reads = 0

    def get_alerts(self) -> tuple[grafana.Alert, ...]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.alerts


class FakeSection:
    def __init__(
        self,
        name: str,
        scope: Scope,
        result: SectionReport | Problem,
    ) -> None:
        self.name = name
        self.scope: Scope = scope
        self.result = result
        self.calls = 0

    def render(self, context: Context) -> SectionReport | Problem:
        del context
        self.calls += 1
        return self.result


class FeedbackContextSection:
    name: str = "feedback-context"
    scope: Scope = "office"

    def __init__(self) -> None:
        self.reading: feedback.FeedbackReading | Problem | None = None
        self.clock: float | None = None

    def render(self, context: Context) -> SectionReport | Problem:
        self.reading = context.feedback()
        self.clock = context.now()
        if isinstance(self.reading, Problem):
            return self.reading
        return SectionReport("read complete", ())


class Status(unittest.TestCase):
    def setUp(self) -> None:
        self.hosts: list[ReadOnlyHost] = []

    def tearDown(self) -> None:
        for host in self.hosts:
            self.assertFalse(host.write_attempted)

    def make_host(self, **overrides: Any) -> ReadOnlyHost:
        host = _host(**overrides)
        self.hosts.append(host)
        return host

    def run_status(
        self,
        host: ReadOnlyHost,
        fake: FakeGrafana | None = None,
        *,
        factory_error: Exception | None = None,
        sections: Sequence[Section] | None = None,
        owui_factory: Any = None,
        site_path: PathLike = SITE_PATH,
        triggers_path: PathLike = TRIGGERS_PATH,
    ) -> tuple[int, str, str, list[object]]:
        out, err = io.StringIO(), io.StringIO()
        factory_calls: list[object] = []

        def client_factory(**kwargs: object) -> grafana.Client:
            factory_calls.append(kwargs.get("credential"))
            if factory_error is not None:
                raise factory_error
            return cast(grafana.Client, fake or FakeGrafana())

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = command.run_status(
                argparse.Namespace(),
                host=host,
                site_path=site_path,
                rendered_dir=RENDERED,
                staging=STAGING,
                checkout_root=CHECKOUT,
                triggers_path=triggers_path,
                client_factory=client_factory,
                owui_client_factory=owui_factory,
                sections=cast(Sequence[Section] | None, sections or ()),
                now=NOW,
            )
        return code, out.getvalue(), err.getvalue(), factory_calls

    def test_firing_alert_prints_three_blocks_and_exits_one(self) -> None:
        host = self.make_host()
        fake = FakeGrafana((_alert("FictitiousPage", (NOW - timedelta(minutes=30)).isoformat()),))
        code, stdout, stderr, factory_calls = self.run_status(host, fake)

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            factory_calls,
            [(GRAFANA_ADMIN_USER, "fictitious-grafana-secret")],
        )
        self.assertIn(
            "needs attention\nFictitiousPage: Fictitious summary"
            " — docs/runbooks/observability.md#example (firing 30m)\n",
            stdout,
        )
        self.assertIn("waiting on you\nnone\nat a glance", stdout)
        self.assertTrue(stdout.rstrip().endswith("status: 1 need attention"))
        self.assertEqual(fake.reads, 1)

    def test_clear_alerts_exit_zero_and_glance_uses_each_reader(self) -> None:
        host = self.make_host()
        fake = FakeGrafana()
        code, stdout, stderr, _ = self.run_status(host, fake)

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("needs attention\nnone\nwaiting on you\nnone", stdout)
        self.assertIn(f"version: {gideon.__version__}", stdout)
        self.assertIn("services: 1 of 2 up; not running: postgres", stdout)
        self.assertIn(f"backup set: {SET_LABEL}, 3h ago", stdout)
        self.assertIn("off-box push: recorded-set pushed 1h ago", stdout)
        self.assertIn("drill: passed, 2h ago", stdout)
        self.assertIn("TLS certificate: 10 days left, expires 2099-01-13", stdout)
        self.assertIn("/data: 0.5 GB free of 2.0 GB (25.0%)", stdout)
        self.assertTrue(stdout.rstrip().endswith("status: nothing needs attention"))
        self.assertEqual(fake.reads, 1)
        self.assertEqual(sum("psql" in argv for argv, _ in host.calls), 1)

    def test_developer_block_order_and_build_box_marker_gate(self) -> None:
        for mode in ("build box", "no marker", "marker pair"):
            with self.subTest(mode=mode):
                host = self.make_host(build_box=True, eval_run_stdout=_newest_run())
                if mode == "no marker":
                    del host.files[os.fspath(nogpu.BUILD_BOX_PATH)]
                elif mode == "marker pair":
                    host.files[os.fspath(nogpu.NO_GPU_PATH)] = "fictitious no-GPU marker\n"
                product = FakeSection(
                    "product-section",
                    "product",
                    SectionReport("pending", (Row("build-action", "fired", "review it"),)),
                )

                code, stdout, stderr, _ = self.run_status(
                    host, FakeGrafana(), sections=(product,)
                )

                self.assertEqual((code, stderr), (0, ""))
                sibling_reads = [
                    argv for argv, _ in host.calls
                    if "ps" in argv and argv[argv.index("--project-directory") + 1] == CI_ROOT
                ]
                newest_reads = [
                    sql for _argv, sql in host.calls if sql is not None and "FROM eval_runs" in sql
                ]
                if mode == "build box":
                    headers = ("needs attention", "waiting on you", "at a glance", "developer")
                    positions = [stdout.index(f"{header}\n") for header in headers]
                    self.assertEqual(positions, sorted(positions))
                    self.assertLess(positions[-1], stdout.index("status: nothing needs attention"))
                    self.assertIn("sibling stack: up, 3 of 3 services", stdout)
                    self.assertIn("eval run: fixture-slice smoke on ci: pass, 3h ago", stdout)
                    self.assertIn("product-section: build-action — review it", stdout)
                    self.assertEqual(product.calls, 1)
                    self.assertEqual((len(sibling_reads), len(newest_reads)), (1, 1))
                else:
                    self.assertNotIn("\ndeveloper\n", stdout)
                    self.assertNotIn("build-action", stdout)
                    self.assertEqual(product.calls, 0)
                    self.assertEqual((sibling_reads, newest_reads), ([], []))

    def test_sibling_service_states_and_fixes(self) -> None:
        partial = json.dumps(
            [
                {"Service": "sibling-a", "State": "running"},
                {"Service": "sibling-b", "State": "exited"},
            ]
        )
        cases = (
            ("not converged", None, 0, "sibling stack: down, not converged", "cistack up"),
            ("all up", "default", 0, "sibling stack: up, 3 of 3 services", None),
            ("all down", "[]", 0, "sibling stack: down, 0 of 3 services", "cistack up"),
            (
                "partly up", partial, 0,
                "sibling stack: 1 of 3 services up; not running: sibling-b, sibling-c",
                "cistack status",
            ),
            (
                "Compose failed", "default", 2,
                "sibling stack: could not read — Compose service status is unavailable.",
                "cistack status",
            ),
            (
                "invalid compose", "default", 0,
                "sibling stack: could not read — rendered Compose file has no services map.",
                "cistack status",
            ),
        )
        for name, answer, rc, detail, fix in cases:
            with self.subTest(name=name):
                host = self.make_host(
                    build_box=True,
                    sibling_compose_rc=rc,
                    sibling_compose_stdout=None if answer == "default" else answer,
                )
                if name == "not converged":
                    del host.files[os.fspath(Path(CI_ROOT) / "compose.yaml")]
                elif name == "invalid compose":
                    host.files[os.fspath(Path(CI_ROOT) / "compose.yaml")] = "services: []\n"

                code, stdout, stderr, _ = self.run_status(host, FakeGrafana())

                self.assertEqual((code, stderr), (0, ""))
                line = next(line for line in _developer_lines(stdout) if line.startswith("sibling stack:"))
                self.assertTrue(line.startswith(detail))
                if fix is None:
                    self.assertNotIn("Fix:", line)
                else:
                    self.assertIn(f"Fix: Run sudo python3 -m tools.{fix}, then retry.", line)
                    self.assertNotIn("gideon apply", line)
                sibling_reads = [
                    argv for argv, _ in host.calls
                    if "ps" in argv and argv[argv.index("--project-directory") + 1] == CI_ROOT
                ]
                self.assertEqual(len(sibling_reads), 0 if name in ("not converged", "invalid compose") else 1)

    def test_sibling_exists_failure_and_production_compose_are_independent(self) -> None:
        host = self.make_host(build_box=True)
        original_exists = host.exists

        def fail_sibling_exists(path: PathLike) -> bool:
            if os.fspath(path) == os.fspath(Path(CI_ROOT) / "compose.yaml"):
                raise OSError("fictitious unreadable sibling")
            return original_exists(path)

        with patch.object(host, "exists", side_effect=fail_sibling_exists):
            code, stdout, stderr, _ = self.run_status(host, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(
            "sibling stack: could not read — sibling Compose file is unavailable. "
            "Fix: Run sudo python3 -m tools.cistack status, then retry.",
            stdout,
        )
        self.assertIn("services: 1 of 2 up; not running: postgres", stdout)

        production_down = self.make_host(build_box=True, compose_rc=2)
        code, stdout, stderr, _ = self.run_status(production_down, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("services: could not read", stdout)
        self.assertIn("sibling stack: up, 3 of 3 services", stdout)

    def test_eval_run_line_age_partial_and_null_slice(self) -> None:
        cases = (
            (
                "complete",
                _newest_run(),
                "eval run: fixture-slice smoke on ci: pass, 3h ago",
            ),
            (
                "partial",
                _newest_run(
                    partial=True,
                    verdict="fail",
                    finished_at=(NOW - timedelta(minutes=15)).isoformat(),
                ),
                "eval run: fixture-slice smoke on ci: fail (partial), 15m ago",
            ),
            (
                "no slice",
                _newest_run(slice=None),
                "eval run: no slice smoke on ci: pass, 3h ago",
            ),
        )
        for name, document, expected in cases:
            with self.subTest(name=name):
                host = self.make_host(build_box=True, eval_run_stdout=document)
                code, stdout, stderr, _ = self.run_status(host, FakeGrafana())
                self.assertEqual((code, stderr), (0, ""))
                self.assertIn(expected, _developer_lines(stdout))
                self.assertTrue(
                    any("FROM eval_runs" in (sql or "") for _argv, sql in host.calls)
                )

    def test_eval_run_none_yet_and_failed_read_have_their_fixes(self) -> None:
        empty = self.make_host(build_box=True)
        code, stdout, stderr, _ = self.run_status(empty, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(
            "eval run: none yet Fix: Run sudo python3 -m tools.cistack smoke, then retry.",
            _developer_lines(stdout),
        )

        failed = self.make_host(build_box=True, eval_run_rc=3)
        code, stdout, stderr, _ = self.run_status(failed, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(
            f"eval run: could not read — eval reader failed: exit 3 "
            f"Fix: {stack.logs_fix(RENDERED, 'postgres')}",
            _developer_lines(stdout),
        )

    def test_product_sections_print_fired_rows_and_failures_once(self) -> None:
        first = FakeSection(
            "triggers",
            "product",
            SectionReport(
                "trigger details",
                (
                    Row("fired-one", "fired", "first decision"),
                    Row("quiet", "not fired", "quiet detail"),
                    Row("unmeasured", "not yet measurable", "unmeasured detail"),
                    Row("rated", "rated", "rated detail"),
                ),
            ),
        )
        second = FakeSection(
            "challenger",
            "product",
            SectionReport("challenger details", (Row("fired-two", "fired", "second decision"),)),
        )
        broken = FakeSection(
            "broken-product", "product", Problem("fictional reader failed", "Run the fictional reader.")
        )
        office = FakeSection(
            "office-section",
            "office",
            SectionReport("office details", (Row("office-action", "fired", "office decision"),)),
        )
        host = self.make_host(build_box=True)

        code, stdout, stderr, _ = self.run_status(
            host, FakeGrafana(), sections=(first, office, broken, second)
        )

        self.assertEqual((code, stderr), (0, ""))
        waiting = stdout.split("waiting on you\n", 1)[1].split("\nat a glance", 1)[0]
        developer = _developer_lines(stdout)
        self.assertEqual(waiting, "office-action: office decision")
        self.assertIn("triggers: fired-one — first decision", developer)
        self.assertIn("challenger: fired-two — second decision", developer)
        self.assertIn(
            "broken-product: could not read — fictional reader failed "
            "Fix: Run the fictional reader.",
            developer,
        )
        self.assertLess(
            developer.index("triggers: fired-one — first decision"),
            developer.index("challenger: fired-two — second decision"),
        )
        for hidden in ("quiet", "unmeasured", "rated", "office-action", "none fired"):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, "\n".join(developer))
        self.assertEqual((first.calls, office.calls, broken.calls, second.calls), (1, 1, 1, 1))

    def test_product_sections_report_none_fired(self) -> None:
        product = FakeSection(
            "triggers",
            "product",
            SectionReport(
                "quiet",
                (
                    Row("not-fired", "not fired", "quiet detail"),
                    Row("rated", "rated", "rated detail"),
                ),
            ),
        )
        host = self.make_host(build_box=True)
        code, stdout, stderr, _ = self.run_status(host, FakeGrafana(), sections=(product,))

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            _developer_lines(stdout),
            (
                "sibling stack: up, 3 of 3 services",
                "eval run: none yet Fix: Run sudo python3 -m tools.cistack smoke, then retry.",
                "proposals: none fired",
            ),
        )
        self.assertEqual(product.calls, 1)

    def test_unloadable_registry_prints_the_same_line_in_both_blocks(self) -> None:
        host = self.make_host(build_box=True)
        host.files[os.fspath(TRIGGERS_PATH)] = "version: [not valid\n"
        product = FakeSection(
            "product-section",
            "product",
            SectionReport("would fire", (Row("hidden", "fired", "hidden"),)),
        )

        code, stdout, stderr, _ = self.run_status(host, FakeGrafana(), sections=(product,))

        self.assertEqual((code, stderr), (0, ""))
        waiting = stdout.split("waiting on you\n", 1)[1].split("\nat a glance", 1)[0]
        self.assertTrue(waiting.startswith("could not check — trigger registry could not be loaded Fix:"))
        self.assertEqual(_developer_lines(stdout)[-1], waiting)
        self.assertEqual(stdout.count(waiting), 2)
        self.assertEqual(product.calls, 0)
        self.assertIn("sibling stack:", stdout)
        self.assertIn("eval run:", stdout)

    def test_developer_failures_never_change_attention_exit_or_write(self) -> None:
        cases = (
            (0, FakeGrafana(), "status: nothing needs attention"),
            (
                1,
                FakeGrafana((_alert("FictitiousPage", NOW.isoformat()),)),
                "status: 1 need attention",
            ),
            (
                2,
                FakeGrafana(error=grafana.GrafanaError("fictional Grafana failure")),
                "status: could not check",
            ),
        )
        for expected_code, grafana_reader, closing in cases:
            with self.subTest(expected_code=expected_code):
                host = self.make_host(
                    build_box=True,
                    sibling_compose_rc=2,
                    eval_run_rc=3,
                )
                product = FakeSection(
                    "broken-product", "product", Problem("fictional read failed", "Fix the fiction.")
                )

                code, stdout, stderr, _ = self.run_status(
                    host, grafana_reader, sections=(product,)
                )

                self.assertEqual((code, stderr), (expected_code, ""))
                self.assertTrue(stdout.rstrip().endswith(closing))
                developer = _developer_lines(stdout)
                self.assertEqual(sum("could not read" in line for line in developer), 3)
                self.assertTrue(all("Fix:" in line for line in developer))
                self.assertEqual(product.calls, 1)
                self.assertFalse(host.write_attempted)

    def test_silenced_page_is_printed_but_does_not_make_status_red(self) -> None:
        host = self.make_host()
        fake = FakeGrafana(
            (_alert("SuppressedPage", NOW.isoformat(), state="suppressed", silenced=False),)
        )
        code, stdout, stderr, _ = self.run_status(host, fake)

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("SuppressedPage: Fictitious summary", stdout)
        self.assertIn("(silenced, firing 0m)", stdout)
        self.assertTrue(stdout.rstrip().endswith("status: nothing needs attention"))

    def test_heartbeat_and_dashboard_are_filtered_and_pages_sort_by_start(self) -> None:
        host = self.make_host()
        fake = FakeGrafana(
            (
                _alert("ZetaPage", (NOW - timedelta(minutes=20)).isoformat()),
                _alert("Heartbeat", NOW.isoformat(), heartbeat=True),
                _alert("DashboardAlert", NOW.isoformat(), alert_class="dashboard"),
                _alert("AlphaPage", "2099-01-03T10:00:00.123456789Z"),
            )
        )
        code, stdout, stderr, _ = self.run_status(host, fake)

        self.assertEqual((code, stderr), (1, ""))
        alpha = stdout.index("AlphaPage:")
        zeta = stdout.index("ZetaPage:")
        self.assertLess(alpha, zeta)
        self.assertIn("AlphaPage: Fictitious summary — docs/runbooks/observability.md#example (firing 1h)", stdout)
        self.assertNotIn("Heartbeat:", stdout)
        self.assertNotIn("DashboardAlert:", stdout)
        self.assertTrue(stdout.rstrip().endswith("status: 2 need attention"))

    def test_nudge_dashboard_does_not_enter_needs_attention_or_change_exit(self) -> None:
        for with_page in (False, True):
            with self.subTest(with_page=with_page):
                alerts = [
                    _alert(
                        "Proposals waiting",
                        NOW.isoformat(),
                        alert_class="dashboard",
                        nudge="true",
                    )
                ]
                if with_page:
                    alerts.append(_alert("Fictitious page", NOW.isoformat()))
                code, stdout, stderr, _ = self.run_status(
                    self.make_host(), FakeGrafana(tuple(alerts))
                )
                self.assertEqual((code, stderr), (int(with_page), ""))
                needs_attention = stdout.split("waiting on you", 1)[0]
                self.assertNotIn("Proposals waiting", needs_attention)
                self.assertEqual("Fictitious page:" in needs_attention, with_page)
                self.assertTrue(stdout.rstrip().endswith(
                    "status: 1 need attention" if with_page
                    else "status: nothing needs attention"
                ))

    def test_unreachable_grafana_and_factory_error_exit_two_but_print_other_blocks(self) -> None:
        cases = (
            (self.make_host(), FakeGrafana(error=grafana.GrafanaError("fictional Grafana failure")), None),
            (self.make_host(), None, grafana.GrafanaError("fictional constructor failure")),
        )
        for host, fake, factory_error in cases:
            with self.subTest(factory_error=factory_error is not None):
                code, stdout, stderr, _ = self.run_status(
                    host, fake, factory_error=factory_error
                )
                self.assertEqual((code, stderr), (2, ""))
                self.assertIn("needs attention\ncould not check —", stdout)
                self.assertIn("waiting on you\nnone\nat a glance", stdout)
                self.assertTrue(stdout.rstrip().endswith("status: could not check"))

    def test_non_root_and_invalid_site_refuse_on_stderr(self) -> None:
        host = self.make_host(euid=1000)
        code, stdout, stderr, _ = self.run_status(host)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("root privileges are required", stderr)
        self.assertIn("sudo python3 -m gideon status", stderr)

        invalid = self.make_host()
        invalid.files[os.fspath(BAD_SITE_PATH)] = "hostname: []\n"
        code, stdout, stderr, _ = self.run_status(invalid, site_path=BAD_SITE_PATH)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("gideon status:", stderr)
        self.assertIn("Correct the site file", stderr)

    def test_main_reads_command_form_before_status_refuses_without_box_access(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        with (
            patch.object(RealHost, "geteuid", return_value=1000) as geteuid,
            patch.object(RealHost, "read_text", side_effect=AssertionError("box read")) as read_text,
        ):
            for installed in (True, False):
                with self.subTest(installed=installed):
                    environment = (
                        {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                        if installed
                        else {}
                    )
                    out, err = io.StringIO(), io.StringIO()
                    with (
                        patch.dict(os.environ, environment, clear=True),
                        contextlib.redirect_stdout(out),
                        contextlib.redirect_stderr(err),
                    ):
                        code = cli.main(["status"])
                    self.assertEqual(code, 2)
                    self.assertEqual(out.getvalue(), "")
                    expected = "gideon status" if installed else "sudo python3 -m gideon status"
                    self.assertIn(f"Fix: Run {expected}.", err.getvalue())
            self.assertEqual(geteuid.call_count, 2)
            read_text.assert_not_called()

    def test_every_status_command_fix_uses_the_runs_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        command_pattern = re.compile(
            r"(?:sudo python3 -m )?gideon "
            r"(?P<path>backup run|backup push|backup drill|proposals|status|apply)\b"
        )
        expected_paths = {"status", "apply", "backup run", "backup push", "backup drill", "proposals"}

        class MalformedFeedbackClient:
            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del method, path, kwargs
                return owui.Response(200, {})

        def malformed_feedback(**kwargs: object) -> owui.Client:
            del kwargs
            return cast(owui.Client, MalformedFeedbackClient())

        for installed in (True, False):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                printed: list[str] = []

                # The installed wrapper re-executes under sudo before status,
                # so this installed-form non-root refusal is reachable in-process only.
                non_root = self.make_host(euid=1000)
                code, stdout, stderr, _ = self.run_status(non_root)
                self.assertEqual(code, 2)
                printed.extend((stdout, stderr))

                invalid_site = self.make_host()
                invalid_site.files[os.fspath(SITE_PATH)] = "hostname: []\n"
                code, stdout, stderr, _ = self.run_status(invalid_site)
                self.assertEqual(code, 2)
                printed.extend((stdout, stderr))

                failed_readers = self.make_host(
                    build_box=True,
                    set_names=(),
                    compose_rc=1,
                    sibling_compose_rc=1,
                    psql_rc=1,
                    guardrail_rc=1,
                    eval_run_rc=1,
                    df_rc=1,
                    handshake_rc=1,
                )
                del failed_readers.files[os.fspath(secrets.secret_path("grafana_admin_password"))]
                code, stdout, stderr, _ = self.run_status(
                    failed_readers,
                    sections=(ratings.FEEDBACK_SECTION, trips.TRIPS_SECTION),
                )
                self.assertEqual(code, 2)
                printed.extend((stdout, stderr))

                unreadable_registry = self.make_host()
                unreadable_registry.files[os.fspath(TRIGGERS_PATH)] = "version: [not valid\n"
                code, stdout, stderr, _ = self.run_status(
                    unreadable_registry,
                    FakeGrafana(error=grafana.GrafanaError("fictional Grafana failure")),
                )
                self.assertEqual(code, 2)
                printed.extend((stdout, stderr))

                missing_compose = self.make_host(set_names=(), psql_stdout="")
                del missing_compose.files[os.fspath(RENDERED / "compose.yaml")]
                missing_compose.files[os.fspath(secrets.secret_path("gideon_admin_api_key"))] = "fictitious-key"
                code, stdout, stderr, _ = self.run_status(
                    missing_compose,
                    sections=(ratings.FEEDBACK_SECTION,),
                    owui_factory=malformed_feedback,
                )
                self.assertEqual((code, stderr), (0, ""))
                printed.append(stdout)

                prefix = "gideon" if installed else "sudo python3 -m gideon"
                found: set[str] = set()
                for line in "\n".join(printed).splitlines():
                    if installed:
                        self.assertNotIn("python3 -m gideon", line)
                    if "Fix: " not in line:
                        continue
                    fix = line.split("Fix: ", 1)[1]
                    for match in command_pattern.finditer(fix):
                        path = match.group("path")
                        found.add(path)
                        self.assertEqual(match.group(0), f"{prefix} {path}")
                self.assertEqual(found, expected_paths)

    def test_push_uses_the_audit_row_not_a_newer_staging_push_record(self) -> None:
        files = {
            os.fspath(SITE_PATH): SITE_TEXT,
            os.fspath(TRIGGERS_PATH): TRIGGERS_TEXT,
            os.fspath(RENDERED / "compose.yaml"): COMPOSE_TEXT,
            os.fspath(secrets.secret_path("grafana_admin_password")): "fictitious-secret\n",
            os.fspath(STAGING / "push.json"): json.dumps(
                {"pushed_at": (NOW - timedelta(minutes=5)).isoformat(), "newest_set": "later-failed-push"}
            ),
            os.fspath(STAGING / "sets" / SET_LABEL / backupset.MANIFEST_NAME): _manifest(
                NOW - timedelta(hours=3)
            ),
        }
        host = ReadOnlyHost(
            files=files,
            directories=(os.fspath(STAGING / "sets"),),
            psql_stdout=f"backup_push|{(NOW - timedelta(hours=1)).isoformat()}|verified-push\n",
        )
        self.hosts.append(host)
        code, stdout, stderr, _ = self.run_status(host, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("off-box push: verified-push pushed 1h ago", stdout)
        self.assertNotIn("later-failed-push", stdout)

    def test_failed_audit_read_marks_both_rows_with_postgres_logs_fix(self) -> None:
        host = self.make_host(psql_rc=1)
        facts = glance.record_facts(host, RENDERED, NOW)
        push_line, drill_line = (glance.fact_line(fact) for fact in facts)
        logs = stack.logs_fix(RENDERED, "postgres")

        self.assertIn("off-box push: could not read", push_line)
        self.assertIn("drill: could not read", drill_line)
        self.assertTrue(push_line.endswith(f"Fix: {logs}"))
        self.assertTrue(drill_line.endswith(f"Fix: {logs}"))

    def test_waiting_block_prints_only_fired_office_rows_and_skips_product(self) -> None:
        host = self.make_host()
        fired = FakeSection(
            "office-fired",
            "office",
            SectionReport("section detail", (
                Row("owed-action", "fired", "one action"),
                Row("rated-item", "rated", "feedback count"),
                Row("quiet", "not fired", "none"),
            )),
        )
        none = FakeSection(
            "office-none",
            "office",
            SectionReport("no pending work", (Row("not-owed", "not fired", "none"),)),
        )
        problem = FakeSection(
            "office-problem", "office", Problem("fictional read failed", "Run the fictional reader.")
        )
        product = FakeSection(
            "product-section", "product", SectionReport("never", (Row("hidden", "fired", "hidden"),))
        )
        code, stdout, stderr, _ = self.run_status(
            host, FakeGrafana(), sections=(fired, none, problem, product)
        )

        self.assertEqual((code, stderr), (0, ""))
        waiting = stdout.split("waiting on you\n", 1)[1].split("\nat a glance", 1)[0]
        self.assertIn("owed-action: one action", waiting)
        self.assertIn("office-problem: fictional read failed Fix: Run the fictional reader.", waiting)
        self.assertNotIn("quiet", waiting)
        self.assertNotIn("rated-item", waiting)
        self.assertNotIn("not-owed", waiting)
        self.assertNotIn("product-section", waiting)
        self.assertEqual(product.calls, 0)

    def test_office_sections_share_feedback_and_guardrail_rows_do_not_wait(self) -> None:
        host = self.make_host(
            guardrail_stdout="fictional-family|fictional-pattern|fictional-chat|1\n"
        )
        reading = feedback.FeedbackReading(
            (
                feedback.FeedbackRecord(
                    "down",
                    "fictional-chat",
                    "fictional-message",
                    "fictional-model",
                    int(NOW.timestamp()) - 10,
                ),
            ),
            0,
        )

        class Source:
            def __init__(self) -> None:
                self.calls = 0

            def read(self) -> feedback.FeedbackReading:
                self.calls += 1
                return reading

        source = Source()
        with patch.object(command.owuifeedback, "source", return_value=source):
            code, stdout, stderr, _ = self.run_status(
                host,
                FakeGrafana(),
                sections=(ratings.FEEDBACK_SECTION, trips.TRIPS_SECTION),
            )

        waiting = stdout.split("waiting on you\n", 1)[1].split("\nat a glance", 1)[0]
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(waiting, "none")
        self.assertEqual(source.calls, 1)
        self.assertEqual(
            sum("FROM guardrail_trips" in (input_text or "") for _argv, input_text in host.calls),
            1,
        )

    def test_refused_guardrail_read_is_one_waiting_line_with_its_fix(self) -> None:
        host = self.make_host(guardrail_rc=1)
        code, stdout, stderr, _ = self.run_status(
            host, FakeGrafana(), sections=(trips.TRIPS_SECTION,)
        )

        waiting = stdout.split("waiting on you\n", 1)[1].split("\nat a glance", 1)[0]
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(waiting.count("guardrail:"), 1)
        self.assertIn("guardrail: metrics reader failed with exit code 1 Fix:", waiting)
        self.assertTrue(waiting.endswith("then retry."))

    def test_feedback_context_fields_and_owui_factory_reach_read(self) -> None:
        host = self.make_host()
        host.files[os.fspath(secrets.secret_path("gideon_admin_api_key"))] = "fictional-admin-key"
        section = FeedbackContextSection()
        calls: list[tuple[str, str]] = []

        class Client:
            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del kwargs
                calls.append((method, path))
                return owui.Response(200, {"items": [], "total": 0})

        def factory(**kwargs: object) -> owui.Client:
            self.assertEqual(kwargs.get("api_key"), "fictional-admin-key")
            return cast(owui.Client, Client())

        code, _stdout, stderr, _ = self.run_status(
            host, FakeGrafana(), sections=(section,), owui_factory=factory
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(section.reading, feedback.FeedbackReading((), 0))
        self.assertEqual(section.clock, NOW.timestamp())
        self.assertEqual(calls, [("GET", "/api/v1/evaluations/feedbacks/list?page=1")])

    def test_frontend_down_refuses_each_office_section_from_one_shared_read(self) -> None:
        host = self.make_host()
        host.files[os.fspath(secrets.secret_path("gideon_admin_api_key"))] = "fictional-admin-key"
        factory_calls = 0

        def factory(**kwargs: object) -> owui.Client:
            nonlocal factory_calls
            factory_calls += 1
            del kwargs
            raise owui.OwuiError("Open WebUI request failed.", "Check Open WebUI availability, then retry.")

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = command.run_status(
                argparse.Namespace(),
                host=host,
                site_path=SITE_PATH,
                rendered_dir=RENDERED,
                staging=STAGING,
                checkout_root=CHECKOUT,
                triggers_path=TRIGGERS_PATH,
                client_factory=lambda **kwargs: cast(grafana.Client, FakeGrafana()),
                owui_client_factory=factory,
                now=NOW,
            )
        waiting = out.getvalue().split("waiting on you\n", 1)[1].split("at a glance\n", 1)[0]
        self.assertEqual(code, 0)
        self.assertEqual(
            waiting,
            "feedback: Open WebUI request failed. Fix: Check Open WebUI availability, then retry.\n"
            "guardrail: Open WebUI request failed. Fix: Check Open WebUI availability, then retry.\n",
        )
        self.assertEqual(factory_calls, 1)

    def test_waiting_block_reports_none_and_unloadable_registry(self) -> None:
        host = self.make_host()
        office = FakeSection(
            "office-none",
            "office",
            SectionReport("no pending work", (Row("not-owed", "not fired", "none"),)),
        )
        code, stdout, stderr, _ = self.run_status(host, FakeGrafana(), sections=(office,))
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("waiting on you\nnone\n", stdout)

        broken = self.make_host()
        broken.files[os.fspath(TRIGGERS_PATH)] = "version: [not valid\n"
        code, stdout, stderr, _ = self.run_status(broken, FakeGrafana())
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("waiting on you\ncould not check — trigger registry could not be loaded Fix:", stdout)

    def test_glance_readers_report_none_yet_for_backup_and_audit_rows(self) -> None:
        host = self.make_host(set_names=(), psql_stdout="")
        backup = glance.backup_set_fact(host, STAGING, NOW)
        push, drill = glance.record_facts(host, RENDERED, NOW)

        self.assertEqual(backup.detail, "none yet")
        self.assertIn("backup run", backup.fix)
        self.assertEqual(push.detail, "none yet")
        self.assertIn("backup push", push.fix)
        self.assertEqual(drill.detail, "none yet")
        self.assertIn("backup drill", drill.fix)

    def test_glance_readers_render_their_failure_lines(self) -> None:
        broken_compose = self.make_host(compose_rc=1)
        service_line = glance.fact_line(glance.services_fact(broken_compose, RENDERED))
        broken_sets = self.make_host()

        def fail_listdir(path: PathLike) -> list[str]:
            if os.fspath(path) == os.fspath(STAGING / "sets"):
                raise PermissionError("fictitious staging refusal")
            return ReadOnlyHost.listdir(broken_sets, path)

        broken_sets.listdir = fail_listdir  # type: ignore[method-assign]
        backup_line = glance.fact_line(glance.backup_set_fact(broken_sets, STAGING, NOW))
        broken_tls = self.make_host(handshake_rc=1)
        tls_line = glance.fact_line(glance.tls_fact(broken_tls, "gideon.example.org", NOW))
        broken_data = self.make_host(df_rc=1)
        data_line = glance.fact_line(glance.data_fact(broken_data))

        for line in (service_line, backup_line, tls_line, data_line):
            with self.subTest(line=line):
                self.assertIn("could not read", line)
                self.assertIn("Fix:", line)

    def test_age_text_boundaries(self) -> None:
        cases = (
            (timedelta(minutes=59, seconds=59), "59m"),
            (timedelta(hours=1), "1h"),
            (timedelta(hours=23, minutes=59), "23h"),
            (timedelta(days=1), "1d 0h"),
            (timedelta(days=2, hours=7, minutes=59), "2d 7h"),
        )
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(glance.age_text(NOW, NOW - age), expected)

    def test_status_package_imports_only_stdlib_yaml_and_gideon(self) -> None:
        package = ROOT / "gideon" / "status"
        allowed = set(sys.stdlib_module_names) | {"yaml", "gideon"}
        paths = sorted(package.glob("*.py"))
        self.assertIn(package / "developer.py", paths)
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    roots = [node.module.split(".", 1)[0]]
                else:
                    continue
                for root in roots:
                    with self.subTest(path=path.name, root=root):
                        self.assertIn(root, allowed)


if __name__ == "__main__":
    unittest.main()
