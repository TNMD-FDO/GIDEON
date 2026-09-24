"""The command contracts for ordered Grafana contact points."""

import argparse
import contextlib
import io
import json
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gideon.host import alerts, audit, grafana
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.site import load_site
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
RENDERED = "/etc/gideon/rendered"
COMPOSE = (ROOT / "tests/fixtures/render/example/compose.yaml").read_text()


class FakeHost:
    def __init__(self, *, euid: int = 0, compose: str = COMPOSE, secret: str | None = "break-glass", site: bool = True) -> None:
        self.euid = euid
        self.files = {
            f"{RENDERED}/compose.yaml": compose,
        }
        if site:
            self.files[str(EXAMPLE)] = EXAMPLE.read_text()
        if secret is not None:
            self.files["/etc/gideon/secrets/grafana_admin_password"] = secret

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
        del check, input, cwd, env, timeout
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def geteuid(self) -> int:
        return self.euid


class FakeAudit:
    AuditRow = audit.AuditRow

    def __init__(self, *, probe_problem: str | None = None, write_problem: str | None = None) -> None:
        self.probe_problem = probe_problem
        self.write_problem = write_problem
        self.rows: list[audit.AuditRow] = []

    def probe(self, io: FakeHost, rendered_dir: PathLike) -> str | None:
        del io, rendered_dir
        return self.probe_problem

    def write_rows(self, io: FakeHost, rendered_dir: PathLike, rows: tuple[audit.AuditRow, ...]) -> str | None:
        del io, rendered_dir
        self.rows.extend(rows)
        return self.write_problem


class FakeGrafana:
    def __init__(self, *, ready: bool = True, receiver: grafana.Receiver | None = None, result: grafana.TestResult | None = None) -> None:
        self.ready = ready
        self.receiver = receiver
        self.result = result or grafana.TestResult(True, 200)
        self.credentials: list[tuple[str, str] | None] = []
        self.tested: tuple[grafana.Receiver, Mapping[str, object], tuple[str, ...]] | None = None

    def request(self, method: str, path: str) -> grafana.Response:
        del method, path
        return grafana.Response(200 if self.ready else 503)

    def get_receiver(self, name: str) -> grafana.Receiver | None:
        assert name == "page"
        return self.receiver

    def test_receiver(self, receiver: grafana.Receiver, integration: Mapping[str, object], *, recipients: Sequence[str]) -> grafana.TestResult:
        self.tested = (receiver, integration, tuple(recipients))
        return self.result


