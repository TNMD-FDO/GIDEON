"""The read-only box status report."""

import argparse
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from unittest.mock import patch

import gideon
from gideon.host import backupset, grafana, owui, secrets, stack
from gideon.host.checks.capacity import DATA_DF_ARGV
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike
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


def _alert(
    title: str,
    started_at: str,
    *,
    alert_class: str = "page",
    heartbeat: str = "false",
    state: str = "active",
    summary: str = "Fictitious summary",
    runbook: str = "docs/runbooks/observability.md#example",
    silenced: bool = False,
) -> grafana.Alert:
    return grafana.Alert(
        labels={"alertname": title, "class": alert_class, "heartbeat": heartbeat},
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
        psql_rc: int = 0,
        psql_stdout: str = "",
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
        self.psql_rc = psql_rc
        self.psql_stdout = psql_stdout
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
                if input is not None and "FROM guardrail_trips" in input:
                    return self._completed(
                        command_argv, self.guardrail_rc, self.guardrail_stdout
                    )
                return self._completed(command_argv, self.psql_rc, self.psql_stdout)
            if "ps" in command_argv:
                return self._completed(command_argv, self.compose_rc, self.compose_stdout)
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
    **overrides: Any,
) -> ReadOnlyHost:
    files = {
        os.fspath(SITE_PATH): SITE_TEXT,
        os.fspath(TRIGGERS_PATH): TRIGGERS_TEXT,
        os.fspath(RENDERED / "compose.yaml"): COMPOSE_TEXT,
        os.fspath(secrets.secret_path("grafana_admin_password")): "fictitious-grafana-secret\n",
    }
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
        if self.scope == "product":
            raise AssertionError("status rendered a product section")
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
                _alert("Heartbeat", NOW.isoformat(), heartbeat="true"),
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
        for path in sorted(package.glob("*.py")):
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
