"""The host agreement facade over an in-memory queue and file seam."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host import agreement, report, staging, worker
from gideon.host.render import worker as identity
from gideon.host.sysio import Command, PathLike, RealHost

RENDERED = "/rendered"
WORK_ROOT = Path("/fictitious-work")
LABEL = "corpus-2099-01-03"
SNAPSHOT = "fictions-2099-01-02"
COURT = "court1"
JOB = 31
INPUT = {"path": "citation-map.csv.bz2", "sha256": "a" * 64}


class FakeHost(RealHost):
    """Serve queue rows and durable files without touching the filesystem."""

    def __init__(self) -> None:
        super().__init__()
        self.status = "succeeded"
        self.code = 0
        self.calls: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        self.calls.append((list(argv), input))
        assert input is not None
        output = str(JOB) if "procrastinate_defer_jobs_v1" in input else f"{JOB}|{self.status}|1"
        return subprocess.CompletedProcess(list(argv), self.code, output + "\n", "diagnostic")

    def exists(self, path: PathLike) -> bool:
        return str(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        return self.files[str(path)]


def figure() -> dict[str, object]:
    return {
        "schema": 1, "label": LABEL, "snapshot": SNAPSHOT, "court": COURT,
        "job": JOB, "documents": 5, "gideon_pairs": 4, "map_rows": 6,
        "map_outside": 1, "map_pairs": 3, "agreed": 2, "gideon_only": 2,
        "map_only_seen": 1, "map_only_missed": 0, "seconds": 0.5,
        "computed_at": "2099-01-03T12:00:00+00:00",
    }


def failure(reason: str, *, job: int = JOB) -> dict[str, object]:
    return {
        "schema": 1, "job": job, "court": COURT, "reason": reason,
        "table": None, "error": None, "at": "2099-01-03T12:00:00+00:00",
    }


class AgreementHost(unittest.TestCase):
    """Queue validation, figures, and filed reasons stay bounded and typed."""

    def setUp(self) -> None:
        self.host = FakeHost()
        self.work_dir = staging.work_directory(LABEL, "fictions", work_root=WORK_ROOT)
        self.figure_path = str(self.work_dir / f"{COURT}.{identity.AGREEMENT_RECORD_NAME}")
        self.failure_path = str(self.work_dir / f"{COURT}.{identity.AGREEMENT_FAILURE_NAME}")

    def read(self) -> agreement.AgreementRead | report.Problem:
        return agreement.read_agreement(
            self.host, RENDERED, JOB, label=LABEL, snapshot=SNAPSHOT,
            court=COURT, work_root=WORK_ROOT,
        )

    def test_defer_binds_arguments_and_lock_on_stdin(self) -> None:
        result = agreement.defer_agreement(
            self.host, RENDERED, label=LABEL, snapshot=SNAPSHOT, court=COURT,
            input=INPUT,
        )
        self.assertEqual(result, JOB)
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for value in (LABEL, SNAPSHOT, COURT, identity.AGREEMENT_TASK,
                      identity.AGREEMENT_QUEUE, INPUT["path"], INPUT["sha256"]):
            self.assertNotIn(value, " ".join(argv))
            self.assertIn(value, sql)
        self.assertIn(f"\\set v_lock 'agreement-{LABEL}-{COURT}'", sql)
        self.assertIn("ROW(:'v_queue', :'v_task', 0, :'v_lock', NULL, :'v_args'::jsonb, NULL)", sql)
        args = json.loads(sql.split("\\set v_args '", 1)[1].split("'\n", 1)[0])
        self.assertEqual(args, {"label": LABEL, "snapshot": SNAPSHOT, "court": COURT, "input": INPUT})

    def test_defer_refuses_each_grammar_before_psql(self) -> None:
        base: dict[str, object] = {
            "label": LABEL, "snapshot": SNAPSHOT, "court": COURT, "input": INPUT,
        }
        cases: tuple[tuple[str, object, str], ...] = (
            ("label", "../other", "corpus-YYYY-MM-DD"),
            ("label", "corpus-2099-13-03", "real date"),
            ("snapshot", "../other", "<source>-YYYY-MM-DD"),
            ("snapshot", "fictions-2099-13-02", "real date"),
            ("court", "Court1", "lowercase court id"),
            ("court", "c" * 33, "32"),
            ("input", {}, "one map input"),
            ("input", {"path": "../map", "sha256": INPUT["sha256"]}, "one-segment"),
            ("input", {"path": "map.partial", "sha256": INPUT["sha256"]}, "reserved suffix"),
            ("input", {"path": INPUT["path"], "sha256": "A" * 64}, "lowercase sha256"),
        )
        for field, bad, grammar in cases:
            with self.subTest(field=field, bad=bad):
                self.host.calls.clear()
                result = agreement.defer_agreement(
                    self.host, RENDERED, **{**base, field: bad},  # type: ignore[arg-type]
                )
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn(field, result.problem)
                self.assertIn(grammar, result.fix)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertEqual(self.host.calls, [])

    def test_record_fields_arithmetic_and_shares(self) -> None:
        self.host.files[self.figure_path] = json.dumps(figure())
        result = agreement.read_record(
            self.host, LABEL, SNAPSHOT, COURT, work_root=WORK_ROOT,
        )
        self.assertIsInstance(result, agreement.AgreementRecord)
        assert isinstance(result, agreement.AgreementRecord)
        self.assertEqual((result.gideon_share(), result.map_share()), (50, 67))
        empty = {**figure(), "gideon_pairs": 0, "map_pairs": 0, "agreed": 0,
                 "gideon_only": 0, "map_only_seen": 0}
        self.host.files[self.figure_path] = json.dumps(empty)
        result = agreement.read_record(self.host, LABEL, SNAPSHOT, COURT, work_root=WORK_ROOT)
        assert isinstance(result, agreement.AgreementRecord)
        self.assertEqual((result.gideon_share(), result.map_share()), (0, 0))

        bad_fields: tuple[tuple[str, object], ...] = (
            ("schema", True), ("schema", 2), ("label", "corpus-2099-01-04"),
            ("snapshot", "fictions-2099-01-03"), ("court", "court2"),
            ("job", 0), ("job", True), ("seconds", float("inf")),
            ("seconds", True), ("seconds", -1), ("computed_at", "2099-01-03"),
            ("agreed", 5), ("gideon_only", 1), ("map_only_seen", 0),
            ("map_outside", 7),
        )
        for name in ("documents", "gideon_pairs", "map_rows", "map_outside",
                     "map_pairs", "agreed", "gideon_only", "map_only_seen", "map_only_missed"):
            bad_fields += ((name, -1), (name, True), (name, "1"))
        for name, bad in bad_fields:
            with self.subTest(name=name, bad=bad):
                self.host.files[self.figure_path] = json.dumps({**figure(), name: bad})
                self.assertIsInstance(
                    agreement.read_record(self.host, LABEL, SNAPSHOT, COURT, work_root=WORK_ROOT),
                    report.Problem,
                )
        for value in ({**figure(), "extra": 1},
                      {key: value for key, value in figure().items() if key != "documents"}):
            self.host.files[self.figure_path] = json.dumps(value)
            self.assertIsInstance(
                agreement.read_record(self.host, LABEL, SNAPSHOT, COURT, work_root=WORK_ROOT),
                report.Problem,
            )

    def test_read_outcomes_and_stale_figure(self) -> None:
        for status in ("todo", "doing"):
            self.host.status = status
            result = self.read()
            self.assertIsInstance(result, agreement.AgreementRead)
            assert isinstance(result, agreement.AgreementRead)
            self.assertFalse(result.done)
            self.assertIsNone(result.record)
        self.host.status = "succeeded"
        missing = self.read()
        self.assertIsInstance(missing, report.Problem)
        self.host.files[self.figure_path] = json.dumps(figure())
        result = self.read()
        assert isinstance(result, agreement.AgreementRead)
        self.assertTrue(result.done)
        self.assertIsInstance(result.record, agreement.AgreementRecord)
        self.host.files[self.figure_path] = json.dumps({**figure(), "job": JOB + 1})
        stale = self.read()
        self.assertIsInstance(stale, report.Problem)
        assert isinstance(stale, report.Problem)
        self.assertIn("logs", stale.fix)
        self.host.files[self.figure_path] = "not json"
        self.assertIsInstance(self.read(), report.Problem)

    def test_each_failure_reason_has_its_fix(self) -> None:
        self.host.status = "failed"
        expected = {
            "missing-input": ("citation-map", ".fetch.json", "corpus install"),
            "input-mismatch": ("citation-map", ".fetch.json", "corpus install"),
            "missing-stage": (str(self.work_dir), "corpus install"),
            "stage-mismatch": (str(self.work_dir), "corpus install"),
            "malformed": ("logs", "corpus install"),
            "database": ("host provision", "apply", "corpus install"),
            "local": ("host provision", "apply", "corpus install"),
            "busy": ("Wait", "corpus install"),
            "invalid": ("logs", "corpus install"),
        }
        self.assertEqual(set(expected), identity.AGREEMENT_FAILURE_REASONS)
        for reason, fix_parts in expected.items():
            with self.subTest(reason=reason):
                self.host.files[self.failure_path] = json.dumps(failure(reason))
                result = self.read()
                assert isinstance(result, agreement.AgreementRead)
                self.assertEqual(result.reason, reason)
                self.assertFalse(result.done)
                assert result.failure is not None
                for part in fix_parts:
                    self.assertIn(part, result.failure.fix)
        self.host.files[self.failure_path] = json.dumps({**failure("malformed"), "table": "opinions"})
        result = self.read()
        assert isinstance(result, agreement.AgreementRead)
        assert result.failure is not None
        self.assertIn("opinions", result.failure.problem)

    def test_terminal_without_this_jobs_failure_is_local(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for filed in (None, failure("invalid", job=JOB + 1)):
                with self.subTest(status=status, filed=filed):
                    self.host.status = status
                    self.host.files.clear()
                    if filed is not None:
                        self.host.files[self.failure_path] = json.dumps(filed)
                    result = self.read()
                    assert isinstance(result, agreement.AgreementRead)
                    self.assertEqual(result.reason, "local")
                    assert result.failure is not None
                    self.assertIn("host provision", result.failure.fix)

    def test_failure_file_validates_each_field_and_status(self) -> None:
        self.host.status = "failed"
        bad_fields: tuple[tuple[str, object], ...] = (
            ("schema", True), ("court", "court2"),
            ("reason", "invented"), ("table", "invented"),
            ("error", "private text"), ("at", "2099-01-03"),
        )
        for name, bad in bad_fields:
            with self.subTest(name=name):
                self.host.files[self.failure_path] = json.dumps({**failure("invalid"), name: bad})
                self.assertIsInstance(self.read(), report.Problem)
        self.host.files[self.failure_path] = json.dumps({**failure("invalid"), "extra": 1})
        self.assertIsInstance(self.read(), report.Problem)
        self.host.status = "unknown"
        self.assertIsInstance(self.read(), report.Problem)