class CommandTests(unittest.TestCase):
    def run_command(
        self,
        host: Any,
        fake_grafana: FakeGrafana,
        audit_backend: FakeAudit | None = None,
    ) -> tuple[int, str, FakeAudit]:
        backend = audit_backend or FakeAudit()
        out = io.StringIO()

        def client_factory(*, credential: tuple[str, str] | None = None) -> Any:
            return self._client(fake_grafana, credential)

        with contextlib.redirect_stdout(out):
            code = alerts.run_alerts_test(
                argparse.Namespace(),
                host=host,
                site_path=EXAMPLE,
                rendered_dir=RENDERED,
                client_factory=client_factory,
                audit=backend,
                sleep=lambda _: None,
            )
        return code, out.getvalue(), backend

    @staticmethod
    def _client(fake: FakeGrafana, credential: tuple[str, str] | None) -> FakeGrafana:
        fake.credentials.append(credential)
        return fake

    def receiver(
        self,
        receiver_type: str = "email",
        *,
        count: int = 1,
        addresses: str = "csa1@example.org;csa2@example.org",
    ) -> grafana.Receiver:
        integrations = tuple(
            {
                "uid": f"page-email-{index}",
                "type": receiver_type,
                "version": "1",
                "disableResolveMessage": False,
                "settings": {"addresses": addresses, "singleEmail": True},
                "secureFields": {},
            }
            for index in range(count)
        )
        return grafana.Receiver("page-email", "page", integrations)

    def test_success_has_ordered_rows_and_safe_audit_detail(self) -> None:
        fake = FakeGrafana(receiver=self.receiver())
        code, out, backend = self.run_command(FakeHost(), fake)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(
            [line.split(":", 1)[0] for line in lines[:4]],
            ["preconditions", "grafana", "send", "audit"],
        )
        self.assertEqual(lines[-1], "check the inbox")
        self.assertEqual(fake.credentials, [None, (GRAFANA_ADMIN_USER, "break-glass")])
        assert fake.tested is not None
        self.assertEqual(fake.tested[0].uid, "page-email")
        self.assertEqual(fake.tested[1]["uid"], "page-email-0")
        self.assertEqual(fake.tested[2], ("csa1@example.org", "csa2@example.org"))
        self.assertEqual(len(backend.rows), 1)
        detail = backend.rows[0].detail
        site_result = load_site(EXAMPLE)
        assert site_result.config is not None
        self.assertEqual(
            detail,
            {
                "recipients": len(site_result.config.alerts.recipients),
                "relay": (
                    f"{site_result.config.alerts.smtp.host}:"
                    f"{site_result.config.alerts.smtp.port}"
                ),
                "outcome": "ok",
                "smtp_code": None,
            },
        )
        self.assertNotIn("csa1@example.org", json.dumps(detail))

    def test_failed_send_still_records_audit_and_returns_one(self) -> None:
        fake = FakeGrafana(
            receiver=self.receiver(),
            result=grafana.TestResult(False, 200, "smtp returned 550 for <recipient>", "fix", 550),
        )
        code, out, backend = self.run_command(FakeHost(), fake)
        self.assertEqual(code, 1)
        self.assertIn("send: refuse", out)
        self.assertIn("audit: ok", out)
        self.assertEqual(backend.rows[0].detail["outcome"], "failed")
        self.assertEqual(backend.rows[0].detail["smtp_code"], 550)

    def test_missing_or_ambiguous_email_integration_fails_with_apply(self) -> None:
        cases = (None, self.receiver("slack"), self.receiver(count=2))
        for receiver in cases:
            with self.subTest(receiver=receiver):
                fake = FakeGrafana(receiver=receiver)
                code, out, backend = self.run_command(FakeHost(), fake)
                self.assertEqual(code, 1)
                self.assertIn("send: refuse", out)
                self.assertIn("apply", out)
                self.assertEqual(len(backend.rows), 1)
                self.assertEqual(backend.rows[0].detail["outcome"], "failed")

    def test_stored_destination_mismatch_fails_before_sending(self) -> None:
        fake = FakeGrafana(
            receiver=self.receiver(addresses="other@example.org;csa2@example.org")
        )
        code, out, backend = self.run_command(FakeHost(), fake)
        self.assertEqual(code, 1)
        self.assertIn("does not match the site file", out)
        self.assertIn("apply", out)
        self.assertIsNone(fake.tested)
        self.assertEqual(backend.rows[0].detail["outcome"], "failed")

    def test_precondition_refusals_stop_in_the_first_row(self) -> None:
        cases = (
            (FakeHost(euid=1000), "sudo"),
            (FakeHost(site=False), "site file"),
            (FakeHost(compose=""), "rendered Compose"),
            (FakeHost(compose="services: {}\n"), "apply"),
            (FakeHost(secret=None), "apply"),
        )
        for host, expected in cases:
            with self.subTest(expected=expected):
                code, out, backend = self.run_command(host, FakeGrafana(receiver=self.receiver()))
                self.assertEqual(code, 1)
                self.assertTrue(out.startswith("preconditions: refuse"))
                self.assertIn(expected, out)
                self.assertEqual(backend.rows, [])

    def test_grafana_refusal_names_logs_and_does_not_audit(self) -> None:
        code, out, backend = self.run_command(
            FakeHost(), FakeGrafana(ready=False, receiver=self.receiver())
        )
        self.assertEqual(code, 1)
        self.assertIn("grafana: refuse", out)
        self.assertIn("logs grafana", out)
        self.assertEqual(backend.rows, [])


if __name__ == "__main__":
    unittest.main()
